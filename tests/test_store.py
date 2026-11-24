"""Tests for compact_ledger.store."""

from __future__ import annotations

from pathlib import Path

import pytest

from compact_ledger.models import CompactionSnapshot, PinEntry
from compact_ledger.store import (
    ENV_STORE_DIR,
    CorruptSnapshotError,
    SessionStore,
    sanitize_session_id,
)


def test_store_directory_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_STORE_DIR, raising=False)
    monkeypatch.chdir(tmp_path)

    default_store = SessionStore()
    assert default_store.root_dir == tmp_path / ".compact-ledger"
    assert default_store.sessions_dir == tmp_path / ".compact-ledger" / "sessions"

    base_dir = tmp_path / "project"
    base_store = SessionStore(base_dir=base_dir)
    assert base_store.root_dir == base_dir / ".compact-ledger"

    env_dir = tmp_path / "from-env"
    monkeypatch.setenv(ENV_STORE_DIR, str(env_dir))
    env_store = SessionStore()
    assert env_store.root_dir == env_dir

    explicit_dir = tmp_path / "explicit"
    explicit_store = SessionStore(store_dir=explicit_dir)
    assert explicit_store.root_dir == explicit_dir

    with pytest.raises(ValueError, match="at most one"):
        SessionStore(store_dir=explicit_dir, base_dir=base_dir)


def test_sanitize_session_id_prevents_traversal_and_collisions(tmp_path: Path) -> None:
    store = SessionStore(store_dir=tmp_path)
    sessions_root = store.sessions_dir.resolve()

    # Plain readable IDs stay clean
    assert sanitize_session_id("abc-123_def") == "abc-123_def"

    # Distinct adversarial IDs must stay inside sessions_dir and never collide
    candidates = [
        "../../etc/passwd",
        "../evil",
        "/etc/passwd",
        ".",
        "..",
        "a/b",
        "a_b",
        "a\\b",
        "SessionA",
        "sessiona",
        "con",
        "CON",
        "nul",
        "com1",
        "a" * 300 + "1",
        "a" * 300 + "2",
        "unicode-session-\u2603",
        "spaces in id",
        "abc\n",
        "surrogate-\ud83d",
        "surrogate-\ud83e",
    ]
    # Also test hash-marker spoofing collision attempt
    spoof_target = sanitize_session_id("a/b")
    candidates.append(spoof_target)

    mapped: dict[str, str] = {}
    for sid in candidates:
        safe = sanitize_session_id(sid)
        assert "/" not in safe
        assert "\\" not in safe
        assert ".." not in safe
        assert "\n" not in safe
        assert len(safe) <= 128
        path = store.session_path(sid)
        assert path.parent == sessions_root
        assert safe not in mapped.values(), f"Collision for {sid!r} -> {safe!r}"
        mapped[sid] = safe

    # Reject empty / whitespace / null bytes
    for bad in ("", "   ", "\t\n", "abc\x00def"):
        with pytest.raises(ValueError):
            sanitize_session_id(bad)


def test_atomic_write_and_failure_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = SessionStore(store_dir=tmp_path)
    original = CompactionSnapshot(
        session_id="sess-atomic",
        pins=[PinEntry(pin_id="pin-auto-1", text="Initial constraint", source="auto")],
    )
    store.save_snapshot(original)
    target = store.session_path("sess-atomic")
    assert target.is_file()
    assert list(store.sessions_dir.glob("*.tmp")) == []

    # Simulate os.replace failure mid-write: original file survives and temp file is cleaned
    def _boom(src: object, dst: object) -> None:
        raise OSError("Simulated disk failure")

    monkeypatch.setattr("compact_ledger.store.os.replace", _boom)
    updated = CompactionSnapshot(
        session_id="sess-atomic",
        pins=[PinEntry(pin_id="pin-auto-2", text="New constraint", source="auto")],
    )
    with pytest.raises(OSError, match="Simulated disk failure"):
        store.save_snapshot(updated)

    # No orphaned temp files and original content untouched
    assert list(store.sessions_dir.glob("*.tmp")) == []
    loaded = store.load_snapshot("sess-atomic")
    assert loaded is not None
    assert [p.text for p in loaded.pins] == ["Initial constraint"]


def test_manual_pin_persistence_and_deduplication_across_snapshots(tmp_path: Path) -> None:
    store = SessionStore(store_dir=tmp_path)

    # Manual pin added before any snapshot exists
    p1 = store.add_manual_pin("s1", "  Never modify prod schema  ", tags=["db"])
    assert p1.source == "manual"
    assert p1.text == "Never modify prod schema"
    assert p1.tags == ["db"]

    # Adding same text with extra whitespace deduplicates and merges tags
    p1_dup = store.add_manual_pin("s1", "Never   modify\tprod schema", tags=["db", "critical"])
    assert p1_dup.pin_id == p1.pin_id
    assert p1_dup.tags == ["db", "critical"]
    assert len(store.list_manual_pins("s1")) == 1

    # Add a second distinct manual pin
    store.add_manual_pin("s1", "Always keep zero runtime dependencies", is_negative=False)
    assert len(store.list_manual_pins("s1")) == 2

    # Save a snapshot containing ONLY auto pins -> manual pins must survive
    snap1 = CompactionSnapshot(
        session_id="s1",
        pins=[
            PinEntry(pin_id="pin-auto-1", text="Use July 31 deadline", source="auto", turn_index=1),
        ],
        modified_files=["src/app.py"],
    )
    saved1 = store.save_snapshot(snap1)
    assert [p.text for p in saved1.pins if p.source == "manual"] == [
        "Never modify prod schema",
        "Always keep zero runtime dependencies",
    ]
    assert [p.text for p in saved1.pins if p.source == "auto"] == ["Use July 31 deadline"]

    # Save a second snapshot that repeats one manual pin and adds a new manual pin + new auto pin
    p3 = PinEntry(pin_id="pin-manual-extra", text="Keep hooks fail-open", source="manual")
    snap2 = CompactionSnapshot(
        session_id="s1",
        pins=[
            p1,
            p3,
            PinEntry(pin_id="pin-auto-2", text="Switch to UTC", source="auto", turn_index=2),
        ],
        modified_files=["src/hooks.py"],
    )
    saved2 = store.save_snapshot(snap2)
    manual_texts = [p.text for p in saved2.pins if p.source == "manual"]
    assert manual_texts == [
        "Never modify prod schema",
        "Always keep zero runtime dependencies",
        "Keep hooks fail-open",
    ]
    assert [p.text for p in saved2.pins if p.source == "auto"] == ["Switch to UTC"]
    assert saved2.modified_files == ["src/hooks.py"]

    # Reload from disk confirms persistence
    reloaded = store.load_snapshot("s1")
    assert reloaded == saved2
    assert len(store.list_manual_pins("s1")) == 3

    # Empty pin rejected
    with pytest.raises(ValueError, match="non-empty"):
        store.add_manual_pin("s1", "   ")


def test_list_clear_and_corrupt_snapshot_handling(tmp_path: Path) -> None:
    store = SessionStore(store_dir=tmp_path)
    assert store.list_sessions() == []
    assert store.load_snapshot("missing") is None
    assert store.list_manual_pins("missing") == []
    assert store.clear_session("missing") is False

    store.save_snapshot(CompactionSnapshot(session_id="sess-b"))
    store.save_snapshot(CompactionSnapshot(session_id="Feature/Branch-A"))
    assert store.list_sessions() == ["Feature/Branch-A", "sess-b"]

    # Corrupt file raises CorruptSnapshotError on load, is skipped in list_sessions,
    # and can be overwritten cleanly by save_snapshot.
    corrupt_path = store.session_path("sess-corrupt")
    corrupt_path.write_text("{corrupt json", encoding="utf-8")
    assert store.list_sessions() == ["Feature/Branch-A", "sess-b"]

    with pytest.raises(CorruptSnapshotError):
        store.load_snapshot("sess-corrupt")

    recovered = store.save_snapshot(CompactionSnapshot(session_id="sess-corrupt"))
    assert recovered.session_id == "sess-corrupt"
    assert store.load_snapshot("sess-corrupt") is not None
    # Corrupt file was quarantined with a unique timestamped backup rather than silently destroyed
    backups = list(store.sessions_dir.glob(f"{corrupt_path.name}.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].is_file()
    assert store.last_quarantined_path == backups[0]

    assert store.clear_session("sess-b") is True
    assert store.clear_session("sess-b") is False
    assert "sess-b" not in store.list_sessions()


def test_save_snapshot_allows_updating_existing_manual_pin_and_remove_manual_pin(
    tmp_path: Path,
) -> None:
    store = SessionStore(store_dir=tmp_path)
    pin = store.add_manual_pin("s1", "Use June 30 deadline", tags=["deadline"])

    # PR 2 loads snapshot, marks manual pin as superseded, adds a tag, and saves
    loaded = store.load_snapshot("s1")
    assert loaded is not None
    loaded.pins[0].superseded_by = "pin-override-99"
    loaded.pins[0].tags.append("superseded")
    loaded.pins.append(
        PinEntry(
            pin_id="pin-override-99",
            text="Actually use July 31",
            source="override",
            supersedes=pin.pin_id,
        )
    )
    store.save_snapshot(loaded)

    reloaded = store.load_snapshot("s1")
    assert reloaded is not None
    manual = next(p for p in reloaded.pins if p.pin_id == pin.pin_id)
    assert manual.superseded_by == "pin-override-99"
    assert "superseded" in manual.tags

    # Explicit removal works and does not resurrect
    assert store.remove_manual_pin("s1", pin.pin_id) is True
    assert store.remove_manual_pin("s1", pin.pin_id) is False
    assert store.list_manual_pins("s1") == []


def test_store_resilience_to_binary_files_mismatched_ids_and_symlinks(tmp_path: Path) -> None:
    store = SessionStore(store_dir=tmp_path / "store")
    store.save_snapshot(CompactionSnapshot(session_id="valid-1"))

    # Non-UTF-8 binary file in sessions_dir must not crash list_sessions
    (store.sessions_dir / "binary.json").write_bytes(b"\xff\xfe\x00\x01")
    # Mismatched embedded session_id must not create duplicates or spoof entries
    (store.sessions_dir / "other.json").write_text('{"session_id": "valid-1"}', encoding="utf-8")
    assert store.list_sessions() == ["valid-1"]

    # Symlinked sessions directory is rejected
    real_target = tmp_path / "elsewhere"
    real_target.mkdir()
    symlink_root = tmp_path / "sym-store"
    symlink_root.mkdir()
    (symlink_root / "sessions").symlink_to(real_target)
    sym_store = SessionStore(store_dir=symlink_root)
    with pytest.raises(ValueError, match="symlinked"):
        sym_store.save_snapshot(CompactionSnapshot(session_id="s1"))


def test_store_handles_lone_surrogates_in_pin_and_session_id(tmp_path: Path) -> None:
    store = SessionStore(store_dir=tmp_path)
    pin = store.add_manual_pin("sess-\ud83d", "Keep emoji \ud83d safe")
    assert "\ufffd" in pin.text
    loaded = store.load_snapshot("sess-\ud83d")
    assert loaded is not None
    assert loaded.pins[0].text == pin.text
