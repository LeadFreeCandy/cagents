"""Tests for cagents' own (deliberately tiny) persistent store."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import SID1, SID2

from cagents.store import Store, default_store_path


def test_load_missing_file_gives_empty_store(tmp_path: Path):
    store = Store.load(tmp_path / "state.json")
    assert store.sessions == {}


def test_load_corrupt_file_gives_empty_store(tmp_path: Path):
    path = tmp_path / "state.json"
    path.write_text("{not json", "utf-8")
    assert Store.load(path).sessions == {}


def test_track_and_roundtrip(tmp_path: Path):
    path = tmp_path / "state.json"
    store = Store.load(path)
    store.track(SID1, "/proj/alpha", "2026-08-17T10:00:00+00:00", label="my label")
    store.track(SID2, "/proj/beta", "2026-08-17T11:00:00+00:00")

    reloaded = Store.load(path)
    assert set(reloaded.sessions) == {SID1, SID2}
    t = reloaded.sessions[SID1]
    assert t.project_dir == "/proj/alpha"
    assert t.label == "my label"
    assert t.reviewed_at == ""


def test_track_is_idempotent(tmp_path: Path):
    store = Store.load(tmp_path / "state.json")
    store.track(SID1, "/proj/alpha", "2026-08-17T10:00:00+00:00")
    store.mark_reviewed(SID1, "2026-08-17T12:00:00+00:00")
    # Tracking again must not clobber review state
    store.track(SID1, "/proj/alpha", "2026-08-17T13:00:00+00:00")
    assert store.sessions[SID1].reviewed_at == "2026-08-17T12:00:00+00:00"


def test_untrack(tmp_path: Path):
    path = tmp_path / "state.json"
    store = Store.load(path)
    store.track(SID1, "/proj/alpha", "2026-08-17T10:00:00+00:00")
    store.untrack(SID1)
    assert Store.load(path).sessions == {}
    store.untrack(SID1)  # no-op, no crash


def test_review_note_label(tmp_path: Path):
    path = tmp_path / "state.json"
    store = Store.load(path)
    store.track(SID1, "/proj/alpha", "2026-08-17T10:00:00+00:00")
    store.mark_reviewed(SID1, "2026-08-17T12:00:00+00:00")
    store.set_note(SID1, "waiting on CI")
    store.set_label(SID1, "auth work")

    reloaded = Store.load(path)
    t = reloaded.sessions[SID1]
    assert t.reviewed_at == "2026-08-17T12:00:00+00:00"
    assert t.reviewed_datetime() is not None
    assert t.note == "waiting on CI"
    assert t.label == "auth work"

    store.clear_reviewed(SID1)
    assert Store.load(path).sessions[SID1].reviewed_at == ""


def test_mutations_on_unknown_session_are_noops(tmp_path: Path):
    store = Store.load(tmp_path / "state.json")
    store.mark_reviewed(SID1, "2026-08-17T12:00:00+00:00")
    store.set_note(SID1, "x")
    store.set_label(SID1, "y")
    assert store.sessions == {}


def test_save_writes_valid_json_atomically(tmp_path: Path):
    path = tmp_path / "deep" / "state.json"
    store = Store.load(path)
    store.track(SID1, "/proj/alpha", "2026-08-17T10:00:00+00:00")
    data = json.loads(path.read_text("utf-8"))
    assert data["version"] == 1
    assert not path.with_suffix(".json.tmp").exists()


def test_default_store_path_respects_xdg(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert default_store_path() == tmp_path / "cagents" / "state.json"
    monkeypatch.delenv("XDG_DATA_HOME")
    assert str(default_store_path()).endswith(".local/share/cagents/state.json")
