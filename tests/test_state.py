"""State persistence tests. Atomicity matters here — a truncated state.json is how a
GC run loses track of what's referenced."""

from __future__ import annotations

from pathlib import Path

import pytest

from dzsm.state import State, StateError, load_state, save_state


def test_missing_state_file_is_a_fresh_install(tmp_path: Path) -> None:
    state = load_state(tmp_path / "state.json")
    assert state.versions == {}
    assert state.pins == {}


def test_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = State()
    state.record_version(1559212036, 1721005200, size_bytes=1234)
    state.pins["chernarus"] = {"CF": 1721005200}
    save_state(path, state)

    loaded = load_state(path)
    assert loaded.known_versions(1559212036) == [1721005200]
    assert loaded.pins["chernarus"]["CF"] == 1721005200


def test_known_versions_returns_a_list_never_false(tmp_path: Path) -> None:
    """Rule 1: collection-returning functions return collections."""
    state = State()
    assert state.known_versions(999) == []
    assert state.latest_version(999) is None


def test_versions_sort_newest_first() -> None:
    state = State()
    state.record_version(1, 1719878400)
    state.record_version(1, 1721005200)
    assert state.known_versions(1) == [1721005200, 1719878400]
    assert state.latest_version(1) == 1721005200


def test_corrupt_state_raises(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(StateError):
        load_state(path)


def test_save_leaves_no_temp_files(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    save_state(path, State())
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]
