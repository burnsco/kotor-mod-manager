"""
Mod installation pipeline: parallel downloads, sequential installs.
For each mod in build order: download (up to 3 at once) → extract → detect → install.

- Up to 3 mods download concurrently; extraction and install stay sequential so
  patchers never clobber each other.
- Multi-file submissions are downloaded and installed in order.
- TSLPatcher mods go through the strategy cascade (headless HoloPatcher shim →
  Win32 automation → pywinauto → manual GUI) so almost nothing needs a click.
- Install-phase progress is reported so the UI can show live status.
"""
import copy
import json
import re
import shutil
import threading
import time
from urllib.parse import unquote
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from installer.build_directives import match_option_index
from installer.detector import InstallMethod, InstallPlan, ModFileMapping, detect
from installer.extractor import ExtractionError, extract
from installer.download_paths import download_folder_name
from installer.installer import InstallError, install
from installer import texture_dedupe
from installer.pathcase import resolve_ci, rglob_ci
from installer.patcher_strategy import run_tslpatcher_cascade
from installer.runner import PatcherError, run_holopatcher
from scraper.build_scraper import BuildMod
from scraper.deadlystream import DeadlyStreamClient, DownloadError


def _dedupe_keep_order(seq: list) -> list:
    out: list = []
    seen: set = set()
    for s in seq:
        k = str(s).lower().strip()
        if k and k not in seen:
            seen.add(k)
            out.append(s)
    return out


def _resolution_in_name(name: str) -> "tuple[int, int] | None":
    match = re.search(r"(\d{3,4})\s*[x×]\s*(\d{3,4})", name or "", re.I)
    if match:
        return int(match.group(1)), int(match.group(2))
    token = re.search(r"\b(1080p|1440p|2160p|2k|4k)\b", name or "", re.I)
    return {
        "1080p": (1920, 1080), "1440p": (2560, 1440),
        "2k": (2560, 1440), "2160p": (3840, 2160), "4k": (3840, 2160),
    }.get(token.group(1).lower()) if token else None


class ModStatus(Enum):
    PENDING          = "Pending"
    DOWNLOADING      = "Downloading"
    EXTRACTING       = "Extracting"
    READY            = "Ready"
    INSTALLING       = "Installing"
    WAITING_PATCHER  = "Waiting for patcher..."
    DONE             = "Done"
    SKIPPED          = "Skipped"
    MANUAL           = "Manual install needed"
    ERROR            = "Error"


# Downloads run side by side across sites, each with its own limit. Nexus is
# one at a time because a free account needs a click per mod; MEGA and Google
# Drive ration bandwidth per connection.
_HOST_LIMITS = {"deadlystream": 3, "nexus": 1, "mega": 1, "googledrive": 1,
                "github": 2, "direct": 2}
_DEFAULT_HOST_LIMIT = 1
# How long one "Mod manager download" click is waited for before giving up.
_NXM_CLICK_TIMEOUT = 900
# How long a mod the app cannot download waits for the player to add its file.
_DROP_WAIT_SECONDS = 900
_DROP_POLL_SECONDS = 1.0
# When DeadlyStream keeps refusing a mod, the whole mod is tried again after these
# pauses (seconds) before it is reported as failed.
_MOD_RETRY_WAITS = (45, 90)
# A file counts as finished once its size has not changed for this long.
_DROP_SETTLE_SECONDS = 2.5
_DROP_ARCHIVES = (".zip", ".7z", ".rar")
# One worker per download that can run at once, plus spares for the sites that
# fall back to the default limit. Downloads are not held back while earlier mods
# wait (a Nexus click, say): installs run in build order, but every other download
# carries on, and the archives stay on disk either way.
_DOWNLOAD_WORKERS = sum(_HOST_LIMITS.values()) + 2 * _DEFAULT_HOST_LIMIT


def _download_host(pm) -> str:
    return getattr(pm.build_mod, "source_host", "") or "deadlystream"


class ManualInstallRequired(Exception):
    """
    Raised when a mod can only be installed by hand (no recognised auto-install
    method). Not a failure - it carries the extracted folder and any readme so
    the UI can guide the player through finishing it.
    """
    def __init__(self, mod_root: Path, readme: str = ""):
        self.mod_root = mod_root
        self.readme = readme
        super().__init__(f"Manual install required: {mod_root}")


@dataclass
class PipelineMod:
    build_mod: BuildMod
    status: ModStatus = ModStatus.PENDING
    archive_paths: list[Path] = field(default_factory=list)
    extracted_paths: list[Path] = field(default_factory=list)
    plans: list[InstallPlan] = field(default_factory=list)
    error: str = ""
    download_progress: float = 0.0   # 0.0–1.0
    download_kb: int = 0
    download_total_kb: int = 0
    strategy_used: str = ""

    # Back-compat single-value accessors
    @property
    def archive_path(self) -> Optional[Path]:
        return self.archive_paths[0] if self.archive_paths else None

    @property
    def extracted_path(self) -> Optional[Path]:
        return self.extracted_paths[0] if self.extracted_paths else None

    @property
    def plan(self) -> Optional[InstallPlan]:
        return self.plans[0] if self.plans else None


# Callbacks
StatusCallback   = Callable[[str, ModStatus, str], None]  # (file_id, status, detail)
LogCallback      = Callable[[str, str], None]              # (message, tag)
ProgressCB       = Callable[[str, float, int, int], None]  # (file_id, pct, kb, total_kb)
InstallProgressCB = Callable[[str, float, str], None]      # (file_id, pct, label)
ManualCB         = Callable[[str, str, str, str], None]     # (file_id, name, folder, readme)
ConfirmCB        = Callable[[str, str, str, list], None]    # (request_id, title, body, options)


class Pipeline:
    def __init__(
        self,
        mods: list[BuildMod],
        game_path: Path,
        download_dir: Path,
        client: DeadlyStreamClient,
        on_status: Optional[StatusCallback] = None,
        on_log: Optional[LogCallback] = None,
        on_progress: Optional[ProgressCB] = None,
        on_install_progress: Optional[InstallProgressCB] = None,
        on_manual: Optional[ManualCB] = None,
        on_confirm: Optional[ConfirmCB] = None,
        auto_unattended: bool = False,
        game_key: str = "",
        game_type: str = "",
        record_to_library: bool = True,
        language: str = "en",
        screen_resolution: str = "1920x1080",
        browser_download_mode: Optional[bool] = None,
    ):
        self._mods = [PipelineMod(m) for m in mods]
        self._game_path = game_path
        self._download_dir = download_dir
        self._client = client
        self._on_status = on_status
        self._on_log = on_log
        self._on_progress = on_progress
        self._on_install_progress = on_install_progress
        self._on_manual = on_manual
        self._on_confirm = on_confirm
        # Open questions for the player: request id -> (event, answer holder).
        self._confirms: dict[str, tuple[threading.Event, list[str]]] = {}
        # Downloads run side by side, and the window shows one question at a time.
        self._confirm_lock = threading.Lock()
        # Mods the player chose not to download when asked which version to get.
        self._variant_skipped: set = set()
        # When True, never fall back to a manual GUI click (fully unattended).
        self._auto_unattended = auto_unattended
        # Mod-manager recording. game_key is the manifest scope (profile id or
        # game); game_type is the actual game ("KOTOR1"/"KOTOR2"). Empty key
        # disables recording.
        self._game_key = game_key
        self._game_type = game_type or game_key
        self._record_to_library = record_to_library and bool(game_key)
        # Player's game language: bundled translation patches for other
        # languages are skipped at download AND at install (stale caches).
        self._language = language or "en"
        # Used for the widescreen patches, and shown to the player when a mod
        # ships per-resolution packs and they are asked which one to get.
        self._screen_resolution = screen_resolution

        self._browser_download_mode = bool(browser_download_mode)
        self._browser_download_lock = threading.Lock()

        self._stop_event = threading.Event()
        # Free Nexus accounts: the clicks are collected ahead of the downloads.
        self._nexus_lock = threading.Lock()
        self._nexus_chosen_cache: dict = {}
        self._nxm_links: dict = {}            # (game, mod, file) -> link clicked ahead
        self._nxm_pending_mods: set = set()   # mods the click queue still means to ask about
        self._nxm_queue_done = threading.Event()
        self._nxm_queue_done.set()            # no queue running
        self._dl_started: set = set()         # mods whose download has begun
        self._pause_event = threading.Event()
        self._pause_event.set()  # not paused initially
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._manual_left: list[tuple[str, str]] = []
        # Mods that edit the game's program file; handled together at the end.
        self._tool_mods: list[PipelineMod] = []
        # The guide's "Remove Duplicate TGA/TPC" entries; the app does it itself.
        self._cleanup_mods: list[PipelineMod] = []
        self._current: Optional[PipelineMod] = None

    @property
    def mods(self) -> list[PipelineMod]:
        return self._mods

    def start(self) -> None:
        if self._running:
            return
        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def confirm(self, title: str, body: str, options: Optional[list[str]] = None) -> str:
        """
        Ask the player a question and wait for the answer.

        Returns the chosen option, or "" when nobody can answer: no window to
        ask in (a headless run) or the run being stopped. Callers treat "" as
        "no", so nothing is ever changed without an explicit yes. The app's
        "unattended" flag only means "no manual GUI clicks", and does not stop
        a question appearing in the app's own window.
        """
        options = options or ["continue", "skip"]
        if not self._on_confirm:
            return ""
        with self._confirm_lock:
            if self._stop_event.is_set():
                return ""
            request_id = f"c{int(time.time() * 1000)}"
            event, answer = threading.Event(), []
            self._confirms[request_id] = (event, answer)
            try:
                self._on_confirm(request_id, title, body, options)
                while not event.wait(0.25):
                    if self._stop_event.is_set():
                        return ""
                return answer[0] if answer else ""
            finally:
                self._confirms.pop(request_id, None)

    def answer_confirm(self, request_id: str, choice: str) -> bool:
        """Deliver the player's answer to a waiting confirm(). False if none waits."""
        entry = self._confirms.get(request_id)
        if not entry:
            return False
        entry[1].append(choice)
        entry[0].set()
        return True

    def stop(self) -> None:
        self._stop_event.set()
        self._pause_event.set()

    def pause(self) -> None:
        self._pause_event.clear()

    def resume(self) -> None:
        self._pause_event.set()

    def retry_failed(self) -> None:
        """Reset errored mods to pending so a fresh start() re-attempts them."""
        for pm in self._mods:
            if pm.status == ModStatus.ERROR:
                pm.status = ModStatus.PENDING
                pm.error = ""

    @property
    def is_running(self) -> bool:
        return self._running

    # ------------------------------------------------------------------

    def _set_status(self, mod: PipelineMod, status: ModStatus, detail: str = "") -> None:
        mod.status = status
        if self._on_status:
            self._on_status(mod.build_mod.file_id, status, detail)

    def _log(self, msg: str, tag: str = "") -> None:
        """
        Emit a log line, swallowing any failure in the consumer's callback.

        This runs inside the per-mod install try block, so an exception raised by
        a caller's logging code would mark the mod itself as failed. That is a
        real trap on Windows: the pipeline logs box-drawing characters and mod
        names with curly quotes, and printing those to a cp1252 console raises
        UnicodeEncodeError. Losing a log line is acceptable; losing an install
        because of one is not.
        """
        if not self._on_log:
            return
        try:
            self._on_log(msg, tag)
        except Exception:
            pass

    def _install_progress(self, mod: PipelineMod, pct: float, label: str) -> None:
        if self._on_install_progress:
            self._on_install_progress(mod.build_mod.file_id, pct, label)

    def _run(self) -> None:
        self._capture_baseline_once()
        self._apply_layer_order()
        pending = [pm for pm in self._mods if pm.status not in (ModStatus.DONE, ModStatus.SKIPPED)]
        self._resolve_skip_constraints()
        self._check_dependencies()
        if not self._browser_download_mode:
            self._start_click_queue(pending)
        try:
            with ThreadPoolExecutor(max_workers=_DOWNLOAD_WORKERS, thread_name_prefix="mod-dl") as pool:
                futures: dict = {}   # Future -> PipelineMod, until its install turn
                queue = list(pending)
                active: dict = {}    # site -> downloads running right now
                # Guards queue, futures and active. Re-entrant: a download that
                # finishes instantly runs its done-callback inside submit().
                lock = threading.RLock()

                def finished(host: str) -> None:
                    with lock:
                        active[host] -= 1

                def fill_pool() -> None:
                    """Start the earliest queued mods whose site has a free slot,
                    so one site's backlog (a Nexus click, a MEGA quota) never
                    holds up downloads from the others."""
                    with lock:
                        i = 0
                        while i < len(queue) and not self._stop_event.is_set():
                            host = _download_host(queue[i])
                            limit = (
                                1 if (self._browser_download_mode and host in ("deadlystream", "nexus"))
                                else _HOST_LIMITS.get(host, _DEFAULT_HOST_LIMIT)
                            )
                            if active.get(host, 0) >= limit:
                                i += 1
                                continue
                            active[host] = active.get(host, 0) + 1
                            pm = queue.pop(i)
                            fut = pool.submit(self._download_mod, pm)
                            fut.add_done_callback(lambda _f, h=host: finished(h))
                            futures[fut] = pm

                def dispatch() -> None:
                    """Keep downloads starting for as long as there are any to
                    start. This runs on its own thread, because the thread that
                    installs mods can be busy for minutes at a time (a patcher, a
                    large extraction), and downloads must not wait for it."""
                    while not self._stop_event.is_set():
                        fill_pool()
                        with lock:
                            if not queue:
                                return
                        self._stop_event.wait(0.5)

                threading.Thread(target=dispatch, name="mod-dispatch", daemon=True).start()

                for pm in pending:
                    # Installs stay in build order: wait for this mod's own
                    # download while the dispatcher keeps the others going.
                    # _download_mod never raises; errors are stored on
                    # pm.status / pm.error instead.
                    f = None
                    while not self._stop_event.is_set():
                        with lock:
                            f = next((k for k, v in futures.items() if v is pm), None)
                        if f is None:
                            self._stop_event.wait(0.25)     # not started yet
                            continue
                        wait([f], timeout=0.5)
                        if f.done():
                            break
                    if self._stop_event.is_set():
                        break

                    try:
                        f.result()
                    except Exception as e:
                        self._log(f"  Unexpected pipeline error: {e}", "error")
                        pm.status = ModStatus.ERROR
                        pm.error = str(e)
                    with lock:
                        del futures[f]

                    if pm.status == ModStatus.ERROR:
                        continue

                    self._pause_event.wait()
                    self._current = pm
                    self._extract_and_install(pm)
        except Exception as e:
            self._log(f"Pipeline crashed unexpectedly: {e}", "error")
        finally:
            self._running = False
            self._current = None
            self._run_exe_tools()
            self._run_texture_cleanup()
            self._log_manual_summary()
            self._log_overlap_summary()

    def _run_texture_cleanup(self) -> None:
        """
        Remove textures that sit in Override in two formats, as the last step.

        The game can crash when a .tga and a .tpc of the same name disagree, and
        stacking texture packs creates these pairs. The build guides make clearing
        them a mandatory final step, so it runs after every install that changed
        something, whether or not the guide's own entry was selected.
        """
        steps, self._cleanup_mods = self._cleanup_mods, []
        installed = any(pm.status == ModStatus.DONE for pm in self._mods)
        if self._stop_event.is_set() or not (installed or steps):
            return
        override = resolve_ci(self._game_path, "Override")
        if not override.is_dir():
            return
        self._log("\n── Duplicate textures")
        result = texture_dedupe.dedupe(override, on_log=lambda m: self._log("  " + m, "muted"))
        for pm in steps:
            if result.ok:
                self._set_status(pm, ModStatus.DONE)
            else:
                reason = (f"{len(result.failed)} duplicate texture(s) could not be removed. "
                          f"Check that the game's Override folder is not read-only.")
                self._set_status(pm, ModStatus.MANUAL, reason)
                self._manual_left.append((pm.build_mod.name, reason))
        if result.failed:
            self._log("  Some duplicates could not be removed, and they can crash the game.",
                      "warning")

    def _run_exe_tools(self) -> None:
        """Patch swkotor.exe once for every mod that asked for it, after asking."""
        mods, self._tool_mods = self._tool_mods, []
        if not mods or self._stop_event.is_set():
            return
        from installer import exe_setup
        self._log("\n── Game program file (swkotor.exe)")
        steps = {m.build_mod.directives.tool_step for m in mods}
        if "hrmenus" in steps:
            steps.add("uniws")           # the menus patch needs the widescreen patch first

        def finish(ok: bool, reason: str) -> None:
            for m in mods:
                if ok:
                    self._set_status(m, ModStatus.DONE)
                else:
                    self._set_status(m, ModStatus.MANUAL, reason)
                    self._manual_left.append((m.build_mod.name, reason))

        game = self._game_type or "KOTOR1"
        skipped = ("The game program file was not patched, so widescreen will not work "
                   "yet. Run the install again and choose Continue when asked.")
        try:
            if game != "KOTOR1":
                finish(False, "The program file patches are only automated for KOTOR 1.")
                return
            ini = ""
            if "uniws" in steps:
                ini = self._fetch_uniws_ini()
                if not ini:
                    finish(False, "Could not download the widescreen patch data. Check your "
                                  "connection and run the install again.")
                    return
            ok = exe_setup.run(
                self._game_path, sorted(steps), self._screen_resolution, ini,
                confirm=lambda t, b: self.confirm(t, b) == "continue",
                fetch_base=lambda: exe_setup.fetch_editable_exe(
                    self._client, self._download_dir / "_editable_exe", self._log),
                log=self._log, game=game)
            if ok:
                from installer import game_settings
                w, h = (int(x) for x in self._screen_resolution.lower().split("x"))
                if game_settings.set_resolution(self._game_path, w, h):
                    self._log(f"  Set the game's screen size to {w}x{h} in swkotor.ini.", "success")
                for m in mods:
                    if m.build_mod.directives.tool_step == "hrmenus" and m.extracted_paths:
                        exe_setup.copy_gui_set(m.extracted_paths[0], self._game_path,
                                               self._screen_resolution, game, self._log)
            finish(ok, skipped)
        except Exception as e:
            self._log(f"  The program file patches failed: {e}", "error")
            finish(False, skipped)

    def _fetch_uniws_ini(self) -> str:
        """patches.ini from the Universal Widescreen Patcher download."""
        from installer import exe_setup
        from scraper import hosts as _hosts
        dest = self._download_dir / "_uniws"
        try:
            dest.mkdir(parents=True, exist_ok=True)
            cached = dest / "uniws.zip"
            if not cached.is_file():
                s = _hosts._session()
                s.headers["Referer"] = "https://www.wsgf.org/"   # the site refuses requests without it
                cached = _hosts.download_direct("https://www.wsgf.org/downloads/uniws.zip", dest, session=s)
            return exe_setup.load_uniws_ini(cached)
        except Exception as e:
            self._log(f"  Could not get the widescreen patch data: {e}", "warning")
            return ""

    def _log_manual_summary(self) -> None:
        """
        Repeat the steps left for the player, in order, once the run ends.

        The per-mod warning scrolls past among hundreds of log lines, and a
        skipped step (such as the widescreen executable patches) leaves the
        game visibly broken, so the list closes the log where it is seen.
        """
        if not self._manual_left:
            return
        self._log("", "")
        self._log("Steps still left for you to do (in this order):", "warning")
        for i, (name, reason) in enumerate(self._manual_left, 1):
            self._log(f"  {i}. {name}: {reason}", "warning")
        self._manual_left = []

    def _log_overlap_summary(self) -> None:
        """
        Report file overlaps once, in the activity log, rather than as conflicts.

        A curated build produces hundreds of them by design - the guide picked
        these mods together and the install order decides which wins - so
        listing them as conflicts buries the few that matter. Recording the
        count here keeps the information without the false alarm.
        """
        if not self._game_key:
            return
        try:
            from installer import mod_manager
            s = mod_manager.conflict_summary(self._game_key)
            if s["info"]:
                self._log(
                    f"{s['info']} file(s) are provided by more than one mod. The "
                    f"build's install order decides which one wins, so no action "
                    f"is needed.", "muted")
            if s["error"] or s["warning"]:
                self._log(
                    f"{s['error'] + s['warning']} conflict(s) need a look - see "
                    f"the Conflicts tab.", "warning")
        except Exception:
            pass

    def _mod_dir(self, pm: PipelineMod) -> Path:
        mod = pm.build_mod
        return self._download_dir / download_folder_name(mod.file_id, mod.slug)

    def _archive_matches_screen_resolution(self, path: Path) -> bool:
        """Reject a cached/browser archive explicitly tagged for another size."""
        found = _resolution_in_name(path.name)
        if not found:
            return True  # Most mods are not resolution-specific.
        wanted = re.search(r"(\d{3,4})\s*[x×]\s*(\d{3,4})", self._screen_resolution, re.I)
        if not wanted:
            return True
        return found == (int(wanted.group(1)), int(wanted.group(2)))

    def _cached_for(self, pm: PipelineMod, dest_dir: Path) -> list:
        """Complete archives already on disk for this mod, or []."""
        if not dest_dir.exists():
            return []
        self._migrate_encoded_cache_names(dest_dir)
        cached = self._cached_archives(dest_dir)
        # A previous install may have cached a huge movie/menu pack for another
        # display size. Keep unrelated files, but make the selected variant
        # download again when the cache has no matching resolution.
        if cached:
            tagged = [p for p in cached if _resolution_in_name(p.name)]
            if tagged:
                matching = [p for p in tagged if self._archive_matches_screen_resolution(p)]
                if not matching:
                    return []
                cached = matching + [p for p in cached if p not in tagged]
        # An interrupted download leaves some of a mod's files and no record of a
        # finished one. If the guide names the exact file it wants and that file is
        # not among them, these are leftovers, not the mod: download it properly.
        keep = pm.build_mod.directives.download_only
        if (cached and keep and not (dest_dir / self._CACHE_MANIFEST).exists()
                and all(Path(k).suffix.lower() in _DROP_ARCHIVES for k in keep)):
            from scraper.deadlystream import exact_keep_matches
            if not exact_keep_matches([c.name for c in cached], keep):
                return []
        # Honour the guide's download filters on cache reuse too - an old cache
        # may hold every variant of a submission (e.g. HQ Skyboxes' per-mod
        # editions) when the guide wants just one.
        if len(cached) > 1:
            cached = self._filter_cached(cached, pm.build_mod)
        return cached

    def _ask_variant(self, pm: PipelineMod, variants: list[dict]) -> "dict | None":
        """
        A mod that comes in several screen sizes (and sometimes frame rates):
        ask the player which one they want. The app never picks for them, as
        each one can be 15+ GB. Returns the chosen entry of `variants`, or None
        when the player skips the mod or nobody is there to answer.
        """
        from installer import build_overrides
        from scraper.deadlystream import variant_labels
        mod = pm.build_mod
        labels = variant_labels(variants)
        body = (f"{mod.name} comes in {len(variants)} versions and you only need "
                f"one. Pick the one closest to your screen"
                + (f" (yours is {self._screen_resolution.replace('x', ' x ')})"
                   if self._screen_resolution else "")
                + ". Nothing is downloaded until you choose.")
        note = build_overrides.lookup(mod.build_key, mod.file_id,
                                      mod.guide_index or mod.install_order).get("note")
        if note:
            body += f"\n\nFrom the build guide: {note}"
        self._set_status(pm, ModStatus.DOWNLOADING, "Waiting for you to pick a version")
        answer = self.confirm(f"Which version of {mod.name}?", body, [*labels, "skip"])
        if answer in labels:
            self._log(f"  You chose: {answer}")
            self._set_status(pm, ModStatus.DOWNLOADING)
            return variants[labels.index(answer)]
        if self._stop_event.is_set():
            return None
        self._variant_skipped.add(mod.file_id)
        if answer == "skip":
            self._log("  Skipped: you chose not to download any version.", "muted")
        else:
            self._log("  Skipped: this mod comes in several versions and there was "
                      "no window to ask which one you want.", "warning")
        self._set_status(pm, ModStatus.SKIPPED)
        return None

    def _settle_cached_variants(self, pm: PipelineMod, cached: list, dest_dir: Path) -> list:
        """An older version of the app could leave several screen sizes of one
        mod in its folder. Ask which to use instead of installing all of them.
        Returns [] when the player skips the mod."""
        from scraper.deadlystream import resolution_variants
        if pm.build_mod.directives.download_only:
            return cached
        records = [{"name": c.name, "path": c,
                    "size": f"{c.stat().st_size / 1024 ** 3:.2f} GB"}
                   for c in cached]
        variants = resolution_variants(records)
        if not variants:
            return cached
        chosen = self._ask_variant(pm, variants)
        if chosen is None:
            return []
        kept = [r["path"] for r in records
                if r is chosen or not any(r is v for v in variants)]
        # Remember the answer, so pressing Install again does not ask again.
        self._write_cache_manifest(dest_dir, kept)
        return kept

    def _finish_download(self, pm: PipelineMod, archives: list, dest_dir: Path) -> None:
        pm.archive_paths = archives
        self._write_cache_manifest(dest_dir, archives)
        if len(archives) > 1:
            self._log(f"  Downloaded {len(archives)} files.")
        for a in archives:
            self._log(f"  Downloaded: {a.name} ({a.stat().st_size // 1024} KB)")
        # Installs run in build order, so a finished download can wait a while
        # for the mods ahead of it. Say so rather than still "Downloading".
        self._set_status(pm, ModStatus.READY, "Downloaded, waiting for earlier mods")

    # Files the player adds by hand or downloads via browser -------------------

    @staticmethod
    def _get_xdg_downloads_dir() -> "Path | None":
        user_dirs = Path.home() / ".config" / "user-dirs.dirs"
        if not user_dirs.is_file():
            return None
        try:
            for line in user_dirs.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("XDG_DOWNLOAD_DIR"):
                    parts = line.split("=", 1)
                    if len(parts) == 2:
                        val = parts[1].strip().strip('"').replace("$HOME", str(Path.home()))
                        p = Path(val)
                        if p.is_dir():
                            return p
        except Exception:
            pass
        return None

    def _candidate_download_dirs(self) -> list:
        dirs = []
        if self._download_dir:
            dirs.append(self._download_dir)
        is_test_dir = (
            str(self._download_dir).startswith(("/tmp", "/var/tmp"))
            or "pytest" in str(self._download_dir)
        )
        if not is_test_dir:
            xdg = self._get_xdg_downloads_dir()
            if xdg and xdg.is_dir() and xdg not in dirs:
                dirs.append(xdg)
            home_dl = Path.home() / "Downloads"
            if home_dl not in dirs and home_dl.is_dir():
                dirs.append(home_dl)
        return dirs

    @staticmethod
    def _name_score(filename: str, name: str, slug: str = "",
                    directives=None, file_id: str = "") -> int:
        filename_lower = filename.lower()
        score = 0

        # Check name words
        if name:
            words = {w for w in re.split(r"[^a-z0-9]+", name.lower()) if len(w) > 2}
            score += sum(1 for w in words if w in filename_lower)
        if slug:
            slug_words = {w for w in re.split(r"[^a-z0-9]+", slug.lower()) if len(w) > 2}
            score += sum(1 for w in slug_words if w in filename_lower)

        # Check directives download_only
        if directives and getattr(directives, "download_only", None):
            for target in directives.download_only:
                target_lower = target.lower()
                if target_lower == filename_lower or target_lower in filename_lower:
                    score += 50
                else:
                    target_words = {w for w in re.split(r"[^a-z0-9]+", target_lower) if len(w) > 2}
                    score += sum(10 for w in target_words if w in filename_lower)

        return score

    def _dropped_archive(self, pm: PipelineMod, dest_dir: Path, seen: dict,
                         started_at: float = 0) -> "list[Path] | None":
        """An archive the player put in this mod's folder, or loose in the mod
        folder root or browser Downloads folder. Only finished files count: one must
        stop growing first."""
        def settled(p: Path) -> bool:
            try:
                size = p.stat().st_size
            except OSError:
                return False
            now = time.monotonic()
            last, since = seen.get(p, (None, now))
            if size != last:
                since = now
            seen[p] = (size, since)
            if (p.parent / f"{p.name}.part").exists() or (p.parent / f"{p.name}.crdownload").exists():
                return False
            return size > 0 and now - since >= _DROP_SETTLE_SECONDS

        cached = self._cached_for(pm, dest_dir)
        if cached and all(settled(f) for f in cached):
            return cached

        loose = []
        for d in self._candidate_download_dirs():
            try:
                if not d.is_dir():
                    continue
                for p in d.iterdir():
                    if (p.is_file() and not p.name.startswith(".")
                            and p.suffix.lower() in _DROP_ARCHIVES):
                        if (p.parent / f"{p.name}.part").exists() or (p.parent / f"{p.name}.crdownload").exists():
                            continue
                        if started_at and p.stat().st_mtime < started_at - 2:
                            continue
                        if not self._archive_matches_screen_resolution(p):
                            continue
                        loose.append(p)
            except OSError:
                continue

        ready = [p for p in loose if settled(p)]
        if not ready:
            return None

        # Take the best matching file named like this mod
        mod = pm.build_mod
        ready.sort(key=lambda p: self._name_score(
            p.name, mod.name, slug=mod.slug,
            directives=mod.directives, file_id=mod.file_id), reverse=True)

        best = ready[0]
        if self._name_score(best.name, mod.name, slug=mod.slug,
                            directives=mod.directives, file_id=mod.file_id) == 0:
            return None

        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / best.name
        if best.resolve() != target.resolve():
            shutil.move(str(best), str(target))
        return [target]

    def _await_dropped_file(self, pm: PipelineMod, dest_dir: Path, why: str,
                            started_at: float = 0):
        """The app cannot fetch this mod directly. Wait for the player to add
        the file to the mod folder (or drop it loose in the root or Downloads
        folder) and carry on with it."""
        if not self._browser_download_mode:
            self._log(f"  {why} Save the file in {self._download_dir} and the app "
                      f"carries on by itself.", "warning")
            self._set_status(pm, ModStatus.DOWNLOADING, "Add the file to your Downloads folder (Settings>General)")
        seen: dict = {}
        deadline = time.monotonic() + _DROP_WAIT_SECONDS
        while time.monotonic() < deadline and not self._stop_event.is_set():
            found = self._dropped_archive(pm, dest_dir, seen, started_at)
            if found:
                return found
            self._stop_event.wait(_DROP_POLL_SECONDS)
        return None

    def _resolve_browser_download_url(self, pm: PipelineMod) -> str:
        mod = pm.build_mod
        if mod.source_host == "nexus":
            from scraper import nexus
            mod_id = self._nexus_mod_id(mod)
            if mod_id:
                domain = nexus.GAME_DOMAIN.get(mod.game, "kotor")
                return f"https://www.nexusmods.com/{domain}/mods/{mod_id}?tab=files"
            if mod.url:
                return mod.url
            return nexus.search_url(mod.game, mod.name)
        elif mod.source_host == "deadlystream":
            return mod.url or mod.ds_url
        return mod.url or ""

    @staticmethod
    def _open_in_browser(url: str) -> None:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            try:
                import subprocess
                subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass

    def _download_via_browser(self, pm: PipelineMod, dest_dir: Path) -> "list[Path] | None":
        """Guide the player to download via their web browser (Firefox, Edge, etc.)
        and watch the Downloads folder for the resulting archive. Avoids API and
        direct session permission issues on DeadlyStream and Nexus Mods."""
        from scraper.build_scraper import HOST_LABELS
        mod = pm.build_mod

        cached = self._cached_for(pm, dest_dir)
        if cached:
            return cached

        if self._browser_download_lock.locked():
            self._set_status(pm, ModStatus.DOWNLOADING, "Waiting for previous browser download...")

        with self._browser_download_lock:
            if self._stop_event.is_set():
                return None

            cached = self._cached_for(pm, dest_dir)
            if cached:
                return cached

            url = self._resolve_browser_download_url(pm)
            host_label = HOST_LABELS.get(mod.source_host, mod.source_host.title())

            self._log(f"  {host_label}: opening page for \"{mod.name}\" in your browser...", "warning")
            if url:
                self._log(f"  {url}", "muted")

            keep_names = mod.directives.download_only
            if mod.source_host == "nexus":
                self._log(f"  On the Files tab, choose the {self._screen_resolution.replace('x', ' × ')} file when the mod offers resolution variants, then click 'Manual Download' (or 'Slow Download' for free accounts).", "warning")
            elif mod.source_host == "deadlystream":
                self._log(f"  Choose the {self._screen_resolution.replace('x', ' × ')} file if the download list offers resolution variants, then click 'Download this file'.", "warning")

            if keep_names:
                self._log(f"  Build guide recommendation: download only {', '.join(keep_names)}", "warning")

            self._set_status(pm, ModStatus.DOWNLOADING, "Waiting for download in browser...")

            started_at = time.time()
            if url:
                self._open_in_browser(url)

            archives = self._await_dropped_file(
                pm, dest_dir,
                why=f"Waiting for \"{mod.name}\" to be downloaded in your browser.",
                started_at=started_at)
            return archives

    def _download_mod(self, pm: PipelineMod) -> None:
        """Download all files for a mod. Called concurrently; never raises."""
        if self._stop_event.is_set():
            return

        mod = pm.build_mod
        self._dl_started.add(mod.file_id)
        self._log(f"\n── [{mod.install_order:3d}] {mod.name}")

        if texture_dedupe.is_cleanup_step(mod.name):
            self._log("  Nothing to download: the app removes duplicate textures itself "
                      "when the install finishes.", "muted")
            self._cleanup_mods.append(pm)
            self._set_status(pm, ModStatus.READY, "Done when the install finishes")
            return

        if mod.directives.tool_step == "laa":
            self._log("  Nothing to download: the app makes this change itself, together "
                      "with the other game program file patches at the end.", "muted")
            self._set_status(pm, ModStatus.READY, "Done with the program file patches")
            return

        try:
            dest_dir = self._mod_dir(pm)

            # If complete archives are already on disk, skip re-downloading.
            # This makes pressing Install again (or Retry) resumable without
            # restarting every download from zero.
            cached = self._cached_for(pm, dest_dir)
            if len(cached) > 1:
                cached = self._settle_cached_variants(pm, cached, dest_dir)
                if not cached:
                    return
            if cached:
                pm.archive_paths = cached
                total_kb = sum(f.stat().st_size for f in cached) // 1024
                for a in cached:
                    self._log(f"  Cached: {a.name} ({a.stat().st_size // 1024} KB)")
                if self._on_progress:
                    self._on_progress(mod.file_id, 1.0, total_kb, total_kb)
                return

            # Hosts with no downloader (GameFront, unknown) have no
            # DeadlyStream page to fetch either.
            from scraper import hosts as _hosts
            if mod.source_host not in ("deadlystream", "nexus") \
                    and not _hosts.can_download(mod.source_host):
                from scraper.build_scraper import HOST_LABELS
                site = HOST_LABELS.get(mod.source_host, mod.source_host)
                if self._browser_download_mode and mod.url:
                    self._open_in_browser(mod.url)
                archives = self._await_dropped_file(
                    pm, dest_dir, f"The app cannot download from {site} ({mod.url}).")
                if not archives:
                    pm.error = (f"Download this one yourself from {site}"
                                + (f" ({mod.url})" if mod.url else "")
                                + f", save it in {self._download_dir}, then press Install again.")
                    self._set_status(pm, ModStatus.ERROR, pm.error)
                    self._log(f"  {pm.error}", "warning")
                    return
                self._finish_download(pm, archives, dest_dir)
                return

            self._set_status(pm, ModStatus.DOWNLOADING)
            dest_dir.mkdir(parents=True, exist_ok=True)

            def dl_progress(downloaded: int, total: int, filename: str) -> None:
                kb = downloaded // 1024
                total_kb = total // 1024 if total else 0
                pct = downloaded / total if total else 0
                pm.download_kb = kb
                pm.download_total_kb = total_kb
                pm.download_progress = pct
                if self._on_progress:
                    self._on_progress(mod.file_id, pct, kb, total_kb)

            keep_names = mod.directives.download_only
            ignore_names = mod.directives.download_ignore
            if keep_names:
                self._log(
                    f"  Build guide: downloading only {', '.join(keep_names)} "
                    f"(ignoring the other files in this submission).")
            if ignore_names:
                self._log(
                    f"  Build guide: skipping download of {', '.join(ignore_names)}.")
            if self._browser_download_mode and mod.source_host in ("deadlystream", "nexus"):
                archives = self._download_via_browser(pm, dest_dir)
                if not archives:
                    if not pm.error:
                        pm.error = (f"Download timed out or was cancelled for {mod.name}. "
                                    f"Save the file in your Downloads folder and retry.")
                        self._set_status(pm, ModStatus.ERROR, pm.error)
                        self._log(f"  {pm.error}", "error")
                    return
            elif mod.source_host == "nexus":
                archives = self._download_from_nexus(
                    pm, dest_dir, dl_progress, keep_names, ignore_names)
                if archives is None:
                    return
            elif _hosts.can_download(mod.source_host):
                try:
                    archives = _hosts.download_from_host(
                        mod.source_host, mod.url, dest_dir, dl_progress,
                        self._stop_event, self._pause_event)
                except _hosts.HostDownloadError as e:
                    self._set_status(pm, ModStatus.ERROR, str(e))
                    self._log(f"  Download failed: {e}", "error")
                    pm.error = str(e)
                    return
            else:
                import requests
                for attempt in range(len(_MOD_RETRY_WAITS) + 1):
                    try:
                        archives = self._client.download_all_files(
                            file_id=mod.file_id,
                            slug=mod.slug,
                            dest_dir=dest_dir,
                            progress_callback=dl_progress,
                            cancel_event=self._stop_event,
                            pause_event=self._pause_event,
                            keep_names=keep_names,
                            ignore_names=ignore_names,
                            language=self._language,
                            choose_variant=lambda v: self._ask_variant(pm, v),
                        )
                        break
                    except requests.HTTPError as e:
                        code = getattr(e.response, "status_code", 0)
                        if (code not in (403, 429, 502, 503, 504)
                                or attempt == len(_MOD_RETRY_WAITS)
                                or self._stop_event.is_set()):
                            raise
                        wait = _MOD_RETRY_WAITS[attempt]
                        self._log(f"  DeadlyStream is refusing for now (HTTP {code}); "
                                  f"trying this mod again in {wait} seconds.", "warning")
                        self._set_status(pm, ModStatus.DOWNLOADING,
                                         "DeadlyStream is busy, trying again shortly")
                        self._stop_event.wait(wait)
                        if self._stop_event.is_set():
                            raise
                        self._set_status(pm, ModStatus.DOWNLOADING)
            pm.archive_paths = archives
            if not archives and (mod.file_id in self._variant_skipped
                                 or self._stop_event.is_set()):
                return
            if not archives:
                # Everything was filtered out: an over-broad "do not download"
                # instruction. Without this check the mod would sail through
                # the install loop and be reported as installed with nothing
                # on disk.
                self._set_status(pm, ModStatus.ERROR,
                                 "No files were downloaded for this mod.")
                pm.error = ("No files were downloaded for this mod (the build "
                            "guide's download filter matched nothing).")
                self._log(f"  {pm.error}", "error")
                return
            self._finish_download(pm, archives, dest_dir)

        except DownloadError as e:
            self._set_status(pm, ModStatus.ERROR, str(e))
            self._log(f"  Download failed: {e}", "error")
            pm.error = str(e)
        except Exception as e:
            self._set_status(pm, ModStatus.ERROR, str(e))
            self._log(f"  Unexpected download error: {e}", "error")
            pm.error = str(e)

    def _nexus_key(self) -> str:
        import config as cfg
        from scraper import nexus
        return nexus.load_api_key(cfg.load().get("nexus_api_key", ""))

    @staticmethod
    def _nexus_mod_id(mod) -> "int | None":
        m = re.search(r"/mods/(\d+)", mod.url or "")
        return int(m.group(1)) if m else None

    def _nexus_chosen(self, pm: PipelineMod, mod_id: int, key: str) -> list:
        """The Nexus files to fetch for a mod, asked for once per run."""
        from scraper import nexus
        mod = pm.build_mod
        with self._nexus_lock:
            hit = self._nexus_chosen_cache.get(mod.file_id)
        if hit is not None:
            return hit
        chosen = nexus.pick_files(nexus.list_files(mod.game, mod_id, key),
                                  mod.directives.download_only,
                                  mod.directives.download_ignore)
        with self._nexus_lock:
            self._nexus_chosen_cache[mod.file_id] = chosen
        return chosen

    # Free Nexus accounts ---------------------------------------------------
    #
    # Nexus hands a free account a download link only when its "Mod manager
    # download" button is pressed, so each Nexus mod needs a click. They are
    # collected in a queue that runs AHEAD of the downloads: the pages open one
    # after another as each click lands, and the downloads then run unattended.

    def _start_click_queue(self, pending: list) -> None:
        targets = [pm for pm in pending
                   if pm.build_mod.source_host == "nexus"
                   and not self._cached_for(pm, self._mod_dir(pm))]
        if not targets:
            return
        with self._nexus_lock:
            self._nxm_pending_mods = {pm.build_mod.file_id for pm in targets}
        self._nxm_queue_done.clear()
        threading.Thread(target=self._run_click_queue, args=(targets,),
                         name="nexus-clicks", daemon=True).start()

    def _run_click_queue(self, targets: list) -> None:
        from scraper import nexus
        try:
            key = self._nexus_key()
            if not key:
                return
            check = nexus.validate(key)
            # Premium needs no clicks, and a failed check leaves each download
            # to explain the problem itself.
            if not check.get("ok") or check.get("is_premium"):
                return
            skip = (ModStatus.DONE, ModStatus.SKIPPED, ModStatus.ERROR)
            with nexus.NXM.hold():
                for pm in targets:
                    mod = pm.build_mod
                    try:
                        if self._stop_event.is_set():
                            return
                        mod_id = self._nexus_mod_id(mod)
                        if mod_id is None or pm.status in skip:
                            continue
                        try:
                            chosen = self._nexus_chosen(pm, mod_id, key)
                        except Exception:
                            continue  # the download reports the real problem
                        for f in chosen:
                            link = self._await_nxm_click(pm, mod, mod_id, f["file_id"],
                                                         ahead=True)
                            if link is None:
                                if not self._stop_event.is_set():
                                    self._log(
                                        "Nexus: no click came in time, so the rest will "
                                        "ask when their turn comes.", "warning")
                                return
                            with self._nexus_lock:
                                self._nxm_links[(nexus.GAME_DOMAIN.get(mod.game, "kotor"),
                                                 mod_id, f["file_id"])] = link
                    finally:
                        with self._nexus_lock:
                            self._nxm_pending_mods.discard(mod.file_id)
        except Exception as e:
            self._log(f"Nexus click queue stopped: {e}", "warning")
        finally:
            with self._nexus_lock:
                self._nxm_pending_mods.clear()
            self._nxm_queue_done.set()

    def _link_for(self, pm: PipelineMod, mod, mod_id: int, file_id: int):
        """A Nexus link for this file: one clicked ahead, else wait for the
        queue to reach it, else ask now. None if stopped or not clicked."""
        from scraper import nexus
        k = (nexus.GAME_DOMAIN.get(mod.game, "kotor"), mod_id, file_id)
        announced = False
        while not self._stop_event.is_set():
            with self._nexus_lock:
                link = self._nxm_links.pop(k, None)
                queued = (not self._nxm_queue_done.is_set()
                          and mod.file_id in self._nxm_pending_mods)
            if link is not None:
                if announced:
                    # Clear "Waiting for your click": the download starts now.
                    self._set_status(pm, ModStatus.DOWNLOADING)
                return link
            if not queued:
                break
            if not announced:
                self._set_status(pm, ModStatus.DOWNLOADING, "Waiting for your click on Nexus")
                announced = True
            self._stop_event.wait(0.5)
        if self._stop_event.is_set():
            return None
        return self._await_nxm_click(pm, mod, mod_id, file_id)

    def _await_nxm_click(self, pm: PipelineMod, mod, mod_id: int, file_id: int,
                         ahead: bool = False, expired: bool = False):
        """Open the mod's Nexus page and wait for the player to click "Mod manager
        download", which hands the app an nxm:// link. `ahead` means the click
        is being collected before this mod's download has started."""
        import webbrowser
        from scraper import nexus
        url = nexus.nxm_page_url(mod.game, mod_id, file_id)
        if ahead:
            self._log(
                f"Nexus: a page for \"{mod.name}\" is opening in your browser. "
                f"Click \"Mod manager download\" there; it downloads by itself later.",
                "warning")
        else:
            self._log(
                f"  Nexus free account: a page for \"{mod.name}\" is opening in your "
                f"browser. Click \"Mod manager download\" there and this will carry on "
                f"by itself.", "warning")
        self._log(f"  {url}", "muted")

        def open_page() -> None:
            try:
                webbrowser.open(url)
            except Exception:
                pass

        # The badge would otherwise read "Downloading" for up to 15 minutes.
        if not ahead or pm.status == ModStatus.PENDING:
            self._set_status(pm, ModStatus.DOWNLOADING,
                             "Nexus link expired, click again" if expired
                             else "Waiting for your click on Nexus")
        # The page opens only once the app holds nxm:// links, so the click works.
        link = nexus.NXM.wait(nexus.GAME_DOMAIN.get(mod.game, "kotor"), mod_id, file_id,
                              timeout=_NXM_CLICK_TIMEOUT, stop_event=self._stop_event,
                              on_waiting=open_page)
        if ahead:
            # Not downloading yet: put the badge back.
            if mod.file_id not in self._dl_started and pm.status == ModStatus.DOWNLOADING:
                self._set_status(pm, ModStatus.PENDING)
        elif link is not None:
            self._set_status(pm, ModStatus.DOWNLOADING)
        return link

    def _download_from_nexus(self, pm: PipelineMod, dest_dir: Path, progress,
                             keep_names, ignore_names) -> "list[Path] | None":
        """Download a Nexus Mods file. Returns None after flagging the mod failed."""
        import urllib.error
        import config as cfg
        from scraper import nexus

        mod = pm.build_mod

        def fail(msg: str) -> None:
            pm.error = msg
            self._set_status(pm, ModStatus.ERROR, msg)
            self._log(f"  {msg}", "error")
            return None

        mod_id = self._nexus_mod_id(mod)
        if mod_id is None:
            return fail(f"Could not find this mod's Nexus page ({mod.url or 'no link'}).")
        key = self._nexus_key()
        if not key:
            return fail("This mod is on Nexus Mods. Add your Nexus API key in "
                        "Settings so the app can download it.")
        try:
            chosen = self._nexus_chosen(pm, mod_id, key)
            if not chosen:
                return fail("Nexus lists no downloadable file for this mod.")
            archives = []
            for f in chosen:
                self._log(f"  Nexus: {f.get('file_name') or f.get('name')}")
                args = dict(progress_callback=progress, cancel_event=self._stop_event,
                            pause_event=self._pause_event)
                try:
                    archives.append(nexus.download_file(
                        mod.game, mod_id, f["file_id"], dest_dir, key, **args))
                except nexus.NexusAuthError as e:
                    if not e.free_account:
                        raise
                    timed_out = ("Waiting for the Nexus download button timed out "
                                 "(or the install was stopped). Press Install to try again.")
                    link = self._link_for(pm, mod, mod_id, f["file_id"])
                    if link is None:
                        return fail(timed_out)
                    try:
                        archives.append(nexus.download_file(
                            mod.game, mod_id, f["file_id"], dest_dir, key,
                            nxm_key=link.key, nxm_expires=link.expires, **args))
                    except nexus.NexusAuthError as e2:
                        if not e2.free_account:
                            raise
                        # A link clicked ahead can expire before its turn.
                        self._log("  That Nexus link expired before its turn, so "
                                  "asking again.", "warning")
                        link = self._await_nxm_click(pm, mod, mod_id, f["file_id"],
                                                     expired=True)
                        if link is None:
                            return fail(timed_out)
                        archives.append(nexus.download_file(
                            mod.game, mod_id, f["file_id"], dest_dir, key,
                            nxm_key=link.key, nxm_expires=link.expires, **args))
            return archives
        except nexus.NexusAuthError as e:
            return fail(str(e))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return fail("Nexus rejected your API key. Check it in Settings.")
            return fail(f"Nexus returned an error (HTTP {e.code}).")
        except (nexus.NexusDownloadError, urllib.error.URLError, OSError) as e:
            return fail(f"Nexus download failed: {e}")

    # Cache bookkeeping for downloaded archives -------------------------------

    _CACHE_MANIFEST = ".downloads.json"

    def _write_cache_manifest(self, dest_dir: Path, archives: list[Path]) -> None:
        """Record which files make up a completed download, in page order."""
        try:
            (dest_dir / self._CACHE_MANIFEST).write_text(
                json.dumps({"files": [a.name for a in archives]}),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _cached_archives(self, dest_dir: Path) -> list[Path]:
        """
        Return the cached archives for a mod, or [] when the cache must not be
        trusted (missing/partial files) so the download runs again.

        With a manifest the exact recorded set is required, in page order - a
        multi-file download that failed halfway is re-fetched instead of being
        silently installed incomplete. Without a manifest (downloads from
        older versions) fall back to "every complete file in the folder".
        """
        manifest = dest_dir / self._CACHE_MANIFEST
        if manifest.exists():
            try:
                names = json.loads(manifest.read_text(encoding="utf-8")).get("files", [])
            except (OSError, ValueError):
                names = []
            files = [dest_dir / n for n in names]
            if files and all(f.is_file() and f.stat().st_size > 0 for f in files):
                return files
            return []
        # Legacy cache (no manifest). Only files count as archives: a big
        # extracted folder can have a non-zero st_size on NTFS, so a size
        # check alone would treat it as an archive and later fail with
        # Permission denied.
        return sorted(
            [f for f in dest_dir.iterdir()
             if f.is_file() and not f.name.startswith(".")
             and not f.name.endswith(".part") and f.stat().st_size > 0],
            key=lambda f: f.name,
        )

    def _filter_cached(self, cached: list[Path], mod: BuildMod) -> list[Path]:
        """Apply the guide's keep/ignore download filters to cached archives."""
        from scraper.deadlystream import (download_name_excluded,
                                          select_keep_matches)
        dirs = mod.directives
        names = [c.name for c in cached]
        keep = set(select_keep_matches(names, dirs.download_only) or names)
        out = []
        for c in cached:
            if c.name not in keep:
                self._log(f"  Cached {c.name} skipped (build guide keeps "
                          f"{', '.join(dirs.download_only)})", "muted")
                continue
            if dirs.download_ignore and download_name_excluded(
                    c.name, dirs.download_ignore):
                self._log(f"  Cached {c.name} skipped (build guide says not "
                          f"to download it)", "muted")
                continue
            out.append(c)
        return out or cached

    def _migrate_encoded_cache_names(self, dest_dir: Path) -> None:
        """
        Rename percent-encoded leftovers from older versions (e.g.
        'HR%20Menu%20Patch.zip') to their decoded names. Those names broke
        build-guide file matching and inflated nested paths; new downloads are
        decoded, but cached files kept the old names forever.
        """
        try:
            entries = list(dest_dir.iterdir())
        except OSError:
            return
        for f in entries:
            if not f.is_file() or "%" not in f.name:
                continue
            decoded = unquote(f.name)
            if decoded == f.name:
                continue
            decoded = re.sub(r'[<>:"/\\|?*]', "_", decoded)
            target = f.with_name(decoded)
            try:
                if target.exists():
                    # A decoded copy was downloaded next to the stale encoded
                    # one; keep the decoded file, drop the duplicate.
                    f.unlink()
                else:
                    f.rename(target)
            except OSError:
                continue

    def _extract_and_install(self, pm: PipelineMod) -> None:
        """Extract downloaded archives then detect and install the mod. Called sequentially."""
        if self._stop_event.is_set():
            return

        if texture_dedupe.is_cleanup_step(pm.build_mod.name):
            return          # handled by _run_texture_cleanup at the end of the run

        if pm.build_mod.directives.tool_step == "laa":
            self._tool_mods.append(pm)
            return

        # Stale caches may still hold bundled translation patches for other
        # languages (new downloads already filter them). Never install those:
        # e.g. K1CP's Russian patch would overwrite an English game's credits
        # font, and Manaan Fast Travel would be patched in five languages.
        if len(pm.archive_paths) > 1:
            from scraper.deadlystream import is_other_language_file
            keep = [a for a in pm.archive_paths
                    if not is_other_language_file(a.name, self._language)]
            if keep and len(keep) < len(pm.archive_paths):
                for a in pm.archive_paths:
                    if a not in keep:
                        self._log(
                            f"  Skipped {a.name} (translation patch for "
                            f"another language)", "muted")
                pm.archive_paths = keep

        self._order_main_before_patch(pm)

        # ---- Extract archives; treat loose mod files (e.g. .tga) as direct copies ----
        _ARCHIVE_SUFFIXES = {".zip", ".rar", ".7z", ".exe"}
        try:
            self._set_status(pm, ModStatus.EXTRACTING)
            loose_files: list[Path] = []
            for archive in pm.archive_paths:
                if archive.suffix.lower() in _ARCHIVE_SUFFIXES:
                    extracted = extract(archive)
                    pm.extracted_paths.append(extracted)
                    self._log(f"  Extracted: {extracted.name}")
                else:
                    # Loose mod file distributed without an archive wrapper (e.g. a
                    # .tga texture). Queue it for direct copy rather than extraction.
                    loose_files.append(archive)

            if loose_files:
                loose_dir = pm.archive_paths[0].parent / "_loose"
                loose_dir.mkdir(exist_ok=True)
                for f in loose_files:
                    shutil.copy2(f, loose_dir / f.name)
                pm.extracted_paths.append(loose_dir)
                self._log(f"  Loose files: {', '.join(f.name for f in loose_files)}")
        except ExtractionError as e:
            self._set_status(pm, ModStatus.ERROR, str(e))
            self._log(f"  Extraction failed: {e}", "error")
            pm.error = str(e)
            return
        except Exception as e:
            self._set_status(pm, ModStatus.ERROR, str(e)[:1500])
            self._log(f"  Extraction failed: {e}", "error")
            pm.error = str(e)
            return

        if self._stop_event.is_set():
            return

        # ---- Detect + install each extracted payload ----
        for ep in pm.extracted_paths:
            plan = detect(ep)
            pm.plans.append(plan)

        # Honour build-guide nuance: re-order so a "run the patch first" mod
        # applies its patcher before its loose files, and surface the steps we
        # can't safely automate (multi-run, required external patches).
        dirs = pm.build_mod.directives

        # A mod whose prerequisite is genuinely absent installs wrongly rather
        # than failing loudly (patches land on files that aren't there, options
        # get picked for a mod that isn't installed), so hold it back instead.
        unmet = self._unmet_requirements(pm)
        if unmet:
            self._set_status(pm, ModStatus.SKIPPED)
            self._log(
                f"  Held back '{pm.build_mod.name}': it needs "
                f"{', '.join(unmet)} installed first, and that is not part of "
                f"this run. Install the prerequisite, then run this mod again.",
                "warning")
            return

        # Edits of the game's program file run together at the end, in order.
        if dirs.tool_step:
            self._tool_mods.append(pm)
            self._set_status(pm, ModStatus.READY, "Done with the program file patches")
            return

        # Some steps genuinely cannot be automated (interactive .bat files, a
        # patcher that would break a Steam executable). Say so rather than
        # doing something destructive.
        if getattr(dirs, "manual_only", False):
            reason = getattr(dirs, "manual_reason", "") or "This mod needs manual steps."
            self._log(f"  '{pm.build_mod.name}' needs a manual step: {reason}", "warning")
            self._manual_left.append((pm.build_mod.name, reason))
            self._flag_manual(pm, ManualInstallRequired(
                pm.plans[0].mod_root if pm.plans else pm.extracted_paths[0],
                reason))
            return

        self._apply_build_guide_order(pm, dirs)
        self._log_build_guide_notes(pm, dirs)
        self._apply_pre_delete(pm, dirs)
        self._apply_pre_patch_delete(pm, dirs)

        self._set_status(pm, ModStatus.INSTALLING)
        total = len(pm.plans)
        any_succeeded = False
        manual: Optional[ManualInstallRequired] = None
        try:
            for i, plan in enumerate(pm.plans, 1):
                if total > 1:
                    self._log(f"  Component {i}/{total}: {plan.method.name}", "muted")
                else:
                    self._log(f"  Method: {plan.method.name}", "muted")
                self._install_progress(pm, (i - 1) / total, f"{plan.method.name} ({i}/{total})")
                pre = self._pre_install_snapshot(plan)
                try:
                    self._install_one(pm, plan)
                    any_succeeded = True
                except PatcherError as e:
                    # A few mods are documented as reporting an error that is
                    # harmless (High Quality Blasters warns you to expect
                    # exactly one). Only those are allowed through; every other
                    # patcher failure still fails the mod.
                    if not getattr(dirs, "tolerate_patcher_errors", False):
                        raise
                    self._log(
                        f"  The installer reported an error, which the build "
                        f"guide says to expect for this mod: {str(e)[:200]}",
                        "muted")
                    any_succeeded = True
                except ManualInstallRequired as m:
                    if any_succeeded:
                        # Core mod already installed. This is a supplementary component
                        # (e.g. an optional language pack) - log a note but don't block.
                        name = plan.mod_root.name if plan.mod_root else f"component {i}"
                        self._log(
                            f"  Optional component '{name}' needs a manual step "
                            f"- skip it if it is a language pack or variant you do not need.",
                            "muted",
                        )
                    else:
                        manual = m
                    continue
                self._apply_rename_after(dirs)
                self._apply_post_delete(dirs)
                self._record_install(pm, plan, pre)
                self._install_progress(pm, i / total, "Done")

            if manual is not None:
                self._flag_manual(pm, manual)
            else:
                self._set_status(pm, ModStatus.DONE)
                note = f" via {pm.strategy_used}" if pm.strategy_used else ""
                self._log(f"  Installed.{note}", "success")
        except InstallError as e:
            self._set_status(pm, ModStatus.ERROR, str(e)[:1500])
            self._log(f"  Install error: {e}", "error")
            pm.error = str(e)
        except PatcherError as e:
            self._set_status(pm, ModStatus.ERROR, str(e)[:1500])
            self._log(f"  Patcher error: {e}", "error")
            pm.error = str(e)
        except Exception as e:
            self._set_status(pm, ModStatus.ERROR, str(e)[:1500])
            self._log(f"  Unexpected install error: {e}", "error")
            pm.error = str(e)

    def _flag_manual(self, pm: PipelineMod, manual: ManualInstallRequired) -> None:
        """Mark a mod as needing a manual install and hand the UI the details."""
        mod = pm.build_mod
        folder = str(manual.mod_root) if manual.mod_root else ""
        self._set_status(pm, ModStatus.MANUAL, folder)
        self._log(
            f"  This mod can't be installed automatically - it needs a few manual "
            f"steps. Its files are ready in: {folder}", "warning")
        if manual.readme:
            self._log("  Follow the mod's own instructions (readme shown in the app).", "muted")
        if self._on_manual:
            self._on_manual(mod.file_id, mod.name, folder, manual.readme or "")

    def _install_one(self, pm: PipelineMod, plan: InstallPlan) -> None:
        game_path = self._game_path
        mod = pm.build_mod
        # Actionable directives parsed from the build guide (which option to
        # pick, which files to copy, etc.). See installer/build_directives.py.
        dirs = mod.directives

        def log_cb(msg: str) -> None:
            self._log(f"    {msg}", "muted")

        method = plan.method

        # ---- HoloPatcher: fully headless ----
        if method == InstallMethod.HOLOPATCHER:
            exe = plan.holopatcher_exe
            tslpatchdata = exe.parent / "tslpatchdata" if exe else None
            if not tslpatchdata or not tslpatchdata.exists():
                if exe:
                    for candidate in rglob_ci(exe.parent, "tslpatchdata"):
                        if candidate.is_dir():
                            tslpatchdata = candidate
                            break
            if not exe or not tslpatchdata:
                raise PatcherError("Cannot find HoloPatcher.exe or tslpatchdata/")

            ns_index = 0
            if plan.namespaces:
                names = [f"{ns.name} {ns.description}" for ns in plan.namespaces]
                matched = match_option_index(names, dirs, mod.option_hint)
                if matched is not None:
                    ns_index = matched
                    self._log(
                        f"    Namespace: {plan.namespaces[ns_index].name} "
                        f"(matched build-guide instructions)", "muted")
                else:
                    self._log(
                        f"    Namespace: {plan.namespaces[ns_index].name} "
                        f"(default of {len(plan.namespaces)})", "muted")
            # "Install the main option, then re-run for X": when the guide asks
            # for a second run and matched a non-default option, that option is
            # the SECOND step - running it alone fails (e.g. Yavin Station
            # Hangar's visible-forcefield add-on needs the main install first).
            indices = [ns_index]
            if dirs.multi_run and ns_index != 0:
                indices = [0, ns_index]
                self._log(
                    "    Build guide: installing the main option first, then "
                    f"re-running for '{plan.namespaces[ns_index].name}'.", "muted")
            for idx in indices:
                run_holopatcher(exe, game_path, tslpatchdata, idx, log_cb,
                                stop_event=self._stop_event)
            pm.strategy_used = "holopatcher"
            self._apply_compat_patches(pm, plan)

        # ---- TSLPatcher: strategy cascade (headless first) ----
        elif method == InstallMethod.TSLPATCHER:
            def on_waiting() -> None:
                self._set_status(pm, ModStatus.WAITING_PATCHER)
                self._log(
                    f"  [TSLPatcher] Manual step for: {mod.name}\n"
                    f"    Game path is on your clipboard - paste it, click Install, close the window.",
                    "warning"
                )

            multi_opts = dirs.multi_run_options
            if not multi_opts and dirs.multi_run and dirs.namespace_preferences:
                # "Re-run the installer for the X option": main install first,
                # then the named option.
                multi_opts = ["", dirs.namespace_preferences[0]]
            if multi_opts:
                # Run the patcher once per named option in the order the build
                # guide specifies (e.g. TSLRCM Tweak Pack's 6 separate options).
                strategies: list[str] = []
                for i, opt in enumerate(multi_opts, 1):
                    label = opt if opt else "main (default)"
                    self._log(
                        f"    Run {i}/{len(multi_opts)}: {label}", "muted")
                    run_dirs = copy.copy(dirs)
                    # Empty string = install the default/main option; non-empty = named option.
                    if opt:
                        run_dirs.namespace_preferences = [opt]
                        run_dirs.prefer_compatible = False
                    else:
                        run_dirs.namespace_preferences = []
                        run_dirs.prefer_compatible = False
                    run_dirs.multi_run = False
                    run_dirs.multi_run_options = []
                    result = run_tslpatcher_cascade(
                        mod_root=plan.mod_root,
                        exe=plan.tslpatcher_exe,
                        game_dir=game_path,
                        option_hint=opt,
                        directives=run_dirs,
                        cb=log_cb,
                        on_waiting=on_waiting,
                        allow_manual=not self._auto_unattended,
                    )
                    strategies.append(result.strategy)
                    if pm.status == ModStatus.WAITING_PATCHER:
                        self._set_status(pm, ModStatus.INSTALLING)
                pm.strategy_used = f"{strategies[0]}x{len(strategies)}"
            else:
                result = run_tslpatcher_cascade(
                    mod_root=plan.mod_root,
                    exe=plan.tslpatcher_exe,
                    game_dir=game_path,
                    option_hint=mod.option_hint,
                    directives=dirs,
                    cb=log_cb,
                    on_waiting=on_waiting,
                    allow_manual=not self._auto_unattended,
                )
                pm.strategy_used = result.strategy
                if pm.status == ModStatus.WAITING_PATCHER:
                    self._set_status(pm, ModStatus.INSTALLING)

            self._apply_compat_patches(pm, plan)

        # ---- TLK replacement / multi-variant ----
        elif method in (InstallMethod.TLK_REPLACE, InstallMethod.MULTI_VARIANT):
            variants = plan.tlk_variants
            chosen_label, chosen_path = variants[0]
            if len(variants) > 1:
                matched = match_option_index(
                    [lbl for lbl, _ in variants], dirs, mod.option_hint)
                if matched is not None:
                    chosen_label, chosen_path = variants[matched]
            self._log(f"    TLK variant: {chosen_label}", "muted")

            def tlk_chooser(_variants):
                return (chosen_label, chosen_path)

            install(plan, game_path, log_cb, tlk_variant_chooser=tlk_chooser)
            pm.strategy_used = "tlk_copy"

        # ---- Override / Direct copy / Multiple ----
        elif method in (InstallMethod.OVERRIDE_COPY, InstallMethod.DIRECT_COPY, InstallMethod.MULTIPLE):
            self._apply_file_selection(plan, dirs)
            self._apply_renames(plan, dirs)
            skipped = self._apply_no_overwrite(plan, dirs, game_path)
            if skipped:
                self._log(
                    f"    Build guide: left {skipped} existing file(s) alone "
                    f"(this mod must not overwrite).", "muted")
            install(plan, game_path, log_cb)
            self._remove_dds_conflicts(plan)
            # Apply any patcher-based compat patches in subfolders (e.g. Ebon Hawk K1
            # "Compatibility Patches" folder). Loose-file subfolders are already
            # included by _collect_loose_files and don't need a separate pass.
            self._apply_compat_patches(pm, plan, patcher_only=True)
            pm.strategy_used = "file_copy"

        # ---- Standalone game-binary patcher (e.g. 3C-FD Patcher, fog fixes) ----
        elif method == InstallMethod.GAME_PATCHER:
            import subprocess
            exe = plan.tslpatcher_exe
            if not exe or not exe.exists():
                raise ManualInstallRequired(plan.mod_root, plan.readme_text)
            # Put game folder on clipboard so user can paste it when prompted.
            try:
                subprocess.run(
                    ["clip"], input=str(self._game_path),
                    text=True, check=False, capture_output=True,
                )
            except Exception:
                pass
            self._set_status(pm, ModStatus.WAITING_PATCHER)
            self._log(
                f"  [Game Patcher] Opening {exe.name} to apply game-level patches.\n"
                f"    Your game folder is on the clipboard: {self._game_path}\n"
                f"    Select your game executable in the patcher window and apply the patches.",
                "warning",
            )
            subprocess.Popen([str(exe)], cwd=str(exe.parent))
            raise ManualInstallRequired(plan.mod_root, plan.readme_text)

        # ---- Manual ----
        elif method == InstallMethod.MANUAL:
            raise ManualInstallRequired(plan.mod_root, plan.readme_text)

    @staticmethod
    def _is_patcher_plan(plan: InstallPlan) -> bool:
        return plan.method in (InstallMethod.TSLPATCHER, InstallMethod.HOLOPATCHER)

    # ------------------------------------------------------------------
    # Compat-patch subfolder handling
    # ------------------------------------------------------------------

    def _compat_folder_matches_batch(self, folder_name: str) -> str:
        """
        Returns the name of the first mod in the batch whose name/slug contains
        all significant keywords from the folder name. Used for log context only;
        compat patches are applied regardless of match result.
        """
        _SKIP = {"patch", "compat", "compatibility", "fix", "fixes", "for", "and",
                 "with", "the", "install", "mod", "option", "ver", "version"}
        tokens = [w for w in re.findall(r'[a-z0-9]{2,}', folder_name.lower())
                  if w not in _SKIP]
        if not tokens:
            return ""
        for pm_check in self._mods:
            nm = pm_check.build_mod.name.lower()
            sl = (pm_check.build_mod.slug or "").lower()
            haystack = f"{nm} {sl}"
            if all(t in haystack for t in tokens):
                return pm_check.build_mod.name
        return ""

    def _install_sub_plan(self, pm: PipelineMod, sub_plan: InstallPlan) -> None:
        """Execute a single compat-patch sub-plan without full directive processing."""
        game_path = self._game_path
        mod = pm.build_mod
        method = sub_plan.method

        def log_cb(msg: str) -> None:
            self._log(f"      {msg}", "muted")

        if method == InstallMethod.HOLOPATCHER:
            exe = sub_plan.holopatcher_exe
            tslpatchdata = exe.parent / "tslpatchdata" if exe else None
            if exe and (not tslpatchdata or not tslpatchdata.exists()):
                for candidate in rglob_ci(exe.parent, "tslpatchdata"):
                    if candidate.is_dir():
                        tslpatchdata = candidate
                        break
            if exe and tslpatchdata:
                run_holopatcher(exe, game_path, tslpatchdata, 0, log_cb,
                                stop_event=self._stop_event)

        elif method == InstallMethod.TSLPATCHER:
            def on_waiting_sub() -> None:
                self._set_status(pm, ModStatus.WAITING_PATCHER)

            run_tslpatcher_cascade(
                mod_root=sub_plan.mod_root,
                exe=sub_plan.tslpatcher_exe,
                game_dir=game_path,
                option_hint=mod.option_hint,
                directives=mod.directives,
                cb=log_cb,
                on_waiting=on_waiting_sub,
                allow_manual=not self._auto_unattended,
            )
            if pm.status == ModStatus.WAITING_PATCHER:
                self._set_status(pm, ModStatus.INSTALLING)

        elif method == InstallMethod.MULTIPLE:
            # Recursively handle each sub-plan, respecting file_except exclusions.
            dirs = pm.build_mod.directives
            for sp in sub_plan.sub_plans:
                sp_name = sp.mod_root.name if sp.mod_root else ""
                if dirs.file_except and sp_name and any(
                    e.lower().strip() in sp_name.lower() for e in dirs.file_except
                ):
                    self._log(f"      Skipped: {sp_name} (excluded by build guide)", "muted")
                    continue
                self._install_sub_plan(pm, sp)

        elif method in (InstallMethod.OVERRIDE_COPY, InstallMethod.DIRECT_COPY):
            install(sub_plan, game_path, log_cb)

    def _apply_compat_patches(self, pm: PipelineMod, plan: InstallPlan,
                              patcher_only: bool = False) -> None:
        """
        After an install, scan for compat-patch subfolders and apply them.
        In a curated build every bundled compat patch is intentional, so we apply
        all of them except those explicitly excluded by file_except.

        patcher_only: when True (used after DIRECT_COPY/OVERRIDE_COPY installs),
        only run patcher-based sub-plans - loose-file subfolders are already
        included by _collect_loose_files and do not need a second pass.
        """
        dirs = pm.build_mod.directives
        # source/src variants hold the mod's script SOURCE code (.nss), which
        # detection would otherwise happily copy into Override.
        _SKIP_NAMES = {"tslpatchdata", "backup", "__macosx", ".ds_store",
                       "data", "docs", "documentation", "readme",
                       "source", "sources", "src", "_source", "script source",
                       "script_source", "scripts source", "screenshots"}
        _PATCHER_METHODS = {InstallMethod.TSLPATCHER, InstallMethod.HOLOPATCHER,
                            InstallMethod.MULTIPLE}
        try:
            subdirs = sorted(
                [d for d in plan.mod_root.iterdir()
                 if d.is_dir() and d.name.lower() not in _SKIP_NAMES],
                key=lambda d: d.name.lower(),
            )
        except OSError:
            return

        if not subdirs:
            return

        # Locations belonging to the install that JUST ran. Many archives wrap
        # the whole mod in one top-level folder (ModName/tslpatchdata/...); that
        # wrapper is a subdir of mod_root, so without this check the sweep
        # would re-detect it and run the same patcher a second time, writing
        # duplicate 2DA rows and TLK entries into the game.
        own_patch_locations = [
            p for p in (
                plan.tslpatcher_ini.parent if plan.tslpatcher_ini else None,
                plan.tslpatcher_exe,
                plan.holopatcher_exe,
            ) if p is not None
        ]
        if self._is_patcher_plan(plan):
            # The cascade resolves tslpatchdata by first rglob match; mirror
            # that so the folder it ran from is recognised as already done.
            from installer.detector import _find_dir
            own_td = _find_dir(plan.mod_root, "tslpatchdata")
            if own_td:
                own_patch_locations.append(own_td)

        def _is_own_install(d: Path) -> bool:
            for loc in own_patch_locations:
                try:
                    if loc.resolve().is_relative_to(d.resolve()):
                        return True
                except OSError:
                    continue
            return False

        applied = 0
        for sub_dir in subdirs:
            folder_name = sub_dir.name
            folder_low = folder_name.lower()

            if _is_own_install(sub_dir):
                continue

            # Per-language variant folders (Deutsch/, Français/, ...) are not
            # compat patches; only the player's language belongs in the game.
            from scraper.deadlystream import is_other_language_file
            if is_other_language_file(folder_name, self._language):
                self._log(
                    f"    Compat skipped: {folder_name} (another language)", "muted")
                continue

            # Respect explicit build-guide exclusions.
            if dirs.file_except and any(
                e.lower().strip() == folder_low or e.lower().strip() in folder_low
                for e in dirs.file_except
            ):
                self._log(
                    f"    Compat skipped: {folder_name} (excluded by build guide)", "muted")
                continue

            sub_plan = detect(sub_dir)
            if sub_plan.method in (InstallMethod.MANUAL, InstallMethod.GAME_PATCHER):
                continue
            if patcher_only and sub_plan.method not in _PATCHER_METHODS:
                continue

            matched = self._compat_folder_matches_batch(folder_name)
            if matched:
                self._log(f"    Compat: {folder_name} (for {matched})", "muted")
            else:
                self._log(f"    Compat: {folder_name}", "muted")

            try:
                self._install_sub_plan(pm, sub_plan)
                applied += 1
            except ManualInstallRequired:
                self._log(f"    Compat {folder_name}: needs manual step", "warning")
            except (PatcherError, InstallError) as e:
                self._log(f"    Compat {folder_name} failed: {e}", "warning")
            except Exception as e:
                self._log(f"    Compat {folder_name} failed: {e}", "warning")
            if self._stop_event.is_set():
                break

        if applied:
            self._log(f"    Applied {applied} compat patch(es).", "muted")

    # ------------------------------------------------------------------
    # Post-install cleanup
    # ------------------------------------------------------------------

    def _remove_dds_conflicts(self, plan: InstallPlan) -> None:
        """
        Remove .tpc / .tga files from Override when a .dds with the same stem was
        just installed. Some engine versions prefer .tpc over .dds, so the old
        files must be gone for HD textures to take effect.
        """
        override_dir = resolve_ci(self._game_path, "Override")
        if not override_dir.exists():
            return
        dds_stems = {
            fm.source.stem.lower()
            for fm in plan.file_mappings
            if fm.source.suffix.lower() == ".dds"
            and fm.dest_relative.lower().startswith("override/")
        }
        if not dds_stems:
            return
        removed: list[str] = []
        for stem in dds_stems:
            for ext in (".tpc", ".tga"):
                conflict = override_dir / f"{stem}{ext}"
                if not conflict.exists():
                    # Case-insensitive fallback for mounted game dirs on Linux/Mac.
                    for f in override_dir.iterdir():
                        if f.name.lower() == f"{stem}{ext}":
                            conflict = f
                            break
                if conflict.exists():
                    try:
                        conflict.unlink()
                        removed.append(conflict.name)
                    except OSError as e:
                        self._log(f"    Could not remove {conflict.name}: {e}", "warning")
        if removed:
            n = len(removed)
            listed = ", ".join(removed[:5])
            extra = f" +{n - 5} more" if n > 5 else ""
            self._log(f"    Removed {n} outdated file(s): {listed}{extra}", "muted")

    @staticmethod
    def _safe_delete_target(game_path: Path, subdir: str, fn: str) -> "Path | None":
        """Return a validated path for a build-guide delete directive, or None if unsafe.

        Rejects filenames containing path separators or '..' components so a
        crafted build guide cannot traverse outside the intended subdirectory.
        """
        p = Path(fn)
        # Reject multi-component paths and any '..' traversal attempt.
        if len(p.parts) != 1 or ".." in p.parts:
            return None
        # Also reject embedded separators that Path() might not split on Windows.
        if "/" in fn or "\\" in fn:
            return None
        target = resolve_ci(game_path, f"{subdir}/{fn}")
        allowed_root = resolve_ci(game_path, subdir).resolve()
        try:
            if not target.resolve().is_relative_to(allowed_root):
                return None
        except (OSError, ValueError):
            return None
        return target

    def _apply_post_delete(self, dirs) -> None:
        """Delete files from Override per explicit build-guide post-install directives."""
        filenames = getattr(dirs, "post_install_delete", [])
        if not filenames:
            return
        for fn in filenames:
            for subdir in ("Override", "Modules"):
                target = self._safe_delete_target(self._game_path, subdir, fn)
                if target and target.exists():
                    try:
                        target.unlink()
                        self._log(
                            f"    Cleaned up {subdir}/{fn} (per build guide)", "muted")
                    except OSError as e:
                        self._log(f"    Could not clean up {fn}: {e}", "warning")

    def _apply_pre_delete(self, pm: PipelineMod, dirs) -> None:
        """Delete stale files from Override/Modules before installing this mod.

        Build guides say "delete X before install" to clear old texture versions
        that would otherwise shadow the replacement from this mod.
        """
        filenames = getattr(dirs, "pre_install_delete", [])
        if not filenames:
            return
        deleted: list[str] = []
        for fn in filenames:
            for subdir in ("Override", "Modules"):
                target = self._safe_delete_target(self._game_path, subdir, fn)
                if target is None:
                    continue
                if not target.exists():
                    parent = resolve_ci(self._game_path, subdir)
                    if parent.is_dir():
                        for f in parent.iterdir():
                            if f.name.lower() == fn.lower():
                                target = f
                                break
                if target.exists():
                    try:
                        target.unlink()
                        deleted.append(f"{subdir}/{fn}")
                    except OSError as e:
                        self._log(f"    Could not remove {subdir}/{fn}: {e}", "warning")
        if deleted:
            n = len(deleted)
            listed = ", ".join(deleted[:5])
            extra = f" +{n - 5} more" if n > 5 else ""
            self._log(f"  Pre-install: removed {n} outdated file(s): {listed}{extra}", "muted")

    def _resolve_skip_constraints(self) -> None:
        """Mark mods as SKIPPED when a mutex alternative is in the same batch.

        Handles build-guide notes like 'skip if using 3C-FD Patcher' so the
        pipeline never installs two mutually-exclusive mods at once.
        """
        batch = [
            (pm.build_mod.name.lower(), (pm.build_mod.slug or "").lower())
            for pm in self._mods
        ]
        for pm in self._mods:
            if pm.status != ModStatus.PENDING:
                continue
            skip_if = getattr(pm.build_mod.directives, "skip_if", [])
            for skip_name in skip_if:
                sn = skip_name.lower().strip()
                if len(sn) < 3:
                    continue
                for name, slug in batch:
                    if name == pm.build_mod.name.lower():
                        continue
                    if sn in name or sn in slug:
                        self._set_status(pm, ModStatus.SKIPPED)
                        self._log(
                            f"  [{pm.build_mod.name}] Skipped - not needed "
                            f"when '{skip_name}' is also being installed.",
                            "muted",
                        )
                        break
                if pm.status == ModStatus.SKIPPED:
                    break

    def _capture_baseline_once(self) -> None:
        """
        Snapshot the clean game before the first mod goes in.

        Patcher mods rewrite dialog.tlk and the .2da tables in place, so once a
        build is installed there is no way to work out what the game looked like
        beforehand. Without this there is no undo for a whole build - which is
        exactly the hole that left a half-removed install with no way back.
        """
        if not self._game_key:
            return
        try:
            from installer import mod_manager
            if mod_manager.has_baseline(self._game_key):
                return
            result = mod_manager.capture_baseline(self._game_key, self._game_path)
            if result.get("ok"):
                self._log(
                    f"  Saved a snapshot of your clean game ({result['override']} "
                    f"Override file(s), {result['modules']} module(s)) so this "
                    f"build can be undone later.", "muted")
        except Exception as e:
            # Never block an install over the snapshot.
            self._log(f"  (could not snapshot the clean game: {e})", "muted")

    def _apply_layer_order(self) -> None:
        """
        Sort the batch into the build guide's install layers.

        The guide's own numbering already runs roughly in layer order, but a
        player can select a subset in any order, and a few mods (the duplicate
        texture cleanup, the widescreen work) must run at a fixed point
        regardless of where they sit in the list. Sorting by layer and then by
        the mod's position on the page preserves the guide's sequence within a
        layer while pinning those to the right place.
        """
        if not self._mods:
            return
        before = [pm.build_mod.file_id for pm in self._mods]

        def key(pm: PipelineMod):
            mod = pm.build_mod
            layer = getattr(mod.directives, "layer", 0) or 0
            page_pos = getattr(mod, "guide_index", 0) or mod.install_order
            return (layer, page_pos)

        self._mods.sort(key=key)
        if [pm.build_mod.file_id for pm in self._mods] != before:
            self._log("Ordered the mods into the build guide's install layers.", "muted")

    def _installed_ids(self) -> set:
        """Ids of mods already installed - this run plus the recorded library."""
        done = {pm.build_mod.file_id for pm in self._mods
                if pm.status == ModStatus.DONE}
        done |= {f"guide:{getattr(pm.build_mod, 'guide_index', 0)}"
                 for pm in self._mods if pm.status == ModStatus.DONE}
        for scope in {self._game_key, self._game_type} - {""}:
            try:
                from installer.mod_manager import load_manifest
                for im in load_manifest(scope).mods:
                    if im.source_ref:
                        done.add(str(im.source_ref))
            except Exception:
                pass
        return done

    def _unmet_requirements(self, pm: PipelineMod) -> list[str]:
        """
        Which of this mod's prerequisites are not installed and not coming up
        later in this run. A prerequisite still queued is fine - layer ordering
        puts it first - so only genuinely absent ones are reported.
        """
        required = getattr(pm.build_mod.directives, "requires", [])
        if not required:
            return []
        available = self._installed_ids()
        for other in self._mods:
            if other is pm or other.status == ModStatus.SKIPPED:
                continue
            available.add(other.build_mod.file_id)
            gi = getattr(other.build_mod, "guide_index", 0)
            if gi:
                available.add(f"guide:{gi}")
        return [r for r in required if r not in available]

    # Filename markers for the secondary/patch half of a multi-archive mod.
    _PATCH_NAME_TOKENS = ("patch", "compatch", "compat", "hotfix", "fix",
                          "addon", "add-on", "update", "replacement")

    @classmethod
    def _looks_like_patch(cls, name: str) -> bool:
        low = re.sub(r"[^a-z0-9]+", " ", name.lower())
        return any(f" {t} " in f" {low} " or low.endswith(f" {t}")
                   for t in cls._PATCH_NAME_TOKENS)

    def _order_main_before_patch(self, pm: PipelineMod) -> None:
        """
        Within one mod, install the base archive before its patch archive.

        A submission split across several downloads (main mod + compatibility
        patch) has no reliable ordering: the download order is whatever order the
        guide happened to list the links in. Getting it backwards is not a
        cosmetic problem - JAO's saber replacement patches Juhani's .utc, so
        running it before the main mod means the file it edits does not exist yet
        and the patcher fails outright.

        Only reorders when the mod has both kinds, so a submission whose files
        are all patches (or all main) keeps its given order.
        """
        if len(pm.archive_paths) < 2:
            return
        patches = [p for p in pm.archive_paths if self._looks_like_patch(p.stem)]
        mains = [p for p in pm.archive_paths if p not in patches]
        if not patches or not mains:
            return
        reordered = [*mains, *patches]
        if reordered == pm.archive_paths:
            return
        pm.archive_paths = reordered
        self._log(
            f"  Installing the main download before its patch: "
            f"{', '.join(p.name for p in reordered)}", "muted")

    def _apply_pre_patch_delete(self, pm: PipelineMod, dirs) -> None:
        """
        Remove files from the mod's OWN tslpatchdata before its patcher runs.

        High Quality Blasters ships keblastore.utm, which conflicts with this
        build; the guide says to delete it from the mod folder before running
        the installer, not from the game afterwards.
        """
        names = getattr(dirs, "pre_patch_delete", [])
        if not names or not pm.extracted_paths:
            return
        removed: list[str] = []
        for root in pm.extracted_paths:
            for name in names:
                if "/" in name or "\\" in name or ".." in name:
                    continue
                for found in rglob_ci(root, name):
                    try:
                        found.unlink()
                        removed.append(found.name)
                    except OSError as e:
                        self._log(f"    Could not remove {found.name}: {e}", "warning")
        if removed:
            self._log(f"  Build guide: removed {', '.join(removed)} from the mod "
                      f"before running its installer.", "muted")

    def _apply_rename_after(self, dirs) -> None:
        """
        Rename files inside Override after a mod installs.

        Distinct from post_install_delete: the guide sometimes wants a file kept
        under a different name (HQ Blasters' w_ionrfl_04 -> w_ionrfl_004).
        Deleting it instead, which is how the free-text parser used to read the
        sentence, loses the model entirely.
        """
        pairs = getattr(dirs, "rename_after", [])
        if not pairs:
            return
        for src_name, dst_name in pairs:
            for subdir in ("Override", "Modules"):
                src = self._safe_delete_target(self._game_path, subdir, src_name)
                dst = self._safe_delete_target(self._game_path, subdir, dst_name)
                if src is None or dst is None or not src.exists():
                    continue
                try:
                    from installer.fs_retry import with_lock_retry
                    import os as _os
                    with_lock_retry(lambda: _os.replace(src, dst))
                    self._log(f"    Renamed {subdir}/{src_name} -> {dst_name} "
                              f"(per build guide)", "muted")
                except OSError as e:
                    self._log(f"    Could not rename {src_name}: {e}", "warning")

    @staticmethod
    def _apply_no_overwrite(plan: InstallPlan, dirs, game_path: Path) -> int:
        """
        Drop mappings whose destination already exists.

        "Pretty Good! Icons" is explicit that you must not overwrite when
        prompted - its icons are meant to fill gaps, not replace icons an
        earlier mod supplied. Returns how many files were skipped.
        """
        if not getattr(dirs, "no_overwrite", False):
            return 0
        skipped = 0
        for p in [plan, *plan.sub_plans]:
            keep = []
            for fm in p.file_mappings:
                if resolve_ci(game_path, fm.dest_relative).exists():
                    skipped += 1
                    continue
                keep.append(fm)
            p.file_mappings = keep
        return skipped

    def _check_dependencies(self) -> None:
        """Warn when mods that need a community patch don't have it in the batch."""
        pending = [pm for pm in self._mods if pm.status == ModStatus.PENDING]
        if not pending:
            return
        compat_needers = [
            pm for pm in pending
            if getattr(pm.build_mod.directives, "prefer_compatible", False)
        ]
        if not compat_needers:
            return
        haystack = " ".join(
            f"{pm.build_mod.name.lower()} {(pm.build_mod.slug or '').lower()}"
            for pm in pending
        )
        # The community patch is often installed in an earlier run and deselected
        # this time round. Count anything already recorded in the library as
        # present so we don't warn about a patch the player already has.
        # Installs are recorded under the profile scope (game_key), so check
        # that manifest first; the plain game manifest covers older recordings.
        for scope in {self._game_key, self._game_type} - {""}:
            try:
                from installer.mod_manager import load_manifest
                for im in load_manifest(scope).mods:
                    haystack += f" {im.name.lower()} {(im.source_slug or '').lower()}"
            except Exception:
                pass
        _CP_TOKENS = [
            "k1cp", "k2cp", "tslrcm", "community patch",
            "kotor-1-community-patch", "kotor-2-community-patch",
            "tsl-restored", "sith-lords-restored",
        ]
        if any(tok in haystack for tok in _CP_TOKENS):
            return
        names = ", ".join(f"'{pm.build_mod.name}'" for pm in compat_needers[:3])
        extra = f" and {len(compat_needers) - 3} more" if len(compat_needers) > 3 else ""
        self._log(
            f"Warning: {names}{extra} need a community patch (K1CP / TSLRCM / K2CP) "
            f"that isn't in the selected mods. They may not install correctly without it.",
            "warning",
        )

    def _apply_build_guide_order(self, pm: PipelineMod, dirs) -> None:
        """
        For a "the patch is run first" mod, make the patcher run before the
        loose-file copy (the opposite of our default). Applies both across this
        mod's components and within a MULTIPLE plan's sub-plans.
        """
        if not dirs.patch_first:
            return
        before = [p.method.name for p in pm.plans]
        pm.plans.sort(key=lambda p: 0 if self._is_patcher_plan(p) else 1)
        for p in pm.plans:
            if p.sub_plans:
                p.sub_plans.sort(key=lambda sp: 0 if self._is_patcher_plan(sp) else 1)
        after = [p.method.name for p in pm.plans]
        if before != after:
            self._log("  Build guide: applying the patch first, as instructed.", "muted")

    def _log_build_guide_notes(self, pm: PipelineMod, dirs) -> None:
        """
        Surface the build-guide steps we deliberately do NOT guess at - extra
        patcher runs and required external patches - so the player can finish
        them. Guessing which optional re-runs to apply could change the game
        beyond the build's baseline, so we tell the user instead.
        """
        mod = pm.build_mod
        if dirs.requires_patch:
            self._log(
                f"  Heads up for '{mod.name}': the build guide says a patch must be "
                f"installed for this mod. If it was bundled it has been applied; if it "
                f"links an external patch, install that too.", "warning")
        if dirs.multi_run:
            if dirs.multi_run_options:
                opts_list = ", ".join(f'"{o or "main (default)"}"' for o in dirs.multi_run_options)
                self._log(
                    f"  '{mod.name}': running the patcher {len(dirs.multi_run_options)} times "
                    f"in order: {opts_list}.",
                    "muted")
            else:
                self._log(
                    f"  Heads up for '{mod.name}': this mod's guide asks for the patcher to "
                    f"be run more than once (e.g. a compatibility or optional component). "
                    f"The main option was installed; re-run it for any extra options you want.",
                    "warning")
            if mod.instructions:
                self._log(f"    Guide: {mod.instructions[:400]}", "muted")
        for note in dirs.manual_notes[:3]:
            self._log(f"  Note for '{mod.name}': {note}", "warning")

    def _apply_file_selection(self, plan: InstallPlan, dirs) -> None:
        """
        Drop or keep loose files per the build guide's "only move X" / "move all
        EXCEPT Y" / "ignore the Z folder" instructions, so we don't copy files
        the guide says to leave out (which would create conflicts a later mod
        handles). Applied to the plan and any sub-plans (MULTIPLE).
        """
        from installer.build_directives import select_paths

        # "Delete X before moving to override" means the mod's OWN copy of X must
        # not be installed. Clearing X from Override beforehand does nothing on
        # its own, because the very next step copies the mod's copy straight back
        # in. So a pre-install delete also excludes that file from this mod's
        # payload; the delete still runs, to clear a copy an earlier mod left.
        pre_delete = list(getattr(dirs, "pre_install_delete", []))
        if pre_delete:
            dirs = copy.copy(dirs)
            dirs.file_except = _dedupe_keep_order(
                [*dirs.file_except, *pre_delete])

        if not dirs.file_only and not dirs.file_except:
            return
        for p in [plan, *plan.sub_plans]:
            if not p.file_mappings:
                continue
            rels = []
            for m in p.file_mappings:
                try:
                    rels.append(str(m.source.relative_to(p.mod_root)).replace("\\", "/"))
                except ValueError:
                    rels.append(m.source.name)
            kept, dropped = select_paths(rels, dirs)
            if not dropped:
                continue
            keptset = set(kept)
            p.file_mappings = [m for m, r in zip(p.file_mappings, rels) if r in keptset]
            self._log(
                f"    Build guide: copying {len(p.file_mappings)} of {len(rels)} "
                f"file(s); skipped {len(dropped)} per the install instructions.",
                "muted",
            )
            for d in dropped[:6]:
                self._log(f"      skipped: {d}", "muted")

    def _apply_renames(self, plan: InstallPlan, dirs) -> None:
        """Add copy-under-new-name mappings from the build guide's rename directives."""
        rename_copies = getattr(dirs, "rename_copies", [])
        rename_base = getattr(dirs, "rename_base_copies", "")
        if not rename_copies and not rename_base:
            return

        new_mappings: list[ModFileMapping] = []

        for src_name, dst_name in rename_copies:
            src_low = src_name.lower()
            src_stem = src_low.rsplit(".", 1)[0] if "." in src_low else src_low
            for fm in plan.file_mappings:
                fn_low = fm.source.name.lower()
                # Match exact filename or stem-only (source name lacks extension).
                if fn_low == src_low or ("." not in src_low and fn_low.startswith(src_stem + ".")):
                    parts = fm.dest_relative.rsplit("/", 1)
                    dst_rel = f"{parts[0]}/{dst_name}" if len(parts) > 1 else dst_name
                    new_mappings.append(ModFileMapping(source=fm.source, dest_relative=dst_rel))
                    self._log(f"    Rename copy: {fm.source.name} -> {dst_name}", "muted")
                    break

        if rename_base:
            for fm in plan.file_mappings:
                suffix = fm.source.suffix.lower()
                dst_name = f"{rename_base}{suffix}"
                parts = fm.dest_relative.rsplit("/", 1)
                dst_rel = f"{parts[0]}/{dst_name}" if len(parts) > 1 else dst_name
                if dst_rel != fm.dest_relative:
                    new_mappings.append(ModFileMapping(source=fm.source, dest_relative=dst_rel))
                    self._log(f"    Rename copy: {fm.source.name} -> {dst_name}", "muted")

        if new_mappings:
            plan.file_mappings.extend(new_mappings)

    # ------------------------------------------------------------------
    # Mod-manager recording
    # ------------------------------------------------------------------

    @staticmethod
    def _is_baked(plan: InstallPlan) -> bool:
        return plan.method in (InstallMethod.TSLPATCHER, InstallMethod.HOLOPATCHER)

    def _pre_install_snapshot(self, plan: InstallPlan) -> dict:
        """Capture pre-install state needed to record this plan after success."""
        if not self._record_to_library:
            return {}
        from installer import mod_manager
        if self._is_baked(plan):
            return {"baked": True, "before": mod_manager.snapshot_targets(self._game_path)}
        mappings = loose_mappings(plan)
        pre_existing = {
            rel for (rel, _src) in mappings
            if resolve_ci(self._game_path, rel).exists()
        }
        return {"baked": False, "mappings": mappings, "pre_existing": pre_existing}

    def _record_install(self, pm: PipelineMod, plan: InstallPlan, pre: dict) -> None:
        if not self._record_to_library or not pre:
            return
        from installer import mod_manager
        mod = pm.build_mod
        try:
            if pre.get("baked"):
                after = mod_manager.snapshot_targets(self._game_path)
                mod_manager.record_install(
                    self._game_key, self._game_path,
                    name=mod.name, install_method=plan.method.name,
                    source_type="build", source_ref=mod.file_id,
                    deploy_kind=mod_manager.DeployKind.BAKED.value,
                    snapshot_before=pre.get("before"), snapshot_after=after,
                    build_key=mod.build_key, option_hint=mod.option_hint,
                    readme_text=plan.readme_text, game_type=self._game_type,
                    source_slug=mod.slug, category=getattr(mod, "category", "") or "",
                    source_host=mod.source_host, source_url=mod.url,
                )
            else:
                mod_manager.record_install(
                    self._game_key, self._game_path,
                    name=mod.name, install_method=plan.method.name,
                    source_type="build", source_ref=mod.file_id,
                    deploy_kind=mod_manager.DeployKind.LOOSE.value,
                    plan_file_mappings=pre.get("mappings"),
                    pre_existing=pre.get("pre_existing"),
                    build_key=mod.build_key, option_hint=mod.option_hint,
                    readme_text=plan.readme_text, game_type=self._game_type,
                    source_slug=mod.slug, category=getattr(mod, "category", "") or "",
                    source_host=mod.source_host, source_url=mod.url,
                )
        except Exception as e:  # recording must never fail an install
            self._log(f"    (library record skipped: {e})", "muted")


def loose_mappings(plan: InstallPlan) -> list[tuple[str, "Path | None"]]:
    """
    Collect (dest_relative, source) for a loose plan, recursing into sub-plans.
    TLK plans deploy dialog.tlk at game root.
    """
    out: list[tuple[str, "Path | None"]] = []
    for fm in plan.file_mappings:
        out.append((fm.dest_relative, fm.source))
    for sub in plan.sub_plans:
        out.extend(loose_mappings(sub))
    if plan.method in (InstallMethod.TLK_REPLACE, InstallMethod.MULTI_VARIANT):
        if not any(rel.lower() == "dialog.tlk" for rel, _ in out):
            out.append(("dialog.tlk", None))
    return out
