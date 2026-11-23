"""Durable local session storage with atomic writes and manual pin preservation."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterator
from pathlib import Path

from compact_ledger.models import (
    CompactionSnapshot,
    PinEntry,
    make_pin_id,
    normalize_pin_text,
    sanitize_unicode,
    utc_now_iso,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore[assignment]

ENV_STORE_DIR = "COMPACT_LEDGER_DIR"
DEFAULT_DIR_NAME = ".compact-ledger"
SESSIONS_SUBDIR = "sessions"

_PLAIN_SAFE_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,127}")
_HASH_MARKER = "--h_"
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }
)


class CorruptSnapshotError(ValueError):
    """Raised when a persisted session snapshot cannot be parsed."""


def sanitize_session_id(session_id: str) -> str:
    """Convert an arbitrary session ID into a collision-free, path-safe filename stem.

    Lowercase ASCII identifiers matching ``[a-z0-9][a-z0-9_-]{0,127}`` (excluding
    reserved names and the internal hash marker) are kept verbatim for readability.
    All other valid identifiers (including mixed-case strings, trailing newlines,
    paths, unicode, lone surrogates, or long strings) are deterministically mapped to
    ``<safe_prefix>--h_<sha256[:24]>``. Because the two domains are disjoint and all
    output characters are lowercase ASCII ``[a-z0-9_-]``, distinct ``session_id``
    values never collide — even on case-insensitive filesystems — and can never escape
    the sessions directory.
    """
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("session_id must be a non-empty string")
    if "\x00" in session_id:
        raise ValueError("session_id must not contain null bytes")

    if (
        _PLAIN_SAFE_RE.fullmatch(session_id)
        and _HASH_MARKER not in session_id
        and session_id not in _WINDOWS_RESERVED_NAMES
    ):
        return session_id

    # Use surrogatepass so lone UTF-16 surrogates never crash hashing and remain distinct.
    digest = hashlib.sha256(session_id.encode("utf-8", errors="surrogatepass")).hexdigest()[:24]
    prefix = re.sub(r"[^a-z0-9_-]+", "_", sanitize_unicode(session_id).lower())
    prefix = prefix.replace(_HASH_MARKER, "_h_").strip("_-")[:48]
    if not prefix or prefix in _WINDOWS_RESERVED_NAMES:
        prefix = "session"
    return f"{prefix}{_HASH_MARKER}{digest}"


def _merge_manual_pins(
    existing_manual: list[PinEntry],
    incoming_pins: list[PinEntry],
) -> list[PinEntry]:
    """Merge existing on-disk manual pins with incoming pins.

    - Any existing on-disk manual pin omitted from ``incoming_pins`` is preserved.
    - When ``incoming_pins`` contains a manual pin matching an existing manual pin
      by ``pin_id`` or normalized text, the **incoming** pin wins so callers (such as
      override engines) can update ``superseded_by``, ``supersedes``, or ``tags``.
    - Deduplication preserves order and prevents duplicate manual entries.
    """
    incoming_manual_by_id: dict[str, PinEntry] = {}
    incoming_manual_by_text: dict[str, PinEntry] = {}
    for pin in incoming_pins:
        if pin.source == "manual":
            norm = normalize_pin_text(pin.text)
            if not norm:
                continue
            incoming_manual_by_id[pin.pin_id] = pin
            incoming_manual_by_text[norm] = pin

    merged_manual: list[PinEntry] = []
    seen_manual_texts: set[str] = set()
    seen_ids: set[str] = set()

    def _append_manual(pin: PinEntry) -> None:
        norm = normalize_pin_text(pin.text)
        if not norm or norm in seen_manual_texts or pin.pin_id in seen_ids:
            return
        seen_manual_texts.add(norm)
        seen_ids.add(pin.pin_id)
        merged_manual.append(pin)

    for old_pin in existing_manual:
        if old_pin.source != "manual":
            continue
        norm = normalize_pin_text(old_pin.text)
        updated = incoming_manual_by_id.get(old_pin.pin_id) or incoming_manual_by_text.get(norm)
        _append_manual(updated if updated is not None else old_pin)

    non_manual: list[PinEntry] = []
    for pin in incoming_pins:
        if pin.source == "manual":
            _append_manual(pin)
        else:
            if pin.pin_id in seen_ids:
                continue
            seen_ids.add(pin.pin_id)
            non_manual.append(pin)

    return merged_manual + non_manual


class SessionStore:
    """Local JSON snapshot store rooted at ``<base_dir>/.compact-ledger/sessions``."""

    def __init__(
        self,
        store_dir: str | Path | None = None,
        *,
        base_dir: str | Path | None = None,
    ) -> None:
        if store_dir is not None and base_dir is not None:
            raise ValueError("Pass at most one of store_dir or base_dir")

        if store_dir is not None:
            raw_store = str(store_dir)
            if not raw_store.strip():
                raise ValueError("store_dir must be a non-empty path")
            self.root_dir = Path(store_dir).expanduser()
        elif base_dir is not None:
            raw_base = str(base_dir)
            if not raw_base.strip():
                raise ValueError("base_dir must be a non-empty path")
            self.root_dir = Path(base_dir).expanduser() / DEFAULT_DIR_NAME
        else:
            env_dir = os.environ.get(ENV_STORE_DIR)
            if env_dir and env_dir.strip():
                self.root_dir = Path(env_dir.strip()).expanduser()
            else:
                self.root_dir = Path.cwd() / DEFAULT_DIR_NAME

        self.sessions_dir = self.root_dir / SESSIONS_SUBDIR

    def _reject_symlinks(self, target: Path | None = None) -> None:
        for path in (self.root_dir, self.sessions_dir):
            if path.is_symlink():
                raise ValueError(f"Refusing to use symlinked store directory: {path}")
        if target is not None and target.is_symlink():
            raise ValueError(f"Refusing to operate on symlinked session file: {target}")

    def session_path(self, session_id: str) -> Path:
        """Return the safe absolute path for a session's snapshot file."""
        self._reject_symlinks()
        safe_id = sanitize_session_id(session_id)
        sessions_resolved = self.sessions_dir.resolve()
        candidate = (sessions_resolved / f"{safe_id}.json").resolve()
        if candidate.parent != sessions_resolved:
            raise ValueError(f"Unsafe session path resolved outside store: {session_id!r}")
        if candidate.is_symlink():
            raise ValueError(f"Refusing to operate on symlinked session file: {candidate}")
        return candidate

    @contextlib.contextmanager
    def _session_lock(self, session_id: str) -> Iterator[None]:
        self._reject_symlinks()
        safe_id = sanitize_session_id(session_id)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.sessions_dir / f".{safe_id}.lock"
        if fcntl is None:
            yield
            return
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _quarantine_corrupt_file(self, target: Path) -> None:
        backup = target.with_name(f"{target.name}.corrupt")
        with contextlib.suppress(OSError):
            os.replace(target, backup)

    def _load_unlocked(self, session_id: str, target: Path) -> CompactionSnapshot | None:
        self._reject_symlinks(target)
        if not target.is_file():
            return None
        try:
            raw = target.read_text(encoding="utf-8")
            return CompactionSnapshot.from_json(raw)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise CorruptSnapshotError(
                f"Failed to load snapshot for session {session_id!r}: {exc}"
            ) from exc

    def load_snapshot(self, session_id: str) -> CompactionSnapshot | None:
        """Load the persisted snapshot for ``session_id``, or ``None`` if absent."""
        target = self.session_path(session_id)
        return self._load_unlocked(session_id, target)

    def save_snapshot(self, snapshot: CompactionSnapshot) -> CompactionSnapshot:
        """Atomically persist ``snapshot``, preserving and updating manual pins."""
        target = self.session_path(snapshot.session_id)
        with self._session_lock(snapshot.session_id):
            existing_manual: list[PinEntry] = []
            if target.is_file():
                try:
                    existing = self._load_unlocked(snapshot.session_id, target)
                    if existing is not None:
                        existing_manual = [p for p in existing.pins if p.source == "manual"]
                except CorruptSnapshotError:
                    self._quarantine_corrupt_file(target)
                    existing_manual = []

            merged_pins = _merge_manual_pins(existing_manual, snapshot.pins)
            merged_snapshot = CompactionSnapshot(
                session_id=sanitize_unicode(snapshot.session_id),
                created_at=snapshot.created_at,
                transcript_path=snapshot.transcript_path,
                pins=merged_pins,
                attempts=list(snapshot.attempts),
                trajectories=list(snapshot.trajectories),
                thrash_entries=list(snapshot.thrash_entries),
                modified_files=list(snapshot.modified_files),
                schema_version=snapshot.schema_version,
                metadata=dict(snapshot.metadata),
            )

            self._atomic_write_json(target, merged_snapshot.to_json())
            return merged_snapshot

    def add_manual_pin(
        self,
        session_id: str,
        text: str,
        *,
        tags: list[str] | None = None,
        is_negative: bool = False,
        timestamp: str | None = None,
    ) -> PinEntry:
        """Add and immediately persist a manual pin for ``session_id``.

        If a manual pin with identical normalized text already exists, returns the
        existing pin (merging any newly supplied tags) without duplicating it.
        """
        clean_text = sanitize_unicode(text)
        normalized = normalize_pin_text(clean_text)
        if not normalized:
            raise ValueError("Pin text must be non-empty")

        target = self.session_path(session_id)
        clean_tags = list(
            dict.fromkeys(sanitize_unicode(t).strip() for t in (tags or []) if t.strip())
        )

        with self._session_lock(session_id):
            try:
                snapshot = self._load_unlocked(session_id, target)
            except CorruptSnapshotError:
                self._quarantine_corrupt_file(target)
                snapshot = None

            if snapshot is None:
                snapshot = CompactionSnapshot(session_id=sanitize_unicode(session_id))

            for existing in snapshot.pins:
                if existing.source == "manual" and normalize_pin_text(existing.text) == normalized:
                    updated = False
                    for tag in clean_tags:
                        if tag not in existing.tags:
                            existing.tags.append(tag)
                            updated = True
                    if is_negative and not existing.is_negative:
                        existing.is_negative = True
                        updated = True
                    if updated:
                        self._atomic_write_json(target, snapshot.to_json())
                    return existing

            pin = PinEntry(
                pin_id=make_pin_id("manual", normalized),
                text=clean_text.strip(),
                source="manual",
                turn_index=None,
                timestamp=timestamp or utc_now_iso(),
                is_negative=is_negative,
                tags=clean_tags,
            )
            existing_manual = [p for p in snapshot.pins if p.source == "manual"]
            snapshot.pins = _merge_manual_pins(existing_manual, [*snapshot.pins, pin])
            self._atomic_write_json(target, snapshot.to_json())
            return pin

    def remove_manual_pin(self, session_id: str, pin_id_or_text: str) -> bool:
        """Remove a manual pin by exact ``pin_id`` or normalized text.

        Returns ``True`` if a manual pin was removed, ``False`` otherwise.
        """
        query = sanitize_unicode(pin_id_or_text).strip()
        if not query:
            raise ValueError("pin_id_or_text must be non-empty")
        norm_query = normalize_pin_text(query)

        target = self.session_path(session_id)
        with self._session_lock(session_id):
            snapshot = self._load_unlocked(session_id, target)
            if snapshot is None:
                return False

            new_pins: list[PinEntry] = []
            removed = False
            for pin in snapshot.pins:
                if pin.source == "manual" and (
                    pin.pin_id == query or normalize_pin_text(pin.text) == norm_query
                ):
                    removed = True
                    continue
                new_pins.append(pin)

            if not removed:
                return False

            snapshot.pins = new_pins
            self._atomic_write_json(target, snapshot.to_json())
            return True

    def list_manual_pins(self, session_id: str) -> list[PinEntry]:
        """Return all persisted manual pins for ``session_id``."""
        snapshot = self.load_snapshot(session_id)
        if snapshot is None:
            return []
        return [p for p in snapshot.pins if p.source == "manual"]

    def clear_session(self, session_id: str) -> bool:
        """Delete the stored snapshot for ``session_id``. Returns True if removed."""
        path = self.session_path(session_id)
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False

    def list_sessions(self) -> list[str]:
        """Return sorted session IDs for all valid stored sessions."""
        self._reject_symlinks()
        if not self.sessions_dir.is_dir():
            return []

        session_ids: set[str] = set()
        for entry in sorted(self.sessions_dir.glob("*.json")):
            if entry.is_symlink() or not entry.is_file() or entry.name.startswith("."):
                continue
            try:
                data = json.loads(entry.read_text(encoding="utf-8"))
                sid = data.get("session_id") if isinstance(data, dict) else None
                if (
                    isinstance(sid, str)
                    and sid.strip()
                    and "\x00" not in sid
                    and sanitize_session_id(sid) == entry.stem
                ):
                    session_ids.add(sid)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
                continue
        return sorted(session_ids)

    def _atomic_write_json(self, target: Path, payload: str) -> None:
        self._reject_symlinks(target)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        safe_payload = sanitize_unicode(payload)
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.sessions_dir,
                prefix=f".{target.stem}.",
                suffix=".tmp",
                delete=False,
            ) as tmp_file:
                tmp_path = Path(tmp_file.name)
                tmp_file.write(safe_payload)
                if not safe_payload.endswith("\n"):
                    tmp_file.write("\n")
                tmp_file.flush()
                os.fsync(tmp_file.fileno())
            os.replace(tmp_path, target)
            tmp_path = None
            self._fsync_dir()
        finally:
            if tmp_path is not None:
                with contextlib.suppress(OSError):
                    tmp_path.unlink()

    def _fsync_dir(self) -> None:
        if not hasattr(os, "O_RDONLY"):
            return
        with contextlib.suppress(OSError):
            dir_fd = os.open(self.sessions_dir, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
