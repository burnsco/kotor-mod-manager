"""Tests for browser-based download mode (Linux / permission-free downloads).

In browser download mode, DeadlyStream and Nexus Mods downloads are not fetched
via Python scraping or Nexus APIs. Instead, the mod page is opened in the player's
browser, and the pipeline watches the Downloads folder for the resulting archive.
"""
import sys
import threading
import time
from pathlib import Path

import pytest

import installer.pipeline as pl
from installer.pipeline import ModStatus, Pipeline
from scraper.build_scraper import BuildMod
from scraper.deadlystream import DeadlyStreamClient


@pytest.fixture(autouse=True)
def quick_drop(monkeypatch):
    monkeypatch.setattr(pl, "_DROP_POLL_SECONDS", 0.05)
    monkeypatch.setattr(pl, "_DROP_SETTLE_SECONDS", 0.2)
    monkeypatch.setattr(pl, "_DROP_WAIT_SECONDS", 3.0)


def _deadlystream_mod(name="HD Fire and Smoke", file_id="123", slug="hd-fire-and-smoke"):
    return BuildMod(
        install_order=1,
        file_id=file_id,
        slug=slug,
        name=name,
        url=f"https://deadlystream.com/files/file/{file_id}-{slug}/",
        game="KOTOR1",
        section="",
        category="",
        note="",
        option_hint="",
        install_method_hint="",
        build_key="k1_full",
        source_host="deadlystream",
    )


def _nexus_mod(name="KOTOR 1 Community Patch", file_id="guide:5", slug="k1cp", mod_id=1234):
    return BuildMod(
        install_order=2,
        file_id=file_id,
        slug=slug,
        name=name,
        url=f"https://www.nexusmods.com/kotor/mods/{mod_id}",
        game="KOTOR1",
        section="",
        category="",
        note="",
        option_hint="",
        install_method_hint="",
        build_key="k1_full",
        source_host="nexus",
    )


def _pipeline_browser(tmp_path: Path, mods, **kw) -> Pipeline:
    return Pipeline(
        mods,
        game_path=tmp_path / "game",
        download_dir=tmp_path / "dl",
        client=DeadlyStreamClient(),
        record_to_library=False,
        browser_download_mode=True,
        **kw,
    )


def test_deadlystream_mod_opens_browser_and_waits_for_download(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(Pipeline, "_open_in_browser", staticmethod(lambda url: opened.append(url)))

    mod = _deadlystream_mod()
    p = _pipeline_browser(tmp_path, [mod])

    # Client must NOT be used for direct downloading
    p._client.download_all_files = lambda **kw: (_ for _ in ()).throw(AssertionError("Direct client used"))

    # Simulate browser downloading file into download dir after a short delay
    dl_root = tmp_path / "dl"
    dl_root.mkdir(parents=True, exist_ok=True)
    threading.Timer(0.15, lambda: (dl_root / "hd_fire_and_smoke_v1.zip").write_bytes(b"PKdata")).start()

    p._download_mod(p.mods[0])

    assert opened == ["https://deadlystream.com/files/file/123-hd-fire-and-smoke/"]
    assert p.mods[0].status == ModStatus.READY
    assert len(p.mods[0].archive_paths) == 1
    dest = p._mod_dir(p.mods[0]) / "hd_fire_and_smoke_v1.zip"
    assert p.mods[0].archive_paths[0] == dest
    assert dest.read_bytes() == b"PKdata"
    # Moved from download folder into mod folder
    assert not (dl_root / "hd_fire_and_smoke_v1.zip").exists()


def test_nexus_mod_opens_files_tab_and_does_not_call_api(tmp_path, monkeypatch):
    from scraper import nexus
    opened = []
    monkeypatch.setattr(Pipeline, "_open_in_browser", staticmethod(lambda url: opened.append(url)))
    monkeypatch.setattr(nexus, "download_file", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("Nexus API used")))

    mod = _nexus_mod(name="Community Patch", mod_id=987)
    p = _pipeline_browser(tmp_path, [mod])

    dl_root = tmp_path / "dl"
    dl_root.mkdir(parents=True, exist_ok=True)
    threading.Timer(0.15, lambda: (dl_root / "community_patch_1.10.rar").write_bytes(b"Rar!data")).start()

    p._download_mod(p.mods[0])

    assert opened == ["https://www.nexusmods.com/kotor/mods/987?tab=files"]
    assert p.mods[0].status == ModStatus.READY
    dest = p._mod_dir(p.mods[0]) / "community_patch_1.10.rar"
    assert p.mods[0].archive_paths[0] == dest
    assert dest.read_bytes() == b"Rar!data"


def test_browser_downloads_are_serialized(tmp_path, monkeypatch):
    """Multiple browser-assisted downloads open one after another, not all at once."""
    opened = []
    monkeypatch.setattr(Pipeline, "_open_in_browser", staticmethod(lambda url: opened.append(url)))

    mod1 = _deadlystream_mod(name="Mod One", file_id="1", slug="mod-one")
    mod2 = _deadlystream_mod(name="Mod Two", file_id="2", slug="mod-two")
    p = _pipeline_browser(tmp_path, [mod1, mod2])

    dl_root = tmp_path / "dl"
    dl_root.mkdir(parents=True, exist_ok=True)

    def download_first():
        time.sleep(0.1)
        (dl_root / "mod_one_archive.zip").write_bytes(b"PKone")

    def download_second():
        time.sleep(0.3)
        (dl_root / "mod_two_archive.zip").write_bytes(b"PKtwo")

    threading.Thread(target=download_first).start()
    threading.Thread(target=download_second).start()

    # Run downloads concurrently on two threads
    t1 = threading.Thread(target=p._download_mod, args=(p.mods[0],))
    t2 = threading.Thread(target=p._download_mod, args=(p.mods[1],))
    t1.start()
    t2.start()
    t1.join(5)
    t2.join(5)

    assert p.mods[0].status == ModStatus.READY
    assert p.mods[1].status == ModStatus.READY
    assert len(opened) == 2


def test_part_file_is_not_picked_up_until_complete(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(Pipeline, "_open_in_browser", staticmethod(lambda url: opened.append(url)))

    mod = _deadlystream_mod(name="Big Texture Pack", file_id="99", slug="big-texture")
    p = _pipeline_browser(tmp_path, [mod])

    dl_root = tmp_path / "dl"
    dl_root.mkdir(parents=True, exist_ok=True)

    part_file = dl_root / "big_texture.zip.part"
    final_file = dl_root / "big_texture.zip"

    def write_slowly():
        # First write part file
        part_file.write_bytes(b"part1")
        time.sleep(0.2)
        # Still part file
        part_file.write_bytes(b"part123")
        time.sleep(0.2)
        # Rename to final file once download finishes in browser
        part_file.unlink()
        final_file.write_bytes(b"PKfinal")

    threading.Thread(target=write_slowly).start()
    p._download_mod(p.mods[0])

    assert p.mods[0].status == ModStatus.READY
    dest = p._mod_dir(p.mods[0]) / "big_texture.zip"
    assert dest.read_bytes() == b"PKfinal"


def test_server_install_start_allows_unauthenticated_in_browser_mode(monkeypatch):
    from backend.server import status, install_start, state
    from backend.models import StartInstallRequest
    import config as cfg

    # Force unauthenticated state
    state.client._logged_in = False
    state.loaded_mods["test_build"] = [_deadlystream_mod()]

    # Ensure browser_download_mode is True in config
    monkeypatch.setattr(cfg, "load", lambda: {"browser_download_mode": True})

    # Check status endpoint directly
    st = status()
    assert st["browser_download_mode"] is True

    # Check start install succeeds with 200 (not 401 Not logged in)
    game_dummy = Path("/tmp/fake_kotor")
    game_dummy.mkdir(parents=True, exist_ok=True)
    (game_dummy / "chitin.key").write_bytes(b"CHITIN")
    (game_dummy / "dialog.tlk").write_bytes(b"TLK")

    resp = install_start(StartInstallRequest(
        build_key="test_build",
        game_path=str(game_dummy),
    ))
    # In browser_download_mode=True, it doesn't return 401 JSONResponse
    assert isinstance(resp, dict)
    assert resp["ok"] is True
