"""Core data models for compact-ledger.

All models use only the Python standard library and implement strict JSON-safe
``to_dict()`` / ``from_dict()`` round-trip serialization.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

SCHEMA_VERSION = 1

FileOperation = Literal["edit", "write", "delete"]
PinSource = Literal["manual", "auto", "override"]
TestOutcome = Literal["failed", "passed", "error"]
TrajectoryStatus = Literal[
    "PROGRESSING",
    "OSCILLATING",
    "STALLED",
    "RESOLVED",
    "SINGLE_FAILURE",
]

VALID_FILE_OPERATIONS: frozenset[str] = frozenset({"edit", "write", "delete"})
VALID_PIN_SOURCES: frozenset[str] = frozenset({"manual", "auto", "override"})
VALID_TEST_OUTCOMES: frozenset[str] = frozenset({"failed", "passed", "error"})
VALID_TRAJECTORY_STATUSES: frozenset[str] = frozenset(
    {"PROGRESSING", "OSCILLATING", "STALLED", "RESOLVED", "SINGLE_FAILURE"}
)


def utc_now_iso() -> str:
    """Return the current UTC timestamp in ISO-8601 format."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


def sanitize_unicode(text: str) -> str:
    """Replace lone UTF-16 surrogates with U+FFFD so UTF-8 encoding never fails."""
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        return _SURROGATE_RE.sub("\ufffd", text)


def normalize_pin_text(text: str) -> str:
    """Normalize pin text for deterministic deduplication while preserving case."""
    return " ".join(sanitize_unicode(text).split())


def make_pin_id(source: str, text: str, turn_index: int | None = None) -> str:
    """Generate a deterministic, collision-resistant pin identifier."""
    normalized = normalize_pin_text(text)
    payload = f"{source}:{turn_index if turn_index is not None else ''}:{normalized}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"pin-{source}-{digest}"


def _require_mapping(data: Any, model_name: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError(f"{model_name}.from_dict expected a dict, got {type(data).__name__}")
    return data


def _require_str(data: dict[str, Any], key: str, model_name: str) -> str:
    val = data.get(key)
    if not isinstance(val, str):
        raise ValueError(f"{model_name}.{key} must be a str")
    return sanitize_unicode(val)


def _optional_str(data: dict[str, Any], key: str, model_name: str) -> str | None:
    val = data.get(key)
    if val is None:
        return None
    if not isinstance(val, str):
        raise ValueError(f"{model_name}.{key} must be a str or None")
    return sanitize_unicode(val)


def _require_int(data: dict[str, Any], key: str, model_name: str) -> int:
    val = data.get(key)
    if isinstance(val, bool) or not isinstance(val, int):
        raise ValueError(f"{model_name}.{key} must be an int")
    return val


def _optional_int(data: dict[str, Any], key: str, model_name: str) -> int | None:
    val = data.get(key)
    if val is None:
        return None
    if isinstance(val, bool) or not isinstance(val, int):
        raise ValueError(f"{model_name}.{key} must be an int or None")
    return val


def _default_int(data: dict[str, Any], key: str, default: int, model_name: str) -> int:
    val = data.get(key, default)
    if isinstance(val, bool) or not isinstance(val, int):
        raise ValueError(f"{model_name}.{key} must be an int")
    return val


def _optional_bool(data: dict[str, Any], key: str, default: bool, model_name: str) -> bool:
    val = data.get(key, default)
    if not isinstance(val, bool):
        raise ValueError(f"{model_name}.{key} must be a bool")
    return val


def _str_list(data: dict[str, Any], key: str, model_name: str) -> list[str]:
    val = data.get(key)
    if val is None:
        return []
    if not isinstance(val, list) or not all(isinstance(item, str) for item in val):
        raise ValueError(f"{model_name}.{key} must be a list of str")
    return [sanitize_unicode(item) for item in val]


def _int_list(data: dict[str, Any], key: str, model_name: str) -> list[int]:
    val = data.get(key)
    if val is None:
        return []
    if not isinstance(val, list) or not all(
        isinstance(item, int) and not isinstance(item, bool) for item in val
    ):
        raise ValueError(f"{model_name}.{key} must be a list of int")
    return list(val)


@dataclass(slots=True)
class UserTurn:
    """A user message extracted from an agent session transcript."""

    turn_index: int
    text: str
    timestamp: str | None = None
    source: str = "transcript"
    seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_index": self.turn_index,
            "text": self.text,
            "timestamp": self.timestamp,
            "source": self.source,
            "seq": self.seq,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UserTurn:
        d = _require_mapping(data, "UserTurn")
        return cls(
            turn_index=_require_int(d, "turn_index", "UserTurn"),
            text=_require_str(d, "text", "UserTurn"),
            timestamp=_optional_str(d, "timestamp", "UserTurn"),
            source=_optional_str(d, "source", "UserTurn") or "transcript",
            seq=_default_int(d, "seq", 0, "UserTurn"),
        )


@dataclass(slots=True)
class ToolExecution:
    """A tool or shell command invocation paired with its output."""

    turn_index: int
    tool_name: str
    command: str | None = None
    output: str = ""
    exit_code: int | None = None
    is_error: bool = False
    timestamp: str | None = None
    call_id: str | None = None
    seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_index": self.turn_index,
            "tool_name": self.tool_name,
            "command": self.command,
            "output": self.output,
            "exit_code": self.exit_code,
            "is_error": self.is_error,
            "timestamp": self.timestamp,
            "call_id": self.call_id,
            "seq": self.seq,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolExecution:
        d = _require_mapping(data, "ToolExecution")
        output = d.get("output", "")
        if not isinstance(output, str):
            raise ValueError("ToolExecution.output must be a str")
        return cls(
            turn_index=_require_int(d, "turn_index", "ToolExecution"),
            tool_name=_require_str(d, "tool_name", "ToolExecution"),
            command=_optional_str(d, "command", "ToolExecution"),
            output=sanitize_unicode(output),
            exit_code=_optional_int(d, "exit_code", "ToolExecution"),
            is_error=_optional_bool(d, "is_error", False, "ToolExecution"),
            timestamp=_optional_str(d, "timestamp", "ToolExecution"),
            call_id=_optional_str(d, "call_id", "ToolExecution"),
            seq=_default_int(d, "seq", 0, "ToolExecution"),
        )


@dataclass(slots=True)
class FileEdit:
    """A file modification recorded in a transcript."""

    turn_index: int
    path: str
    operation: FileOperation = "edit"
    old_text: str | None = None
    new_text: str | None = None
    timestamp: str | None = None
    call_id: str | None = None
    seq: int = 0

    def __post_init__(self) -> None:
        if self.operation not in VALID_FILE_OPERATIONS:
            raise ValueError(
                f"FileEdit.operation must be one of {sorted(VALID_FILE_OPERATIONS)}, "
                f"got {self.operation!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_index": self.turn_index,
            "path": self.path,
            "operation": self.operation,
            "old_text": self.old_text,
            "new_text": self.new_text,
            "timestamp": self.timestamp,
            "call_id": self.call_id,
            "seq": self.seq,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FileEdit:
        d = _require_mapping(data, "FileEdit")
        operation = d.get("operation", "edit")
        if not isinstance(operation, str) or operation not in VALID_FILE_OPERATIONS:
            raise ValueError(f"FileEdit.operation must be one of {sorted(VALID_FILE_OPERATIONS)}")
        return cls(
            turn_index=_require_int(d, "turn_index", "FileEdit"),
            path=_require_str(d, "path", "FileEdit"),
            operation=operation,  # type: ignore[arg-type]
            old_text=_optional_str(d, "old_text", "FileEdit"),
            new_text=_optional_str(d, "new_text", "FileEdit"),
            timestamp=_optional_str(d, "timestamp", "FileEdit"),
            call_id=_optional_str(d, "call_id", "FileEdit"),
            seq=_default_int(d, "seq", 0, "FileEdit"),
        )


@dataclass(slots=True)
class ParsedTranscript:
    """Structured representation of a parsed agent transcript."""

    format: str
    session_id: str | None = None
    user_turns: list[UserTurn] = field(default_factory=list)
    tool_executions: list[ToolExecution] = field(default_factory=list)
    file_edits: list[FileEdit] = field(default_factory=list)
    modified_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "session_id": self.session_id,
            "user_turns": [t.to_dict() for t in self.user_turns],
            "tool_executions": [t.to_dict() for t in self.tool_executions],
            "file_edits": [e.to_dict() for e in self.file_edits],
            "modified_files": list(self.modified_files),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ParsedTranscript:
        d = _require_mapping(data, "ParsedTranscript")
        raw_turns = d.get("user_turns", [])
        raw_tools = d.get("tool_executions", [])
        raw_edits = d.get("file_edits", [])
        if not isinstance(raw_turns, list):
            raise ValueError("ParsedTranscript.user_turns must be a list")
        if not isinstance(raw_tools, list):
            raise ValueError("ParsedTranscript.tool_executions must be a list")
        if not isinstance(raw_edits, list):
            raise ValueError("ParsedTranscript.file_edits must be a list")
        return cls(
            format=_require_str(d, "format", "ParsedTranscript"),
            session_id=_optional_str(d, "session_id", "ParsedTranscript"),
            user_turns=[UserTurn.from_dict(item) for item in raw_turns],
            tool_executions=[ToolExecution.from_dict(item) for item in raw_tools],
            file_edits=[FileEdit.from_dict(item) for item in raw_edits],
            modified_files=_str_list(d, "modified_files", "ParsedTranscript"),
        )


@dataclass(slots=True)
class PinEntry:
    """A pinned user constraint, directive, or superseding correction."""

    pin_id: str
    text: str
    source: PinSource = "auto"
    turn_index: int | None = None
    timestamp: str | None = None
    supersedes: str | None = None
    superseded_by: str | None = None
    is_negative: bool = False
    tags: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.source not in VALID_PIN_SOURCES:
            raise ValueError(
                f"PinEntry.source must be one of {sorted(VALID_PIN_SOURCES)}, got {self.source!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "pin_id": self.pin_id,
            "text": self.text,
            "source": self.source,
            "turn_index": self.turn_index,
            "timestamp": self.timestamp,
            "supersedes": self.supersedes,
            "superseded_by": self.superseded_by,
            "is_negative": self.is_negative,
            "tags": list(self.tags),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PinEntry:
        d = _require_mapping(data, "PinEntry")
        source = d.get("source", "auto")
        if not isinstance(source, str) or source not in VALID_PIN_SOURCES:
            raise ValueError(f"PinEntry.source must be one of {sorted(VALID_PIN_SOURCES)}")
        return cls(
            pin_id=_require_str(d, "pin_id", "PinEntry"),
            text=_require_str(d, "text", "PinEntry"),
            source=source,  # type: ignore[arg-type]
            turn_index=_optional_int(d, "turn_index", "PinEntry"),
            timestamp=_optional_str(d, "timestamp", "PinEntry"),
            supersedes=_optional_str(d, "supersedes", "PinEntry"),
            superseded_by=_optional_str(d, "superseded_by", "PinEntry"),
            is_negative=_optional_bool(d, "is_negative", False, "PinEntry"),
            tags=_str_list(d, "tags", "PinEntry"),
        )


@dataclass(slots=True)
class TestAttempt:
    """A single test execution observation parsed from a test runner."""

    __test__ = False

    test_id: str
    runner: str
    assertion_loc: str | None = None
    expected: str | None = None
    actual: str | None = None
    outcome: TestOutcome = "failed"
    turn_index: int = 0
    message: str | None = None
    command: str | None = None

    def __post_init__(self) -> None:
        if self.outcome not in VALID_TEST_OUTCOMES:
            raise ValueError(
                f"TestAttempt.outcome must be one of {sorted(VALID_TEST_OUTCOMES)}, "
                f"got {self.outcome!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "test_id": self.test_id,
            "runner": self.runner,
            "assertion_loc": self.assertion_loc,
            "expected": self.expected,
            "actual": self.actual,
            "outcome": self.outcome,
            "turn_index": self.turn_index,
            "message": self.message,
            "command": self.command,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TestAttempt:
        d = _require_mapping(data, "TestAttempt")
        outcome = d.get("outcome", "failed")
        if not isinstance(outcome, str) or outcome not in VALID_TEST_OUTCOMES:
            raise ValueError(f"TestAttempt.outcome must be one of {sorted(VALID_TEST_OUTCOMES)}")
        turn_index = _default_int(d, "turn_index", 0, "TestAttempt")
        return cls(
            test_id=_require_str(d, "test_id", "TestAttempt"),
            runner=_require_str(d, "runner", "TestAttempt"),
            assertion_loc=_optional_str(d, "assertion_loc", "TestAttempt"),
            expected=_optional_str(d, "expected", "TestAttempt"),
            actual=_optional_str(d, "actual", "TestAttempt"),
            outcome=outcome,  # type: ignore[arg-type]
            turn_index=turn_index,
            message=_optional_str(d, "message", "TestAttempt"),
            command=_optional_str(d, "command", "TestAttempt"),
        )


@dataclass(slots=True)
class TestTrajectory:
    """Classified sequence of attempts for a single test identifier."""

    __test__ = False

    test_id: str
    runner: str
    status: TrajectoryStatus
    attempts: list[TestAttempt] = field(default_factory=list)
    summary: str = ""

    def __post_init__(self) -> None:
        if self.status not in VALID_TRAJECTORY_STATUSES:
            raise ValueError(
                f"TestTrajectory.status must be one of {sorted(VALID_TRAJECTORY_STATUSES)}, "
                f"got {self.status!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "test_id": self.test_id,
            "runner": self.runner,
            "status": self.status,
            "attempts": [a.to_dict() for a in self.attempts],
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TestTrajectory:
        d = _require_mapping(data, "TestTrajectory")
        status = d.get("status")
        if not isinstance(status, str) or status not in VALID_TRAJECTORY_STATUSES:
            raise ValueError(
                f"TestTrajectory.status must be one of {sorted(VALID_TRAJECTORY_STATUSES)}"
            )
        raw_attempts = d.get("attempts", [])
        if not isinstance(raw_attempts, list):
            raise ValueError("TestTrajectory.attempts must be a list")
        summary = d.get("summary", "")
        if not isinstance(summary, str):
            raise ValueError("TestTrajectory.summary must be a str")
        return cls(
            test_id=_require_str(d, "test_id", "TestTrajectory"),
            runner=_require_str(d, "runner", "TestTrajectory"),
            status=status,  # type: ignore[arg-type]
            attempts=[TestAttempt.from_dict(item) for item in raw_attempts],
            summary=sanitize_unicode(summary),
        )


@dataclass(slots=True)
class EditThrashEntry:
    """Detected edit-failure thrashing or revert loop on a file."""

    path: str
    edit_count: int
    failure_count: int
    reverted: bool = False
    turn_indices: list[int] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "edit_count": self.edit_count,
            "failure_count": self.failure_count,
            "reverted": self.reverted,
            "turn_indices": list(self.turn_indices),
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EditThrashEntry:
        d = _require_mapping(data, "EditThrashEntry")
        reason = d.get("reason", "")
        if not isinstance(reason, str):
            raise ValueError("EditThrashEntry.reason must be a str")
        return cls(
            path=_require_str(d, "path", "EditThrashEntry"),
            edit_count=_require_int(d, "edit_count", "EditThrashEntry"),
            failure_count=_require_int(d, "failure_count", "EditThrashEntry"),
            reverted=_optional_bool(d, "reverted", False, "EditThrashEntry"),
            turn_indices=_int_list(d, "turn_indices", "EditThrashEntry"),
            reason=sanitize_unicode(reason),
        )


@dataclass(slots=True)
class CompactionSnapshot:
    """Complete persisted ledger state for a session across compaction."""

    session_id: str
    created_at: str = field(default_factory=utc_now_iso)
    transcript_path: str | None = None
    pins: list[PinEntry] = field(default_factory=list)
    attempts: list[TestAttempt] = field(default_factory=list)
    trajectories: list[TestTrajectory] = field(default_factory=list)
    thrash_entries: list[EditThrashEntry] = field(default_factory=list)
    modified_files: list[str] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "transcript_path": self.transcript_path,
            "pins": [p.to_dict() for p in self.pins],
            "attempts": [a.to_dict() for a in self.attempts],
            "trajectories": [t.to_dict() for t in self.trajectories],
            "thrash_entries": [e.to_dict() for e in self.thrash_entries],
            "modified_files": list(self.modified_files),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CompactionSnapshot:
        d = _require_mapping(data, "CompactionSnapshot")
        raw_pins = d.get("pins", [])
        raw_attempts = d.get("attempts", [])
        raw_trajectories = d.get("trajectories", [])
        raw_thrash = d.get("thrash_entries", [])
        raw_meta = d.get("metadata", {})
        if not isinstance(raw_pins, list):
            raise ValueError("CompactionSnapshot.pins must be a list")
        if not isinstance(raw_attempts, list):
            raise ValueError("CompactionSnapshot.attempts must be a list")
        if not isinstance(raw_trajectories, list):
            raise ValueError("CompactionSnapshot.trajectories must be a list")
        if not isinstance(raw_thrash, list):
            raise ValueError("CompactionSnapshot.thrash_entries must be a list")
        if raw_meta is None:
            raw_meta = {}
        if not isinstance(raw_meta, dict):
            raise ValueError("CompactionSnapshot.metadata must be a dict")

        schema_version = _default_int(d, "schema_version", SCHEMA_VERSION, "CompactionSnapshot")
        created_at = _optional_str(d, "created_at", "CompactionSnapshot") or utc_now_iso()

        return cls(
            session_id=_require_str(d, "session_id", "CompactionSnapshot"),
            created_at=created_at,
            transcript_path=_optional_str(d, "transcript_path", "CompactionSnapshot"),
            pins=[PinEntry.from_dict(item) for item in raw_pins],
            attempts=[TestAttempt.from_dict(item) for item in raw_attempts],
            trajectories=[TestTrajectory.from_dict(item) for item in raw_trajectories],
            thrash_entries=[EditThrashEntry.from_dict(item) for item in raw_thrash],
            modified_files=_str_list(d, "modified_files", "CompactionSnapshot"),
            schema_version=schema_version,
            metadata=dict(raw_meta),
        )

    def to_json(self, *, indent: int = 2) -> str:
        return sanitize_unicode(json.dumps(self.to_dict(), indent=indent, ensure_ascii=False))

    @classmethod
    def from_json(cls, raw: str) -> CompactionSnapshot:
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError, RecursionError) as exc:
            raise ValueError(f"Invalid snapshot JSON: {exc}") from exc
        return cls.from_dict(parsed)
