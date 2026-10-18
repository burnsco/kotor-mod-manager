"""Pydantic request/response models and serialization helpers for the API."""

from typing import Optional

from pydantic import BaseModel

from installer.mod_manager import InstalledMod
from installer.pipeline import PipelineMod
from scraper.build_scraper import HOST_LABELS, BuildMod


class LoginRequest(BaseModel):
    username: str
    password: str
    save: bool = True


class SettingsModel(BaseModel):
    kotor1_path: str = ""
    kotor2_path: str = ""
    download_dir: str = ""
    language: str = "en"
    custom_patcher_path: str = ""
    nexus_api_key: str = ""
    preferred_resolution: str = ""    # e.g. 1920x1080; empty = detect the screen
    browser_download_mode: Optional[bool] = None


class StartInstallRequest(BaseModel):
    build_key: str
    unattended: bool = False
    game_path: Optional[str] = None   # override; otherwise resolved from settings
    profile: str = ""                 # target install profile id (optional)
    selected_file_ids: Optional[list[str]] = None  # if set, only install these


class ImportRequest(BaseModel):
    game: str
    path: str
    name: Optional[str] = None
    option_hint: str = ""
    unattended: bool = True
    game_path: Optional[str] = None
    profile: str = ""


class UninstallRequest(BaseModel):
    force: bool = False


class BulkModRequest(BaseModel):
    """Apply one action to many library mods at once."""
    mod_ids: list[str] = []
    force: bool = False          # uninstall baked mods that have no backup


class ImportFolderRequest(BaseModel):
    game: str
    path: str            # a folder containing mod archives (zip/7z/rar)
    profile: str = ""
    unattended: bool = True
    game_path: Optional[str] = None


class ReorderRequest(BaseModel):
    game: str
    ordered_ids: list[str]


class ClearCacheRequest(BaseModel):
    """Clear downloaded mod archives. Empty names means 'everything', subject
    to keep_installed."""
    names: list[str] = []
    keep_installed: bool = True


class ResolveConflictsRequest(BaseModel):
    """Bulk conflict action. Either name the conflicts, or sweep a severity."""
    action: str                                   # dismiss | undismiss | disable_losers
    conflict_ids: list[str] = []
    all_of_severity: Optional[str] = None         # e.g. "info"


class NxmRequest(BaseModel):
    url: str


class ConfirmAnswer(BaseModel):
    id: str
    choice: str


class ProfileCreate(BaseModel):
    name: str
    game: str            # "KOTOR1" | "KOTOR2"
    path: str = ""


class ProfileUpdate(BaseModel):
    name: Optional[str] = None
    path: Optional[str] = None


class ActiveProfileRequest(BaseModel):
    id: str


class AddBuildRequest(BaseModel):
    label: str
    game: str            # "KOTOR1" | "KOTOR2"
    url: str             # a neocities-style build guide page to scrape


class SourceSiteRequest(BaseModel):
    url: str = ""
    username: str = ""
    password: str = ""        # blank = leave the saved password untouched
    clear_password: bool = False


class OpenPathRequest(BaseModel):
    path: str
    select: bool = False   # highlight the file inside its folder (Windows/macOS)


class OpenDownloadRequest(BaseModel):
    file_id: str
    slug: str = ""
    game: str = "KOTOR1"


def installed_mod_to_dict(m: InstalledMod, conflict_count: int = 0,
                          source_exists: bool = False,
                          source: "tuple[str, str] | None" = None) -> dict:
    # source is (host, page url); older records do not carry it themselves.
    host, url = source if source is not None else (m.source_host, m.source_url)
    return {
        "id": m.id,
        "name": m.name,
        "game": m.game,
        "enabled": m.enabled,
        "toggleable": m.toggleable,
        "state": m.state,
        "install_method": m.install_method,
        "deploy_kind": m.deploy_kind,
        "load_order": m.load_order,
        "source_type": m.source_type,
        "source_ref": m.source_ref,
        "source_slug": m.source_slug,
        "source_host": host,
        "source_url": url,
        "category": getattr(m, "category", "") or "",
        "build_key": m.build_key,
        "option_hint": m.option_hint,
        "file_count": len(m.deployed_files),
        "baked_count": len(m.baked_files),
        "install_ts": m.install_ts,
        "has_conflict": conflict_count > 0,
        "conflict_count": conflict_count,
        "source_exists": source_exists,
    }


def build_mod_to_dict(m: BuildMod) -> dict:
    d = m.directives
    return {
        "install_order": m.install_order,
        "file_id": m.file_id,
        "slug": m.slug,
        "name": m.name,
        "url": m.url,
        "game": m.game,
        "section": m.section,
        "category": m.category,
        "note": m.note,
        "option_hint": m.option_hint,
        "install_method_hint": m.install_method_hint,
        "build_key": m.build_key,
        "instructions": m.instructions,
        "warnings": m.warnings,
        "install_method": m.install_method,
        "description": m.description,
        "author": m.author,
        # A short, player-readable summary of the special handling we apply, so
        # the UI can flag mods that need extra steps.
        "directive_summary": d.summary(),
        # Where the mod comes from, and whether the app can fetch it by itself.
        # Nexus needs a click on their site for free accounts, so a player is
        # better off knowing that before starting a build than finding out when
        # the download does not happen.
        "source_host": m.source_host,
        "source_label": HOST_LABELS.get(m.source_host, m.source_host),
        "auto_downloadable": m.auto_downloadable,
    }


def pipeline_mod_to_dict(pm: PipelineMod) -> dict:
    return {
        "file_id": pm.build_mod.file_id,
        "name": pm.build_mod.name,
        "install_order": pm.build_mod.install_order,
        "status": pm.status.name,
        "status_label": pm.status.value,
        "error": pm.error,
        "strategy_used": pm.strategy_used,
        "download_progress": pm.download_progress,
        "download_kb": pm.download_kb,
        "download_total_kb": pm.download_total_kb,
    }
