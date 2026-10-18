import copy
import json
import os
import sys
import uuid
from pathlib import Path

CONFIG_DIR = Path.home() / ".kotor_mod_installer"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULTS = {
    "kotor1_path": "",
    "kotor2_path": "",
    "download_dir": str(Path.home() / "Downloads" / "KOTOR_Mods"),
    "deadlystream_username": "",
    "browser_download_mode": sys.platform != "win32",
    "neocities_urls": [
        "https://kotor.neocities.org/modding/mod_builds/k1/full",
        "https://kotor.neocities.org/modding/mod_builds/k1/spoiler-free",
        "https://kotor.neocities.org/modding/mod_builds/k2/full",
        "https://kotor.neocities.org/modding/mod_builds/k2/spoiler-free",
    ],
    "auto_install": False,
    "keep_archives": True,
    "language": "en",
    "last_seen_version": "",
    # Optional path to a user-supplied headless patcher exe (overrides the
    # bundled HoloPatcher). Must be HoloPatcher-CLI-compatible.
    "custom_patcher_path": "",
    # Nexus Mods personal API key (https://www.nexusmods.com/users/myaccount?tab=api).
    # Empty by default; each user sets their own in Settings. Never commit a key.
    "nexus_api_key": "",
    # Game install profiles - supports multiple KOTOR installs on one machine.
    # Each: {id, name, game ("KOTOR1"|"KOTOR2"), path}. The default profile for
    # a game uses id == game so existing manifests map straight across.
    "game_profiles": [],
    "active_profile": "",
    # User-added mod builds (on top of the four built-in ones). Each is a named
    # guide page we scrape just like the built-ins:
    # {key, label, game ("KOTOR1"|"KOTOR2"), url}. Lets players plug in any
    # neocities-style build guide without a code change.
    "custom_builds": [],
    # The website mod build guides are scraped from. Public today (no login),
    # but the username is stored here (and the password in the OS keyring) so a
    # source that needs an account can be supported without a code change.
    "source_site_url": "https://kotor.neocities.org",
    "source_site_username": "",
}

DEADLYSTREAM_BASE = "https://deadlystream.com"
DEADLYSTREAM_LOGIN = "https://deadlystream.com/login/"
DEADLYSTREAM_DOWNLOAD = "https://deadlystream.com/files/file/{file_id}/?do=download&csrfKey={csrf_key}"


def _reset_to_defaults() -> dict:
    """Start over from the built-in defaults and persist them. Deep copied so
    that editing the returned settings cannot reach back into DEFAULTS and
    change what "fresh settings" mean for the rest of the run."""
    fresh = copy.deepcopy(DEFAULTS)
    save(fresh)
    return fresh


def _quarantine_bad_config() -> None:
    """Move an unreadable config aside so the user still has it if they want
    to pick settings out of it by hand. Never blocks the app from starting."""
    try:
        os.replace(CONFIG_FILE, CONFIG_FILE.with_suffix(".json.corrupt"))
    except OSError:
        pass


def load() -> dict:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        return _reset_to_defaults()
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        # Empty or damaged settings file (a half-written save, a crash mid-write,
        # antivirus interrupting first run). Keep the old file for reference and
        # carry on with defaults rather than breaking every later launch.
        _quarantine_bad_config()
        return _reset_to_defaults()
    if not isinstance(data, dict):
        _quarantine_bad_config()
        return _reset_to_defaults()
    # Deep copied for the same reason: a settings file saved by an older
    # version is missing newer keys, which would otherwise be filled in with
    # the shared DEFAULTS lists and edited in place.
    merged = {**copy.deepcopy(DEFAULTS), **data}
    if _migrate_profiles(merged):
        save(merged)
    return merged


def save(cfg: dict) -> None:
    """Write settings atomically: a temp file in the same folder is filled in
    completely, then swapped over config.json. A crash mid-save leaves the
    previous settings intact instead of a 0-byte file."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CONFIG_FILE)
    except BaseException:
        # Don't leave a half-written scratch file lying around.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def download_dir() -> Path:
    """The folder downloads go into, falling back to the default if unset."""
    raw = load().get("download_dir", "")
    return Path(raw) if raw else (Path.home() / "Downloads" / "KOTOR_Mods")


# ---------------------------------------------------------------------------
# Game install profiles
# ---------------------------------------------------------------------------

def _migrate_profiles(cfg: dict) -> bool:
    """Seed default profiles from legacy kotor1_path/kotor2_path. Returns True
    if the config was changed."""
    if cfg.get("game_profiles"):
        return False
    profiles = []
    for game, key, name in (("KOTOR1", "kotor1_path", "KOTOR 1"),
                            ("KOTOR2", "kotor2_path", "KOTOR 2")):
        path = cfg.get(key, "")
        # Default profile id == game so existing <game>.json manifests map across.
        profiles.append({"id": game, "name": name, "game": game, "path": path})
    cfg["game_profiles"] = profiles
    if not cfg.get("active_profile"):
        cfg["active_profile"] = "KOTOR1"
    return True


def get_profiles(cfg: dict | None = None) -> list:
    cfg = cfg or load()
    return cfg.get("game_profiles", [])


def get_profile(profile_id: str, cfg: dict | None = None) -> dict | None:
    for p in get_profiles(cfg):
        if p["id"] == profile_id:
            return p
    return None


def add_profile(name: str, game: str, path: str) -> dict:
    cfg = load()
    prof = {"id": uuid.uuid4().hex[:12], "name": name, "game": game, "path": path}
    cfg.setdefault("game_profiles", []).append(prof)
    # Keep the legacy single-path fields pointing at the first profile per game.
    _sync_legacy_paths(cfg)
    save(cfg)
    return prof


def update_profile(profile_id: str, *, name: str | None = None,
                   path: str | None = None) -> dict | None:
    cfg = load()
    prof = None
    for p in cfg.get("game_profiles", []):
        if p["id"] == profile_id:
            if name is not None:
                p["name"] = name
            if path is not None:
                p["path"] = path
            prof = p
            break
    if prof:
        _sync_legacy_paths(cfg)
        save(cfg)
    return prof


def remove_profile(profile_id: str) -> bool:
    cfg = load()
    before = len(cfg.get("game_profiles", []))
    cfg["game_profiles"] = [p for p in cfg.get("game_profiles", []) if p["id"] != profile_id]
    if cfg.get("active_profile") == profile_id:
        cfg["active_profile"] = cfg["game_profiles"][0]["id"] if cfg["game_profiles"] else ""
    changed = len(cfg["game_profiles"]) != before
    if changed:
        _sync_legacy_paths(cfg)
        save(cfg)
    return changed


def set_active_profile(profile_id: str) -> bool:
    cfg = load()
    if not any(p["id"] == profile_id for p in cfg.get("game_profiles", [])):
        return False
    cfg["active_profile"] = profile_id
    save(cfg)
    return True


# ---------------------------------------------------------------------------
# Custom mod builds (user-added guide pages)
# ---------------------------------------------------------------------------

def get_custom_builds(cfg: dict | None = None) -> list:
    cfg = cfg or load()
    return cfg.get("custom_builds", [])


def get_custom_build(key: str, cfg: dict | None = None) -> dict | None:
    for b in get_custom_builds(cfg):
        if b.get("key") == key:
            return b
    return None


def add_custom_build(label: str, game: str, url: str) -> dict:
    cfg = load()
    builds = cfg.setdefault("custom_builds", [])
    build = {"key": f"custom_{uuid.uuid4().hex[:10]}", "label": label,
             "game": game, "url": url}
    builds.append(build)
    save(cfg)
    return build


def remove_custom_build(key: str) -> bool:
    cfg = load()
    before = len(cfg.get("custom_builds", []))
    cfg["custom_builds"] = [b for b in cfg.get("custom_builds", []) if b.get("key") != key]
    changed = len(cfg["custom_builds"]) != before
    if changed:
        save(cfg)
    return changed


def _sync_legacy_paths(cfg: dict) -> None:
    """Mirror the first profile of each game back into kotor1/2_path so the
    legacy curated-install flow keeps working."""
    for game, key in (("KOTOR1", "kotor1_path"), ("KOTOR2", "kotor2_path")):
        prof = next((p for p in cfg.get("game_profiles", []) if p["game"] == game), None)
        if prof:
            cfg[key] = prof["path"]
