"""
FastAPI backend for the KOTOR Mod Installer.

Wraps the existing Python pipeline (scraper / detector / patcher_strategy /
pipeline) behind a small REST API plus a WebSocket that streams live status,
log, and progress events. The Tauri + React frontend talks to this over
localhost. The same backend is frozen by PyInstaller into a sidecar exe and
spawned by the Tauri shell on launch.

Run standalone:  python -m backend.server --port 8756
"""

import argparse
import asyncio
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import config as cfg
from backend.models import (
    ActiveProfileRequest,
    AddBuildRequest,
    ConfirmAnswer,
    LoginRequest,
    NxmRequest,
    SourceSiteRequest,
    OpenDownloadRequest,
    OpenPathRequest,
    ProfileCreate,
    ProfileUpdate,
    SettingsModel,
    StartInstallRequest,
    build_mod_to_dict,
    pipeline_mod_to_dict,
)
from installer._version import __version__
from installer.config_loader import find_system_holopatcher
from installer.pipeline import ModStatus, Pipeline
from scraper.build_scraper import BUILD_GAME, BUILD_URLS, BuildMod, scrape_build
from scraper.deadlystream import AuthError, DeadlyStreamClient

BUILD_LABELS = {
    "k1_full":        "KOTOR 1 - Full Build",
    "k1_spoilerfree": "KOTOR 1 - Spoiler-Free",
    "k2_full":        "KOTOR 2 - Full Build",
    "k2_spoilerfree": "KOTOR 2 - Spoiler-Free",
}


def _screen_resolution() -> str:
    """The primary display's resolution, used to pick ONE variant when a mod
    ships separate multi-gigabyte packs per resolution."""
    if sys.platform == "win32":
        try:
            import ctypes
            user32 = ctypes.windll.user32
            try:
                user32.SetProcessDPIAware()
            except Exception:
                pass
            w, h = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
            if w and h:
                return f"{w}x{h}"
        except Exception:
            pass
    elif sys.platform.startswith("linux"):
        # Primary output first, else the first connected one.
        try:
            import re
            import subprocess
            out = subprocess.run(["xrandr", "--query"], capture_output=True,
                                 text=True, timeout=3).stdout
            modes = re.findall(r"^\S+ connected( primary)? (\d+)x(\d+)\+", out, re.M)
            modes.sort(key=lambda m: not m[0])  # primary first
            if modes:
                return f"{modes[0][1]}x{modes[0][2]}"
        except Exception:
            pass
    return "1920x1080"


def _resolution_from_name(name: str) -> tuple[int, int] | None:
    match = re.search(r"(\d{3,4})\s*[x×]\s*(\d{3,4})", name or "", re.I)
    if match:
        return int(match.group(1)), int(match.group(2))
    token = re.search(r"\b(1080p|1440p|2160p|2k|4k)\b", name or "", re.I)
    return {
        "1080p": (1920, 1080), "1440p": (2560, 1440),
        "2k": (2560, 1440), "2160p": (3840, 2160), "4k": (3840, 2160),
    }.get(token.group(1).lower()) if token else None


def _all_builds() -> list[dict]:
    """Built-in builds plus any the user has added, in one list."""
    builtin = [
        {"key": k, "label": BUILD_LABELS.get(k, k), "game": BUILD_GAME.get(k, ""),
         "custom": False, "url": BUILD_URLS[k]}
        for k in BUILD_URLS
    ]
    custom = [
        {"key": b["key"], "label": b["label"], "game": b["game"],
         "custom": True, "url": b["url"]}
        for b in cfg.get_custom_builds()
    ]
    return builtin + custom


def _resolve_build_game(build_key: str) -> str:
    """The game a build targets, for built-in or custom builds."""
    if build_key in BUILD_GAME:
        return BUILD_GAME[build_key]
    b = cfg.get_custom_build(build_key)
    return b["game"] if b else "KOTOR1"


# ---------------------------------------------------------------------------
# Event hub - bridges background-thread pipeline callbacks to WebSocket clients
# ---------------------------------------------------------------------------

class EventHub:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._queue: "asyncio.Queue[dict]" = asyncio.Queue()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._history: list[dict] = []   # last-known state replay for new clients

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    def publish(self, event: dict) -> None:
        """Thread-safe publish from any thread."""
        if event.get("type") == "log":
            # Also write it to stdout (the system journal on Linux), so a problem
            # can be read back after the window that showed it is gone.
            try:
                print(f"[activity] {event.get('message', '')}", flush=True)
            except (OSError, ValueError):
                pass
        if self._loop is None:
            return
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, event)
        except RuntimeError:
            pass

    async def run(self) -> None:
        while True:
            event = await self._queue.get()
            dead = []
            for ws in list(self._clients):
                try:
                    await ws.send_json(event)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self._clients.discard(ws)


# ---------------------------------------------------------------------------
# Application state
# ---------------------------------------------------------------------------

def _nxm_active(active: bool) -> None:
    """Tell the UI to claim (or give back) nxm:// links while a free-account
    Nexus download waits for its click."""
    state.hub.publish({"type": "nxm_wait", "active": active})


class AppState:
    def __init__(self) -> None:
        self.client = DeadlyStreamClient()
        self.hub = EventHub()
        self.pipeline: Optional[Pipeline] = None
        self.loaded_mods: dict[str, list] = {}   # build_key -> list[BuildMod]
        self.current_build: str = ""

    def game_path_for(self, build_key: str, override: Optional[str]) -> Optional[Path]:
        if override:
            return Path(override)
        conf = cfg.load()
        game = _resolve_build_game(build_key)
        key = "kotor1_path" if game == "KOTOR1" else "kotor2_path"
        p = conf.get(key, "")
        return Path(p) if p else None


state = AppState()

from scraper import nexus as _nexus
_nexus.NXM.on_active = _nxm_active

app = FastAPI(title="KOTOR Mod Installer Backend", version=__version__)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # localhost-only service; Tauri + Vite dev
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mod-manager (Library) routes
from backend.library_routes import bind_state as _bind_library, library_router  # noqa: E402

_bind_library(state)
app.include_router(library_router)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def _startup() -> None:
    state.hub.bind_loop(asyncio.get_running_loop())
    asyncio.create_task(state.hub.run())
    # Best-effort auto-login from saved credentials
    u, p = DeadlyStreamClient.load_credentials()
    if u and p:
        threading.Thread(target=_login_safe, args=(u, p), daemon=True).start()


def _login_safe(u: str, p: str) -> None:
    try:
        state.client.login(u, p)
        state.hub.publish({"type": "auth", "logged_in": True, "username": u})
        state.hub.publish({"type": "log", "message": f"Logged in as {u}.", "tag": "success"})
    except AuthError as e:
        state.hub.publish({"type": "log", "message": f"Auto-login failed: {e}", "tag": "error"})


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "version": __version__, "platform": _platform_name()}


def _platform_name() -> str:
    """'windows', 'linux' or 'macos', so the UI can word things for the OS."""
    return {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")


@app.get("/api/status")
def status() -> dict:
    holo = find_system_holopatcher()
    conf = cfg.load()
    browser_dl = conf.get("browser_download_mode", sys.platform != "win32")
    return {
        "version": __version__,
        "logged_in": state.client._logged_in,
        "browser_download_mode": browser_dl,
        "pipeline_running": bool(state.pipeline and state.pipeline.is_running),
        "shim_available": holo is not None,
        "shim_path": str(holo) if holo else None,
        "current_build": state.current_build,
    }


@app.get("/api/builds")
def builds() -> dict:
    return {"builds": _all_builds()}


@app.post("/api/builds")
def add_build(req: AddBuildRequest) -> dict:
    """Add a user-defined build from a guide URL (scraped like the built-ins)."""
    if req.game not in ("KOTOR1", "KOTOR2"):
        return JSONResponse(status_code=400, content={"ok": False, "error": "invalid_game"})
    url = req.url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return JSONResponse(status_code=400, content={"ok": False, "error": "invalid_url"})
    label = req.label.strip() or url
    build = cfg.add_custom_build(label, req.game, url)
    return {"ok": True, "build": {"key": build["key"], "label": build["label"],
                                  "game": build["game"], "custom": True, "url": build["url"]}}


@app.delete("/api/builds/{build_key}")
def delete_build(build_key: str) -> dict:
    """Remove a user-added build (built-in builds can't be removed)."""
    if build_key in BUILD_URLS:
        return JSONResponse(status_code=400, content={"ok": False, "error": "cannot_delete_builtin"})
    ok = cfg.remove_custom_build(build_key)
    state.loaded_mods.pop(build_key, None)
    return {"ok": ok}


@app.post("/api/login")
def login(req: LoginRequest) -> dict:
    try:
        state.client.login(req.username, req.password)
    except AuthError as e:
        return JSONResponse(status_code=401, content={"ok": False, "error": str(e)})
    if req.save:
        DeadlyStreamClient.save_credentials(req.username, req.password)
    state.hub.publish({"type": "auth", "logged_in": True, "username": req.username})
    return {"ok": True, "username": req.username}


@app.post("/api/logout")
def logout() -> dict:
    """Reset the in-memory DeadlyStream session (keeps saved credentials)."""
    state.client = DeadlyStreamClient()
    state.hub.publish({"type": "auth", "logged_in": False, "username": ""})
    return {"ok": True}


# ---------------------------------------------------------------------------
# Self-update check (GitHub Releases)
# ---------------------------------------------------------------------------

UPDATE_REPO = "ChristopherVR/kotor-mod-manager"


def _version_tuple(v: str) -> tuple:
    import re
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3])


def _is_update_asset(name: str) -> bool:
    """Whether a release file is the self-update build for this platform."""
    low = name.lower()
    if sys.platform == "win32":
        return low.endswith(".exe")
    if sys.platform.startswith("linux"):
        return "linux" in low and not low.endswith((".sha256", ".deb", ".rpm", ".zip", ".tar.gz"))
    return False


@app.get("/api/update/check")
def update_check() -> dict:
    """Compare the running version against the latest GitHub release."""
    import json
    import urllib.request

    api_url = f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest"
    req = urllib.request.Request(
        api_url,
        headers={"User-Agent": "kotor-mod-installer",
                 "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"available": False, "current_version": __version__, "error": str(e)}

    tag = data.get("tag_name", "") or ""
    latest = tag.lstrip("vV")
    available = bool(latest) and _version_tuple(latest) > _version_tuple(__version__)
    asset_url = None
    for a in data.get("assets", []):
        if _is_update_asset(a.get("name", "")):
            asset_url = a.get("browser_download_url")
            break
    return {
        "available": available,
        "current_version": __version__,
        "latest_version": latest,
        "url": data.get("html_url"),
        "asset_url": asset_url,
        "notes": (data.get("body") or "")[:2000],
        "repo": UPDATE_REPO,
    }


@app.post("/api/update/open")
def update_open(url: str = "") -> dict:
    """Open a URL in the user's default browser (reliable from a windowless exe)."""
    import sys
    target = url or f"https://github.com/{UPDATE_REPO}/releases/latest"
    if not (target.startswith("http://") or target.startswith("https://")):
        return JSONResponse(status_code=400, content={"ok": False, "error": "invalid_url"})
    try:
        if sys.platform == "win32":
            # os.startfile is the most reliable way to open a URL from a
            # windowless (PyInstaller --windowed) subprocess.
            os.startfile(target)  # noqa: S606
        else:
            import webbrowser
            webbrowser.open(target)
        return {"ok": True}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


# ---------------------------------------------------------------------------
# What's New (changelog for the current version)
# ---------------------------------------------------------------------------

def _changelog_section(version: str) -> str:
    """Extract the '## [version]' section from the bundled/repo CHANGELOG.md."""
    import re
    from installer.config_loader import _bundle_root
    candidates = [
        _bundle_root() / "CHANGELOG.md",
        Path(__file__).resolve().parent.parent / "CHANGELOG.md",
    ]
    text = ""
    for c in candidates:
        try:
            if c.exists():
                text = c.read_text(encoding="utf-8")
                break
        except OSError:
            continue
    if not text:
        return ""
    m = re.search(rf"^## \[{re.escape(version)}\].*?(?=^## \[|\Z)", text, re.M | re.S)
    return m.group(0).strip() if m else ""


@app.get("/api/whatsnew")
def whatsnew() -> dict:
    """Return the changelog for the running version and whether to show it."""
    conf = cfg.load()
    last_seen = conf.get("last_seen_version", "")
    notes = _changelog_section(__version__)
    return {
        "version": __version__,
        "notes": notes,
        "show": bool(notes) and last_seen != __version__,
    }


@app.post("/api/whatsnew/seen")
def whatsnew_seen() -> dict:
    conf = cfg.load()
    conf["last_seen_version"] = __version__
    cfg.save(conf)
    return {"ok": True}


@app.post("/api/update/download")
def update_download() -> dict:
    """
    Download the latest release exe to a temp file, streaming progress over the
    WebSocket, and return its local path. The Tauri shell then swaps it in.
    """
    import tempfile
    import urllib.request

    info = update_check()
    asset = info.get("asset_url")
    if not asset:
        return JSONResponse(status_code=400, content={"ok": False, "error": "no_exe_asset"})

    dest_dir = Path(tempfile.gettempdir()) / "kotor-mod-installer-update"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / ("KOTOR-Mod-Installer-new.exe" if sys.platform == "win32"
                        else "KOTOR-Mod-Installer-new")

    try:
        req = urllib.request.Request(asset, headers={"User-Agent": "kotor-mod-installer"})
        with urllib.request.urlopen(req, timeout=300) as r, open(dest, "wb") as f:
            total = int(r.headers.get("Content-Length", 0))
            done = 0
            last_pct = -1
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total:
                    pct = int(done * 100 / total)
                    if pct != last_pct:
                        last_pct = pct
                        state.hub.publish({"type": "update_progress", "pct": pct,
                                           "downloaded": done, "total": total})
        if sys.platform != "win32":
            dest.chmod(0o755)
        state.hub.publish({"type": "log", "message": "Update downloaded - restarting to apply.",
                           "tag": "success"})
        return {"ok": True, "path": str(dest), "version": info.get("latest_version")}
    except Exception as e:
        dest.unlink(missing_ok=True)
        state.hub.publish({"type": "log", "message": f"Update download failed: {e}", "tag": "error"})
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.get("/api/settings")
def get_settings() -> dict:
    conf = cfg.load()
    return {
        "kotor1_path": conf.get("kotor1_path", ""),
        "kotor2_path": conf.get("kotor2_path", ""),
        "download_dir": conf.get("download_dir", ""),
        "language": conf.get("language", "en"),
        "custom_patcher_path": conf.get("custom_patcher_path", ""),
        "nexus_api_key": conf.get("nexus_api_key", ""),
        "preferred_resolution": conf.get("preferred_resolution", ""),
        "screen_resolution": conf.get("preferred_resolution") or _screen_resolution(),
        "browser_download_mode": conf.get("browser_download_mode", sys.platform != "win32"),
    }


@app.get("/api/patcher/status")
def patcher_status() -> dict:
    """Detailed patcher-engine info for the Settings → Patcher section."""
    holo = find_system_holopatcher()
    conf = cfg.load()
    custom = conf.get("custom_patcher_path", "")
    source = "none"
    if custom and holo and str(holo) == custom:
        source = "custom"
    elif holo:
        source = "bundled" if "_MEI" in str(holo) or "tools" in str(holo).lower() else "system"
    return {
        "available": holo is not None,
        "path": str(holo) if holo else None,
        "source": source,
        "custom_patcher_path": custom,
        "strategies": ["holopatcher_shim", "win32_automation", "pywinauto_automation", "gui_manual"],
    }


@app.post("/api/settings")
def set_settings(s: SettingsModel) -> dict:
    conf = cfg.load()
    conf.update(s.model_dump())
    cfg.save(conf)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Game install profiles (multiple KOTOR installs on one machine)
# ---------------------------------------------------------------------------

def _profile_dict(p: dict) -> dict:
    return {**p, "is_default": p["id"] in ("KOTOR1", "KOTOR2")}


@app.get("/api/profiles")
def get_profiles() -> dict:
    conf = cfg.load()
    return {
        "profiles": [_profile_dict(p) for p in cfg.get_profiles(conf)],
        "active": conf.get("active_profile", ""),
    }


@app.get("/api/games/detect")
def detect_games() -> dict:
    """Steam installs of KOTOR found on the computer."""
    from installer.game_locator import find_game_installs
    known = set()
    for p in cfg.get_profiles(cfg.load()):
        if p.get("path"):
            try:
                known.add(str(Path(p["path"]).resolve()))
            except OSError:
                known.add(p["path"])
    found = []
    for g in find_game_installs():
        try:
            resolved = str(Path(g["path"]).resolve())
        except OSError:
            resolved = g["path"]
        found.append({**g, "already_added": resolved in known})
    return {"installs": found}


@app.post("/api/profiles")
def create_profile(req: ProfileCreate) -> dict:
    if req.game not in ("KOTOR1", "KOTOR2"):
        return JSONResponse(status_code=400, content={"ok": False, "error": "invalid_game"})
    prof = cfg.add_profile(req.name, req.game, req.path)
    return {"ok": True, "profile": _profile_dict(prof)}


@app.patch("/api/profiles/{profile_id}")
def patch_profile(profile_id: str, req: ProfileUpdate) -> dict:
    prof = cfg.update_profile(profile_id, name=req.name, path=req.path)
    if not prof:
        return JSONResponse(status_code=404, content={"ok": False, "error": "not_found"})
    return {"ok": True, "profile": _profile_dict(prof)}


@app.delete("/api/profiles/{profile_id}")
def delete_profile(profile_id: str) -> dict:
    if profile_id in ("KOTOR1", "KOTOR2"):
        return JSONResponse(status_code=400, content={"ok": False, "error": "cannot_delete_default"})
    ok = cfg.remove_profile(profile_id)
    return {"ok": ok}


@app.post("/api/profiles/active")
def set_active(req: ActiveProfileRequest) -> dict:
    ok = cfg.set_active_profile(req.id)
    return {"ok": ok}


# ---------------------------------------------------------------------------
# Mod details (description / screenshots / Nexus link)
# ---------------------------------------------------------------------------

_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_CACHE_TTL = 86400  # 24 hours


def _mod_cache_dir() -> Path:
    d = cfg.CONFIG_DIR / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read_json_cache(filename: str) -> dict:
    """Load a JSON cache file and return only entries that are still within TTL."""
    path = _mod_cache_dir() / filename
    try:
        with open(path, encoding="utf-8") as f:
            raw: dict = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    cutoff = time.time() - _CACHE_TTL
    return {k: v["data"] for k, v in raw.items()
            if isinstance(v, dict) and v.get("_ts", 0) > cutoff}


def _write_json_cache(filename: str, key: str, data) -> None:
    """Persist a single cache entry to disk with a timestamp."""
    path = _mod_cache_dir() / filename
    try:
        with open(path, encoding="utf-8") as f:
            raw: dict = json.load(f)
    except (OSError, json.JSONDecodeError):
        raw = {}
    now = time.time()
    raw[key] = {"data": data, "_ts": now}
    # Prune stale entries while we have the file open.
    cutoff = now - _CACHE_TTL
    raw = {k: v for k, v in raw.items()
           if isinstance(v, dict) and v.get("_ts", 0) > cutoff}
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False)
    except OSError:
        pass


_MODINFO_CACHE: dict = _read_json_cache("mod_info.json")
_NEXUS_CACHE: dict = _read_json_cache("nexus_urls.json")


def _resolve_nexus_url(name: str, game: str) -> str:
    """
    Find the actual Nexus mod page for `name`. Uses the Nexus API (if a key is
    configured) for an accurate match, then a best-effort scrape, then a search
    URL. Cached per (game, name).
    """
    from scraper import nexus
    domain = nexus.GAME_DOMAIN.get(game, "kotor")
    fallback = nexus.search_url(game, name)
    if not name:
        return fallback
    ckey = f"{game}:{name.lower()}"
    if ckey in _NEXUS_CACHE:
        return _NEXUS_CACHE[ckey]

    result = fallback
    api_key = cfg.load().get("nexus_api_key", "")
    # 1. Official-ish API search (accurate, needs a key).
    if api_key:
        try:
            url = nexus.search_by_name(name, game, api_key)
            if url:
                _NEXUS_CACHE[ckey] = url
                _write_json_cache("nexus_urls.json", ckey, url)
                return url
        except Exception:
            pass
    # 2. Best-effort HTML scrape of the public search page.
    try:
        import re as _re
        import requests as _rq
        r = _rq.get(fallback, headers={"User-Agent": _BROWSER_UA}, timeout=5)
        if r.status_code == 200:
            m = _re.search(rf"/{_re.escape(domain)}/mods/(\d+)", r.text)
            if m:
                result = f"https://www.nexusmods.com/{domain}/mods/{m.group(1)}"
    except Exception:
        pass
    _NEXUS_CACHE[ckey] = result
    _write_json_cache("nexus_urls.json", ckey, result)
    return result


@app.get("/api/mod/info")
def mod_info(file_id: str, slug: str = "", game: str = "KOTOR1") -> dict:
    cache_key = f"{file_id}:{game}"
    if cache_key in _MODINFO_CACHE:
        return _MODINFO_CACHE[cache_key]
    details = state.client.get_mod_details(file_id, slug)
    name = details.get("title", "")
    details["nexus_url"] = _resolve_nexus_url(name, game)
    if not details.get("error"):
        _MODINFO_CACHE[cache_key] = details
        _write_json_cache("mod_info.json", cache_key, details)
    return details


@app.post("/api/open-path")
def open_path(req: OpenPathRequest) -> dict:
    """Open an arbitrary local path in the OS file manager."""
    from backend.fsutil import reveal_path
    if not req.path:
        return JSONResponse(status_code=400, content={"ok": False, "error": "path_required"})
    if not reveal_path(req.path, select=req.select):
        return JSONResponse(status_code=404, content={"ok": False, "error": "not_found"})
    return {"ok": True}


@app.post("/api/mod/open-download")
def open_mod_download(req: OpenDownloadRequest) -> dict:
    """Open a build mod's download folder (download_dir/<file_id>_<slug[:30]>).

    If that mod hasn't been downloaded yet, open the base downloads folder
    instead (creating it if needed) so the action always opens something useful.
    """
    from backend.fsutil import reveal_path
    base = cfg.download_dir()
    from installer.download_paths import download_folder_name
    folder = base / download_folder_name(req.file_id, req.slug)
    fallback = False
    if not folder.exists():
        # Not downloaded yet - reveal the downloads folder so the user can see
        # where mods will land.
        fallback = True
        folder = base
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            return JSONResponse(status_code=404, content={"ok": False, "error": "download_unavailable"})
    if not reveal_path(folder):
        return JSONResponse(status_code=500, content={"ok": False, "error": "open_failed"})
    return {"ok": True, "path": str(folder), "fallback": fallback}


@app.post("/api/nexus/nxm")
def nexus_nxm(req: NxmRequest) -> dict:
    """An nxm:// link from the Nexus website ("Mod manager download"). Hands it
    to the download that is waiting for it."""
    from scraper import nexus
    try:
        link = nexus.parse_nxm(req.url)
    except ValueError:
        return JSONResponse(status_code=400, content={"ok": False, "error": "not_an_nxm_link"})
    if not nexus.NXM.deliver(link):
        return JSONResponse(status_code=400, content={"ok": False, "error": "other_game"})
    state.hub.publish({"type": "log", "message": "Got the Nexus download link - continuing.",
                       "tag": "success"})
    return {"ok": True}


@app.get("/api/nexus/validate")
def nexus_validate() -> dict:
    """Validate the configured Nexus API key (for the Settings UI)."""
    from scraper import nexus
    key = cfg.load().get("nexus_api_key", "")
    return nexus.validate(key)


def _image_cache_dir() -> Path:
    d = cfg.CONFIG_DIR / "cache" / "images"
    d.mkdir(parents=True, exist_ok=True)
    return d


@app.get("/api/mod/image")
def mod_image(url: str):
    """
    Proxy a DeadlyStream image through the authenticated session so screenshots
    render in the app (avoids hotlink protection / login-gated images / CSP).
    Restricted to deadlystream.com, and cached on disk so repeat views are fast.
    """
    import hashlib
    from urllib.parse import urlparse
    from fastapi import Response

    host = urlparse(url).hostname or ""
    if not (host == "deadlystream.com" or host.endswith(".deadlystream.com")):
        return JSONResponse(status_code=400, content={"ok": False, "error": "host_not_allowed"})

    ext = os.path.splitext(urlparse(url).path)[1].lower()
    media = {
        ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp",
        ".bmp": "image/bmp", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    }.get(ext, "image/jpeg")

    key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    cache_file = _image_cache_dir() / f"{key}{ext or '.img'}"
    if cache_file.exists():
        return Response(content=cache_file.read_bytes(), media_type=media,
                        headers={"Cache-Control": "max-age=604800"})
    try:
        r = state.client._session.get(
            url, timeout=20, headers={"Referer": "https://deadlystream.com/"}
        )
        r.raise_for_status()
        ctype = r.headers.get("Content-Type", media)
        if "image" not in ctype:
            return JSONResponse(status_code=415, content={"ok": False, "error": "not_an_image"})
        try:
            cache_file.write_bytes(r.content)
        except OSError:
            pass
        return Response(content=r.content, media_type=ctype,
                        headers={"Cache-Control": "max-age=604800"})
    except Exception as e:
        return JSONResponse(status_code=502, content={"ok": False, "error": str(e)})


# Build lists change rarely (the guides are edited every few weeks) but take a
# few seconds to fetch and parse. Cache the scraped result so opening a build is
# instant, and let the player force a fresh fetch when they want the latest.
_BUILD_LIST_CACHE = "build_lists.json"


def _cached_build_mods(build_key: str) -> "list | None":
    entry = _read_json_cache(_BUILD_LIST_CACHE).get(build_key)
    if not entry:
        return None
    try:
        return [BuildMod(**m) for m in entry]
    except (TypeError, ValueError):
        return None   # cache written by an older version - refetch


@app.post("/api/builds/{build_key}/load")
def load_build(build_key: str, profile: str = "", refresh: bool = False) -> dict:
    """
    Load a build's mod list.

    Served from the on-disk cache when there is one, so switching between builds
    does not re-scrape the guide every time. refresh=true forces a fresh fetch,
    which is what the Load button does.
    """
    custom = None if build_key in BUILD_URLS else cfg.get_custom_build(build_key)
    if build_key not in BUILD_URLS and not custom:
        return JSONResponse(status_code=404, content={"ok": False, "error": "Unknown build"})

    from dataclasses import asdict

    mods = None if refresh else _cached_build_mods(build_key)
    from_cache = mods is not None
    if mods is None:
        try:
            if custom:
                mods = scrape_build(build_key, url=custom["url"], game=custom["game"])
            else:
                mods = scrape_build(build_key)
        except Exception as e:
            # A refresh that fails should not lose a list the player already had.
            stale = _cached_build_mods(build_key)
            if stale:
                mods, from_cache = stale, True
            else:
                return JSONResponse(status_code=502,
                                    content={"ok": False, "error": f"Scrape failed: {e}"})
        else:
            _write_json_cache(_BUILD_LIST_CACHE, build_key,
                              [asdict(m) for m in mods])
    state.loaded_mods[build_key] = mods
    state.current_build = build_key

    # Cross-reference with the installed library so the UI can flag mods the
    # player already has. source_ref is the file_id set at install time.
    try:
        from installer import mod_manager as mm
        from installer.download_paths import download_folder_name
        game = _resolve_build_game(build_key)
        scope = profile if (profile and cfg.get_profile(profile)) else game
        manifest = mm.load_manifest(scope)
        installed_refs = {m.source_ref for m in manifest.mods if m.source_type == "build"}
    except Exception:
        installed_refs = set()

    # A resolution-specific archive installed for a previous screen size is
    # not current for this one. Let the player select and reinstall it.
    resolution = cfg.load().get("preferred_resolution") or _screen_resolution()
    resolution_match = re.search(r"(\d{3,4})\s*[x×]\s*(\d{3,4})", resolution or "")
    target_resolution = (int(resolution_match.group(1)), int(resolution_match.group(2))) if resolution_match else None
    download_root = cfg.download_dir()

    def installed_for_resolution(mod) -> bool:
        if mod.file_id not in installed_refs:
            return False
        folder = download_root / download_folder_name(mod.file_id, mod.slug)
        try:
            manifest_path = folder / ".downloads.json"
            if manifest_path.is_file():
                names = json.loads(manifest_path.read_text(encoding="utf-8")).get("files", [])
            else:
                names = [p.name for p in folder.iterdir()
                         if p.is_file() and p.suffix.lower() in (".zip", ".7z", ".rar")]
        except (OSError, ValueError, AttributeError):
            return True  # Legacy installs may no longer have their download cache.
        tagged = [found for found in (_resolution_from_name(name) for name in names) if found]
        return not (tagged and target_resolution and target_resolution not in tagged)

    return {
        "ok": True,
        "build_key": build_key,
        "from_cache": from_cache,
        "mods": [{**build_mod_to_dict(m), "installed": installed_for_resolution(m)} for m in mods],
    }


@app.get("/api/credentials")
def get_credentials() -> dict:
    u, _ = DeadlyStreamClient.load_credentials()
    return {"username": u}


# ---------------------------------------------------------------------------
# Mod build source site (where build guides are scraped from)
# ---------------------------------------------------------------------------

_SOURCE_SITE_KEYRING = "kotor_mod_installer_source"


def _source_has_password(username: str) -> bool:
    if not username:
        return False
    try:
        import keyring
        return bool(keyring.get_password(_SOURCE_SITE_KEYRING, username))
    except Exception:
        return False


@app.get("/api/source-site")
def get_source_site() -> dict:
    conf = cfg.load()
    username = conf.get("source_site_username", "")
    return {
        "url": conf.get("source_site_url", "") or "https://kotor.neocities.org",
        "username": username,
        "has_password": _source_has_password(username),
    }


@app.post("/api/source-site")
def set_source_site(req: SourceSiteRequest) -> dict:
    conf = cfg.load()
    if req.url:
        url = req.url.strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            return JSONResponse(status_code=400, content={"ok": False, "error": "invalid_url"})
        conf["source_site_url"] = url
    old_username = conf.get("source_site_username", "")
    conf["source_site_username"] = req.username.strip()
    cfg.save(conf)

    # Manage the password in the OS keyring (never in the JSON config).
    try:
        import keyring
        if req.clear_password and old_username:
            try:
                keyring.delete_password(_SOURCE_SITE_KEYRING, old_username)
            except Exception:
                pass
        elif req.password and req.username.strip():
            keyring.set_password(_SOURCE_SITE_KEYRING, req.username.strip(), req.password)
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": f"keyring_error: {e}"})

    return {"ok": True, "has_password": _source_has_password(conf["source_site_username"])}


@app.post("/api/install/start")
def install_start(req: StartInstallRequest) -> dict:
    if state.pipeline and state.pipeline.is_running:
        return JSONResponse(status_code=409, content={"ok": False, "error": "Install already running"})
    mods = state.loaded_mods.get(req.build_key)
    if not mods:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Load the mod list first"})
    conf = cfg.load()
    browser_dl = conf.get("browser_download_mode", sys.platform != "win32")
    if not browser_dl and not state.client._logged_in:
        return JSONResponse(status_code=401, content={"ok": False, "error": "Not logged in"})

    # Honour a user selection (toggled-off mods are skipped).
    if req.selected_file_ids is not None:
        chosen = set(req.selected_file_ids)
        mods = [m for m in mods if m.file_id in chosen]
        if not mods:
            return JSONResponse(status_code=400, content={"ok": False, "error": "No mods selected"})

    game = _resolve_build_game(req.build_key)
    # Resolve the target install: a chosen profile, else the game default.
    scope = game
    if req.profile:
        prof = cfg.get_profile(req.profile)
        if prof:
            scope = req.profile
            if not req.game_path:
                req.game_path = prof.get("path", "")
    game_path = state.game_path_for(req.build_key, req.game_path)
    if not game_path or not game_path.exists():
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "game_path_required", "game": game},
        )
    # Make sure it's actually a KOTOR install before downloading anything - a
    # wrong folder otherwise only fails deep inside the patcher.
    from installer.game_validate import is_kotor_install
    if not is_kotor_install(game_path):
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "game_path_invalid",
                     "game": game, "path": str(game_path)},
        )

    conf = cfg.load()
    dl_dir = Path(conf.get("download_dir") or (Path.home() / "Downloads" / "KOTOR_Mods"))

    # Persist a game_path override into settings for convenience.
    if req.game_path and scope == game:
        key = "kotor1_path" if game == "KOTOR1" else "kotor2_path"
        conf[key] = req.game_path
        cfg.save(conf)

    hub = state.hub

    def on_status(file_id: str, st: ModStatus, detail: str) -> None:
        hub.publish({"type": "status", "file_id": file_id,
                     "status": st.name, "status_label": st.value, "detail": detail})

    def on_log(message: str, tag: str) -> None:
        hub.publish({"type": "log", "message": message, "tag": tag})

    def on_progress(file_id: str, pct: float, kb: int, total_kb: int) -> None:
        hub.publish({"type": "progress", "file_id": file_id,
                     "pct": pct, "kb": kb, "total_kb": total_kb})

    def on_install_progress(file_id: str, pct: float, label: str) -> None:
        hub.publish({"type": "install_progress", "file_id": file_id,
                     "pct": pct, "label": label})

    def on_manual(file_id: str, name: str, folder: str, readme: str) -> None:
        hub.publish({"type": "manual", "file_id": file_id, "name": name,
                     "folder": folder, "readme": readme[:4000]})

    def on_confirm(request_id: str, title: str, body: str, options: list) -> None:
        hub.publish({"type": "confirm", "id": request_id, "title": title,
                     "body": body, "options": options})

    state.pipeline = Pipeline(
        mods=mods,
        game_path=game_path,
        download_dir=dl_dir,
        client=state.client,
        on_status=on_status,
        on_log=on_log,
        on_progress=on_progress,
        on_install_progress=on_install_progress,
        on_manual=on_manual,
        on_confirm=on_confirm,
        auto_unattended=req.unattended,
        game_key=scope,
        game_type=game,
        language=conf.get("language", "en"),
        screen_resolution=conf.get("preferred_resolution") or _screen_resolution(),
        browser_download_mode=browser_dl,
    )
    state.pipeline.start()
    hub.publish({"type": "pipeline", "event": "started",
                 "build_key": req.build_key, "total": len(mods)})

    # Notify completion on a watcher thread.
    def _watch(pl: Pipeline) -> None:
        if pl._thread:
            pl._thread.join()
        done = sum(1 for pm in pl.mods if pm.status in (ModStatus.DONE, ModStatus.SKIPPED))
        errors = sum(1 for pm in pl.mods if pm.status == ModStatus.ERROR)
        manual = sum(1 for pm in pl.mods if pm.status == ModStatus.MANUAL)
        hub.publish({"type": "pipeline", "event": "finished",
                     "done": done, "errors": errors, "manual": manual,
                     "total": len(pl.mods)})

    threading.Thread(target=_watch, args=(state.pipeline,), daemon=True).start()
    return {"ok": True, "total": len(mods), "game_path": str(game_path)}


@app.post("/api/install/confirm")
def install_confirm(req: ConfirmAnswer) -> dict:
    """The player's answer to a question the installer is waiting on."""
    pl = state.pipeline
    if not pl or not pl.answer_confirm(req.id, req.choice):
        return JSONResponse(status_code=404, content={"ok": False, "error": "no_such_question"})
    return {"ok": True}


@app.post("/api/install/{action}")
def install_control(action: str) -> dict:
    pl = state.pipeline
    if not pl:
        return JSONResponse(status_code=400, content={"ok": False, "error": "No pipeline"})
    if action == "pause":
        pl.pause()
    elif action == "resume":
        pl.resume()
    elif action == "stop":
        pl.stop()
    elif action == "retry":
        pl.retry_failed()
    else:
        return JSONResponse(status_code=404, content={"ok": False, "error": "Unknown action"})
    return {"ok": True, "action": action}


@app.get("/api/install/state")
def install_state() -> dict:
    pl = state.pipeline
    if not pl:
        return {"running": False, "mods": []}
    return {
        "running": pl.is_running,
        "mods": [pipeline_mod_to_dict(pm) for pm in pl.mods],
    }


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await state.hub.connect(ws)
    try:
        # Send a hello + current status snapshot on connect.
        await ws.send_json({"type": "hello", "version": __version__})
        while True:
            await ws.receive_text()   # keep the connection open; ignore inbound
    except WebSocketDisconnect:
        state.hub.disconnect(ws)
    except Exception:
        state.hub.disconnect(ws)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8756)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
