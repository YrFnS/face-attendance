import hashlib
import json
import math
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone


HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

EVENT_STATES = frozenset(
    {
        "legacy",
        "received",
        "processing",
        "source_verified",
        "validating",
        "recognizing",
        "processed",
        "checkin_created",
        "rejected",
        "failed",
        "uncertain",
        "quarantined",
        "dismissed",
    }
)
EVENT_REASON_CODES = frozenset(
    {
        "legacy_import",
        "receipt_recorded",
        "processing_started",
        "source_verified",
        "source_binding_invalid",
        "upload_too_large",
        "future_timestamp",
        "stale_event",
        "unreadable_image",
        "image_too_large",
        "image_validated",
        "no_face",
        "pad_face_limit",
        "pad_single_face_required",
        "pad_rejected",
        "recognition_started",
        "quality_rejected",
        "unknown_employee",
        "score_below_threshold",
        "margin_below_threshold",
        "duplicate_face",
        "accepted_candidate",
        "cooldown_suppressed",
        "checkin_created",
        "processed_no_checkin",
        "processing_failed",
        "invalid_upload",
        "unexpected_error",
        "generic_rejected",
        "generic_failed",
        "operator_action",
    }
)
ACTOR_TYPES = frozenset({"system", "watcher", "operator", "migration"})
RECEIPT_STATES = frozenset(
    {"pending", "verified", "route_only", "missing", "invalid", "legacy"}
)
RETENTION_STATES = frozenset(
    {
        "pending",
        "retained",
        "temporary",
        "not_retained",
        "deleted",
        "quarantined",
        "unknown",
    }
)
TERMINAL_EVENT_STATES = frozenset(
    {"processed", "checkin_created", "rejected", "failed", "uncertain", "dismissed"}
)


def _sql_values(values):
    return ", ".join("'" + value.replace("'", "''") + "'" for value in sorted(values))


_STATE_SQL = _sql_values(EVENT_STATES)
_REASON_SQL = _sql_values(EVENT_REASON_CODES)
_ACTOR_SQL = _sql_values(ACTOR_TYPES)
_RECEIPT_SQL = _sql_values(RECEIPT_STATES)
_RETENTION_SQL = _sql_values(RETENTION_STATES)


LEDGER_SCHEMA_STATEMENTS = (
    "ALTER TABLE camera_events ADD COLUMN capture_id TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE camera_events ADD COLUMN received_at TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE camera_events ADD COLUMN received_unix REAL NOT NULL DEFAULT 0",
    "ALTER TABLE camera_events ADD COLUMN transport_received_at TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE camera_events ADD COLUMN source_at TEXT",
    "ALTER TABLE camera_events ADD COLUMN source_time_provenance TEXT NOT NULL DEFAULT 'legacy'",
    "ALTER TABLE camera_events ADD COLUMN effective_at TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE camera_events ADD COLUMN branch TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE camera_events ADD COLUMN source_type TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE camera_events ADD COLUMN source_principal TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE camera_events ADD COLUMN source_remote_ip TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE camera_events ADD COLUMN source_binding_id TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE camera_events ADD COLUMN policy TEXT NOT NULL DEFAULT ''",
    f"ALTER TABLE camera_events ADD COLUMN receipt_state TEXT NOT NULL DEFAULT 'legacy' CHECK(receipt_state IN ({_RECEIPT_SQL}))",
    "ALTER TABLE camera_events ADD COLUMN receipt_verified INTEGER NOT NULL DEFAULT 0 CHECK(receipt_verified IN (0, 1))",
    "ALTER TABLE camera_events ADD COLUMN receipt_json TEXT NOT NULL DEFAULT '{}'",
    f"ALTER TABLE camera_events ADD COLUMN lifecycle_state TEXT NOT NULL DEFAULT 'legacy' CHECK(lifecycle_state IN ({_STATE_SQL}))",
    "ALTER TABLE camera_events ADD COLUMN state_version INTEGER NOT NULL DEFAULT 1 CHECK(state_version >= 1)",
    f"ALTER TABLE camera_events ADD COLUMN reason_code TEXT NOT NULL DEFAULT 'legacy_import' CHECK(reason_code IN ({_REASON_SQL}))",
    "ALTER TABLE camera_events ADD COLUMN gallery_version TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE camera_events ADD COLUMN gallery_generated_at TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE camera_events ADD COLUMN gallery_model TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE camera_events ADD COLUMN gallery_model_version TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE camera_events ADD COLUMN recognition_model TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE camera_events ADD COLUMN recognition_model_version TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE camera_events ADD COLUMN preprocessing_version TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE camera_events ADD COLUMN pad_provider TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE camera_events ADD COLUMN pad_model TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE camera_events ADD COLUMN policy_version TEXT NOT NULL DEFAULT ''",
    f"ALTER TABLE camera_events ADD COLUMN retention_state TEXT NOT NULL DEFAULT 'unknown' CHECK(retention_state IN ({_RETENTION_SQL}))",
    "ALTER TABLE camera_events ADD COLUMN final_disposition TEXT NOT NULL DEFAULT ''",
    """
    UPDATE camera_events
    SET capture_id = event_id,
        received_at = strftime('%Y-%m-%dT%H:%M:%fZ', created_unix, 'unixepoch'),
        received_unix = created_unix,
        source_at = CASE
            WHEN source_mtime IS NULL THEN NULL
            ELSE strftime('%Y-%m-%dT%H:%M:%fZ', source_mtime, 'unixepoch')
        END,
        source_time_provenance = 'legacy_filesystem_mtime',
        effective_at = strftime('%Y-%m-%dT%H:%M:%fZ', created_unix, 'unixepoch'),
        policy = log_type,
        lifecycle_state = CASE status
            WHEN 'processing' THEN 'processing'
            WHEN 'checkin_created' THEN 'checkin_created'
            WHEN 'processed_no_checkin' THEN 'processed'
            WHEN 'processed' THEN 'processed'
            WHEN 'rejected' THEN 'rejected'
            WHEN 'failed' THEN 'failed'
            WHEN 'uncertain' THEN 'uncertain'
            ELSE 'legacy'
        END,
        state_version = 1,
        reason_code = 'legacy_import',
        policy_version = 'legacy',
        retention_state = 'unknown',
        final_disposition = CASE
            WHEN completed_at IS NULL THEN ''
            ELSE status
        END
    """,
    f"""
    CREATE TABLE recognition_decisions (
        decision_id TEXT PRIMARY KEY,
        event_id TEXT NOT NULL REFERENCES camera_events(event_id) ON DELETE CASCADE,
        decision_version INTEGER NOT NULL CHECK(decision_version >= 1),
        face_index INTEGER NOT NULL CHECK(face_index >= 1),
        face_count INTEGER NOT NULL CHECK(face_count >= face_index),
        bbox_x1 INTEGER NOT NULL,
        bbox_y1 INTEGER NOT NULL,
        bbox_x2 INTEGER NOT NULL,
        bbox_y2 INTEGER NOT NULL,
        face_width REAL NOT NULL,
        face_height REAL NOT NULL,
        detection_score REAL NOT NULL,
        best_employee TEXT NOT NULL DEFAULT '',
        best_score REAL NOT NULL,
        runner_up_score REAL NOT NULL,
        score_margin REAL NOT NULL,
        pad_passed INTEGER NOT NULL CHECK(pad_passed IN (0, 1)),
        pad_skipped INTEGER NOT NULL CHECK(pad_skipped IN (0, 1)),
        pad_score REAL,
        pad_provider TEXT NOT NULL DEFAULT '',
        pad_model TEXT NOT NULL DEFAULT '',
        pad_evidence_id TEXT NOT NULL DEFAULT '',
        pad_binding_id TEXT NOT NULL DEFAULT '',
        accepted INTEGER NOT NULL CHECK(accepted IN (0, 1)),
        reason_code TEXT NOT NULL CHECK(reason_code IN ({_REASON_SQL})),
        candidate_log_type TEXT NOT NULL DEFAULT '',
        policy_version TEXT NOT NULL DEFAULT '',
        gallery_version TEXT NOT NULL DEFAULT '',
        gallery_generated_at TEXT NOT NULL DEFAULT '',
        gallery_model TEXT NOT NULL DEFAULT '',
        gallery_model_version TEXT NOT NULL DEFAULT '',
        recognition_model TEXT NOT NULL DEFAULT '',
        recognition_model_version TEXT NOT NULL DEFAULT '',
        preprocessing_version TEXT NOT NULL DEFAULT '',
        retention_state TEXT NOT NULL CHECK(retention_state IN ({_RETENTION_SQL})),
        created_at TEXT NOT NULL,
        UNIQUE(event_id, face_index, decision_version)
    )
    """,
    f"""
    CREATE TABLE event_transitions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL REFERENCES camera_events(event_id) ON DELETE CASCADE,
        sequence INTEGER NOT NULL CHECK(sequence >= 1),
        from_state TEXT NOT NULL DEFAULT '',
        to_state TEXT NOT NULL CHECK(to_state IN ({_STATE_SQL})),
        reason_code TEXT NOT NULL CHECK(reason_code IN ({_REASON_SQL})),
        actor_type TEXT NOT NULL CHECK(actor_type IN ({_ACTOR_SQL})),
        actor_id TEXT NOT NULL DEFAULT '',
        detail_json TEXT NOT NULL DEFAULT '{{}}',
        created_at TEXT NOT NULL,
        UNIQUE(event_id, sequence)
    )
    """,
    f"""
    CREATE TABLE operator_actions (
        action_id TEXT PRIMARY KEY,
        event_id TEXT NOT NULL REFERENCES camera_events(event_id) ON DELETE CASCADE,
        actor TEXT NOT NULL,
        action TEXT NOT NULL,
        reason_code TEXT NOT NULL CHECK(reason_code IN ({_REASON_SQL})),
        detail_json TEXT NOT NULL DEFAULT '{{}}',
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX recognition_decisions_event ON recognition_decisions(event_id, face_index)",
    "CREATE INDEX event_transitions_event ON event_transitions(event_id, sequence)",
    "CREATE INDEX operator_actions_event ON operator_actions(event_id, created_at)",
    "CREATE INDEX camera_events_capture_id ON camera_events(capture_id)",
    "CREATE INDEX camera_events_lifecycle ON camera_events(lifecycle_state, received_unix)",
    """
    INSERT INTO event_transitions (
        event_id, sequence, from_state, to_state, reason_code,
        actor_type, actor_id, detail_json, created_at
    )
    SELECT event_id, 1, '', lifecycle_state, 'legacy_import',
           'migration', 'schema-v2', '{}', received_at
    FROM camera_events
    """,
    """
    CREATE TRIGGER recognition_decisions_no_update
    BEFORE UPDATE ON recognition_decisions
    BEGIN
        SELECT RAISE(ABORT, 'recognition decisions are immutable');
    END
    """,
    """
    CREATE TRIGGER recognition_decisions_no_direct_delete
    BEFORE DELETE ON recognition_decisions
    WHEN EXISTS (SELECT 1 FROM camera_events WHERE event_id = OLD.event_id)
    BEGIN
        SELECT RAISE(ABORT, 'recognition decisions are append-only');
    END
    """,
    """
    CREATE TRIGGER event_transitions_no_update
    BEFORE UPDATE ON event_transitions
    BEGIN
        SELECT RAISE(ABORT, 'event transitions are immutable');
    END
    """,
    """
    CREATE TRIGGER event_transitions_no_direct_delete
    BEFORE DELETE ON event_transitions
    WHEN EXISTS (SELECT 1 FROM camera_events WHERE event_id = OLD.event_id)
    BEGIN
        SELECT RAISE(ABORT, 'event transitions are append-only');
    END
    """,
    """
    CREATE TRIGGER operator_actions_no_update
    BEFORE UPDATE ON operator_actions
    BEGIN
        SELECT RAISE(ABORT, 'operator actions are immutable');
    END
    """,
    """
    CREATE TRIGGER operator_actions_no_direct_delete
    BEFORE DELETE ON operator_actions
    WHEN EXISTS (SELECT 1 FROM camera_events WHERE event_id = OLD.event_id)
    BEGIN
        SELECT RAISE(ABORT, 'operator actions are append-only');
    END
    """,
)


LEDGER_REQUIRED_TABLE_COLUMNS = {
    "camera_events": {
        "capture_id": ("TEXT", True, 0),
        "received_at": ("TEXT", True, 0),
        "received_unix": ("REAL", True, 0),
        "transport_received_at": ("TEXT", True, 0),
        "source_at": ("TEXT", False, 0),
        "source_time_provenance": ("TEXT", True, 0),
        "effective_at": ("TEXT", True, 0),
        "branch": ("TEXT", True, 0),
        "source_type": ("TEXT", True, 0),
        "source_principal": ("TEXT", True, 0),
        "source_remote_ip": ("TEXT", True, 0),
        "source_binding_id": ("TEXT", True, 0),
        "policy": ("TEXT", True, 0),
        "receipt_state": ("TEXT", True, 0),
        "receipt_verified": ("INTEGER", True, 0),
        "receipt_json": ("TEXT", True, 0),
        "lifecycle_state": ("TEXT", True, 0),
        "state_version": ("INTEGER", True, 0),
        "reason_code": ("TEXT", True, 0),
        "gallery_version": ("TEXT", True, 0),
        "gallery_generated_at": ("TEXT", True, 0),
        "gallery_model": ("TEXT", True, 0),
        "gallery_model_version": ("TEXT", True, 0),
        "recognition_model": ("TEXT", True, 0),
        "recognition_model_version": ("TEXT", True, 0),
        "preprocessing_version": ("TEXT", True, 0),
        "pad_provider": ("TEXT", True, 0),
        "pad_model": ("TEXT", True, 0),
        "policy_version": ("TEXT", True, 0),
        "retention_state": ("TEXT", True, 0),
        "final_disposition": ("TEXT", True, 0),
    },
    "recognition_decisions": {
        "decision_id": ("TEXT", False, 1),
        "event_id": ("TEXT", True, 0),
        "decision_version": ("INTEGER", True, 0),
        "face_index": ("INTEGER", True, 0),
        "face_count": ("INTEGER", True, 0),
        "bbox_x1": ("INTEGER", True, 0),
        "bbox_y1": ("INTEGER", True, 0),
        "bbox_x2": ("INTEGER", True, 0),
        "bbox_y2": ("INTEGER", True, 0),
        "face_width": ("REAL", True, 0),
        "face_height": ("REAL", True, 0),
        "detection_score": ("REAL", True, 0),
        "best_employee": ("TEXT", True, 0),
        "best_score": ("REAL", True, 0),
        "runner_up_score": ("REAL", True, 0),
        "score_margin": ("REAL", True, 0),
        "pad_passed": ("INTEGER", True, 0),
        "pad_skipped": ("INTEGER", True, 0),
        "pad_score": ("REAL", False, 0),
        "pad_provider": ("TEXT", True, 0),
        "pad_model": ("TEXT", True, 0),
        "pad_evidence_id": ("TEXT", True, 0),
        "pad_binding_id": ("TEXT", True, 0),
        "accepted": ("INTEGER", True, 0),
        "reason_code": ("TEXT", True, 0),
        "candidate_log_type": ("TEXT", True, 0),
        "policy_version": ("TEXT", True, 0),
        "gallery_version": ("TEXT", True, 0),
        "gallery_generated_at": ("TEXT", True, 0),
        "gallery_model": ("TEXT", True, 0),
        "gallery_model_version": ("TEXT", True, 0),
        "recognition_model": ("TEXT", True, 0),
        "recognition_model_version": ("TEXT", True, 0),
        "preprocessing_version": ("TEXT", True, 0),
        "retention_state": ("TEXT", True, 0),
        "created_at": ("TEXT", True, 0),
    },
    "event_transitions": {
        "id": ("INTEGER", False, 1),
        "event_id": ("TEXT", True, 0),
        "sequence": ("INTEGER", True, 0),
        "from_state": ("TEXT", True, 0),
        "to_state": ("TEXT", True, 0),
        "reason_code": ("TEXT", True, 0),
        "actor_type": ("TEXT", True, 0),
        "actor_id": ("TEXT", True, 0),
        "detail_json": ("TEXT", True, 0),
        "created_at": ("TEXT", True, 0),
    },
    "operator_actions": {
        "action_id": ("TEXT", False, 1),
        "event_id": ("TEXT", True, 0),
        "actor": ("TEXT", True, 0),
        "action": ("TEXT", True, 0),
        "reason_code": ("TEXT", True, 0),
        "detail_json": ("TEXT", True, 0),
        "created_at": ("TEXT", True, 0),
    },
}

LEDGER_REQUIRED_INDEXES = {
    "recognition_decisions_event": (False, ("event_id", "face_index")),
    "event_transitions_event": (False, ("event_id", "sequence")),
    "operator_actions_event": (False, ("event_id", "created_at")),
    "camera_events_capture_id": (False, ("capture_id",)),
    "camera_events_lifecycle": (False, ("lifecycle_state", "received_unix")),
}

LEDGER_REQUIRED_TRIGGERS = frozenset(
    {
        "recognition_decisions_no_update",
        "recognition_decisions_no_direct_delete",
        "event_transitions_no_update",
        "event_transitions_no_direct_delete",
        "operator_actions_no_update",
        "operator_actions_no_direct_delete",
    }
)


class EventLedgerError(RuntimeError):
    pass


class EventLedgerValidationError(EventLedgerError, ValueError):
    pass


@dataclass(frozen=True)
class LedgerClaim:
    accepted: bool
    event_id: str
    reason: str = ""
    existing_status: str = ""
    capture_id: str = ""


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def timestamp_from_unix(value):
    return datetime.fromtimestamp(float(value), timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _text(value, field, *, required=False, max_chars=4096):
    if value is None:
        text = ""
    elif not isinstance(value, str):
        raise EventLedgerValidationError(f"{field} must be a string")
    else:
        text = unicodedata.normalize("NFC", value)
    if text != text.strip():
        raise EventLedgerValidationError(
            f"{field} must not contain surrounding whitespace"
        )
    if required and not text:
        raise EventLedgerValidationError(f"{field} is required")
    if len(text) > int(max_chars):
        raise EventLedgerValidationError(
            f"{field} exceeds {int(max_chars)} characters"
        )
    for character in text:
        if unicodedata.category(character) in {"Cc", "Cf", "Cs"}:
            raise EventLedgerValidationError(
                f"{field} contains a control or formatting character"
            )
    return text


def _identifier(value, field):
    text = _text(value, field, required=True, max_chars=64).lower()
    if not HEX64_RE.fullmatch(text):
        raise EventLedgerValidationError(
            f"{field} must be a lowercase 64-character SHA-256 identifier"
        )
    return text


def _timestamp(value, field, *, required=True):
    text = _text(value, field, required=required, max_chars=64)
    if not text:
        return ""
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise EventLedgerValidationError(
            f"{field} must be an RFC 3339 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EventLedgerValidationError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _finite(value, field, *, minimum=None, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EventLedgerValidationError(f"{field} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise EventLedgerValidationError(f"{field} must be finite")
    if minimum is not None and result < float(minimum):
        raise EventLedgerValidationError(f"{field} is below {minimum}")
    if maximum is not None and result > float(maximum):
        raise EventLedgerValidationError(f"{field} exceeds {maximum}")
    return result


def _integer(value, field, *, minimum=0, maximum=(1 << 31) - 1):
    if isinstance(value, bool) or not isinstance(value, int):
        raise EventLedgerValidationError(f"{field} must be an integer")
    if value < int(minimum) or value > int(maximum):
        raise EventLedgerValidationError(
            f"{field} must be between {int(minimum)} and {int(maximum)}"
        )
    return value


def _enum(value, field, allowed):
    text = _text(value, field, required=True, max_chars=64)
    if text not in allowed:
        raise EventLedgerValidationError(
            f"{field} must be one of: {', '.join(sorted(allowed))}"
        )
    return text


def _json_text(value, field, *, max_bytes=32768):
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise EventLedgerValidationError(f"{field} must be a JSON object")
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise EventLedgerValidationError(f"{field} is not JSON-safe") from exc
    if len(encoded.encode("utf-8")) > int(max_bytes):
        raise EventLedgerValidationError(
            f"{field} exceeds {int(max_bytes)} UTF-8 bytes"
        )
    return encoded


def make_capture_id(camera_id, source_sha256, source_name, source_size, source_mtime):
    value = "\0".join(
        (
            str(camera_id),
            str(source_sha256),
            str(source_name),
            str(int(source_size)),
            f"{float(source_mtime):.6f}",
        )
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def make_recognition_decision_id(event_id, face_index, decision_version=1):
    event_id = _identifier(event_id, "event_id")
    face_index = _integer(face_index, "face_index", minimum=1)
    decision_version = _integer(
        decision_version, "decision_version", minimum=1, maximum=1_000_000
    )
    value = "\0".join((event_id, str(face_index), str(decision_version)))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _parse_json_column(row, key):
    value = row.get(key)
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {}


class EventLedgerMixin:
    # These fields describe evidence learned after the immutable normalized
    # receipt is inserted, or later processing outcomes. Camera/source identity,
    # received/effective/source time, branch, and policy are intentionally absent.
    _EVENT_UPDATE_FIELDS = frozenset(
        {
            "transport_received_at",
            "source_remote_ip",
            "receipt_state",
            "receipt_verified",
            "receipt_json",
            "gallery_version",
            "gallery_generated_at",
            "gallery_model",
            "gallery_model_version",
            "recognition_model",
            "recognition_model_version",
            "preprocessing_version",
            "pad_provider",
            "pad_model",
            "policy_version",
            "retention_state",
            "final_disposition",
        }
    )

    @staticmethod
    def _event_update_values(values):
        if not isinstance(values, dict):
            raise EventLedgerValidationError("event_updates must be a JSON object")
        unknown = sorted(set(values) - EventLedgerMixin._EVENT_UPDATE_FIELDS)
        if unknown:
            raise EventLedgerValidationError(
                "unsupported event update fields: " + ", ".join(unknown)
            )
        output = {}
        for key, value in values.items():
            if key in {
                "transport_received_at",
                "gallery_generated_at",
            }:
                output[key] = _timestamp(value, key, required=False)
            elif key == "receipt_verified":
                if not isinstance(value, bool):
                    raise EventLedgerValidationError(
                        "receipt_verified must be a boolean"
                    )
                output[key] = int(value)
            elif key == "receipt_state":
                output[key] = _enum(value, key, RECEIPT_STATES)
            elif key == "retention_state":
                output[key] = _enum(value, key, RETENTION_STATES)
            elif key == "receipt_json":
                output[key] = _json_text(value, key)
            else:
                output[key] = _text(value, key, max_chars=4096)
        return output

    def record_event_receipt(
        self,
        *,
        event_id,
        capture_id,
        camera_id,
        log_type,
        source_sha256,
        source_name,
        source_mtime,
        source_size,
        received_at,
        effective_at,
        branch="",
        source_type="",
        source_principal="",
        source_remote_ip="",
        source_binding_id="",
        policy="",
        source_at="",
        source_time_provenance="",
        transport_received_at="",
        receipt_state="pending",
        receipt_verified=False,
        receipt_detail=None,
        policy_version="",
        reason_code="receipt_recorded",
    ):
        event_id = _identifier(event_id, "event_id")
        capture_id = _identifier(capture_id, "capture_id")
        camera_id = _text(camera_id, "camera_id", required=True, max_chars=128)
        log_type = _text(log_type, "log_type", required=True, max_chars=16)
        if log_type not in {"IN", "OUT"}:
            raise EventLedgerValidationError("log_type must be IN or OUT")
        source_name = _text(source_name, "source_name", required=True, max_chars=1024)
        source_size = _integer(source_size, "source_size", minimum=0)
        source_mtime = _finite(source_mtime, "source_mtime", minimum=0)
        receipt_state = _enum(receipt_state, "receipt_state", RECEIPT_STATES)
        source_sha256 = _text(
            source_sha256, "source_sha256", required=True, max_chars=128
        ).lower()
        if receipt_state != "legacy" and not HEX64_RE.fullmatch(source_sha256):
            raise EventLedgerValidationError(
                "source_sha256 must be a lowercase 64-character SHA-256 digest"
            )
        received_at = _timestamp(received_at, "received_at")
        effective_at = _timestamp(effective_at, "effective_at")
        source_at = _timestamp(source_at, "source_at", required=False)
        transport_received_at = _timestamp(
            transport_received_at, "transport_received_at", required=False
        )
        reason_code = _enum(reason_code, "reason_code", EVENT_REASON_CODES)
        if not isinstance(receipt_verified, bool):
            raise EventLedgerValidationError("receipt_verified must be a boolean")
        received_unix = datetime.fromisoformat(
            received_at.replace("Z", "+00:00")
        ).timestamp()
        receipt_json = _json_text(receipt_detail, "receipt_detail")
        policy = _text(policy or log_type, "policy", required=True, max_chars=32)

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT event_id, status, capture_id
                FROM camera_events
                WHERE event_id = ? OR (camera_id = ? AND source_sha256 = ?)
                LIMIT 1
                """,
                (event_id, camera_id, source_sha256),
            ).fetchone()
            if existing:
                connection.rollback()
                return LedgerClaim(
                    False,
                    existing["event_id"],
                    reason="duplicate",
                    existing_status=existing["status"],
                    capture_id=existing["capture_id"] or "",
                )
            connection.execute(
                """
                INSERT INTO camera_events (
                    event_id, camera_id, log_type, source_sha256, source_name,
                    source_mtime, source_size, status, created_unix, updated_unix,
                    completed_at, error, capture_id, received_at, received_unix,
                    transport_received_at, source_at, source_time_provenance,
                    effective_at, branch, source_type, source_principal,
                    source_remote_ip, source_binding_id, policy, receipt_state,
                    receipt_verified, receipt_json, lifecycle_state, state_version,
                    reason_code, policy_version, retention_state
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, 'received', ?, ?, NULL, '', ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'received', 1, ?, ?, 'pending'
                )
                """,
                (
                    event_id,
                    camera_id,
                    log_type,
                    source_sha256,
                    source_name,
                    source_mtime,
                    source_size,
                    received_unix,
                    received_unix,
                    capture_id,
                    received_at,
                    received_unix,
                    transport_received_at,
                    source_at or None,
                    _text(
                        source_time_provenance,
                        "source_time_provenance",
                        max_chars=128,
                    ),
                    effective_at,
                    _text(branch, "branch", max_chars=128),
                    _text(source_type, "source_type", max_chars=64),
                    _text(source_principal, "source_principal", max_chars=128),
                    _text(source_remote_ip, "source_remote_ip", max_chars=128),
                    _text(source_binding_id, "source_binding_id", max_chars=128),
                    policy,
                    receipt_state,
                    int(receipt_verified),
                    receipt_json,
                    reason_code,
                    _text(policy_version, "policy_version", max_chars=128),
                ),
            )
            connection.execute(
                """
                INSERT INTO event_transitions (
                    event_id, sequence, from_state, to_state, reason_code,
                    actor_type, actor_id, detail_json, created_at
                ) VALUES (?, 1, '', 'received', ?, 'watcher', '', ?, ?)
                """,
                (event_id, reason_code, receipt_json, received_at),
            )
            connection.commit()
            return LedgerClaim(True, event_id, capture_id=capture_id)
        finally:
            connection.close()

    def transition_event(
        self,
        event_id,
        *,
        to_state,
        reason_code,
        actor_type="watcher",
        actor_id="",
        detail=None,
        event_updates=None,
        compatibility_status=None,
        error="",
    ):
        event_id = _identifier(event_id, "event_id")
        to_state = _enum(to_state, "to_state", EVENT_STATES)
        reason_code = _enum(reason_code, "reason_code", EVENT_REASON_CODES)
        actor_type = _enum(actor_type, "actor_type", ACTOR_TYPES)
        actor_id = _text(actor_id, "actor_id", max_chars=256)
        detail_json = _json_text(detail, "transition detail")
        updates = self._event_update_values(event_updates or {})
        status = _text(
            compatibility_status or to_state,
            "compatibility_status",
            required=True,
            max_chars=64,
        )
        raw_error = str(error or "")[:2000]
        error = "".join(
            character
            if unicodedata.category(character) not in {"Cc", "Cf", "Cs"}
            else f"\\u{ord(character):04x}"
            for character in raw_error
        )
        created_at = utc_now()
        now = datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp()

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT lifecycle_state, state_version
                FROM camera_events WHERE event_id = ?
                """,
                (event_id,),
            ).fetchone()
            if row is None:
                raise EventLedgerValidationError(f"event does not exist: {event_id}")
            from_state = row["lifecycle_state"]
            sequence = int(row["state_version"]) + 1
            assignments = [
                "status = ?",
                "lifecycle_state = ?",
                "state_version = ?",
                "reason_code = ?",
                "updated_unix = ?",
                "error = ?",
            ]
            values = [status, to_state, sequence, reason_code, now, error]
            final_disposition = updates.pop("final_disposition", "")
            for key, value in updates.items():
                assignments.append(f"{key} = ?")
                values.append(value)
            if to_state in TERMINAL_EVENT_STATES:
                assignments.extend(["completed_at = ?", "final_disposition = ?"])
                values.extend([created_at, final_disposition or reason_code])
            values.append(event_id)
            connection.execute(
                f"UPDATE camera_events SET {', '.join(assignments)} WHERE event_id = ?",
                values,
            )
            connection.execute(
                """
                INSERT INTO event_transitions (
                    event_id, sequence, from_state, to_state, reason_code,
                    actor_type, actor_id, detail_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    sequence,
                    from_state,
                    to_state,
                    reason_code,
                    actor_type,
                    actor_id,
                    detail_json,
                    created_at,
                ),
            )
            connection.commit()
            return sequence
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def record_recognition_decision(
        self,
        *,
        event_id,
        face_index,
        face_count,
        bbox,
        face_width,
        face_height,
        detection_score,
        best_employee="",
        best_score=0.0,
        runner_up_score=0.0,
        score_margin=0.0,
        pad_passed=False,
        pad_skipped=False,
        pad_score=None,
        pad_provider="",
        pad_model="",
        pad_evidence_id="",
        pad_binding_id="",
        accepted=False,
        reason_code,
        candidate_log_type="",
        policy_version="",
        gallery_version="",
        gallery_generated_at="",
        gallery_model="",
        gallery_model_version="",
        recognition_model="",
        recognition_model_version="",
        preprocessing_version="",
        retention_state="pending",
        decision_version=1,
    ):
        event_id = _identifier(event_id, "event_id")
        face_index = _integer(face_index, "face_index", minimum=1)
        face_count = _integer(face_count, "face_count", minimum=face_index)
        decision_version = _integer(
            decision_version, "decision_version", minimum=1, maximum=1_000_000
        )
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            raise EventLedgerValidationError("bbox must contain four integers")
        bbox_values = [
            int(round(_finite(value, f"bbox[{index}]")))
            for index, value in enumerate(bbox)
        ]
        if not isinstance(pad_passed, bool) or not isinstance(pad_skipped, bool):
            raise EventLedgerValidationError(
                "pad_passed and pad_skipped must be booleans"
            )
        if not isinstance(accepted, bool):
            raise EventLedgerValidationError("accepted must be a boolean")
        if pad_score is not None:
            pad_score = _finite(pad_score, "pad_score", minimum=0, maximum=1)
        reason_code = _enum(reason_code, "reason_code", EVENT_REASON_CODES)
        retention_state = _enum(
            retention_state, "retention_state", RETENTION_STATES
        )
        candidate_log_type = _text(
            candidate_log_type, "candidate_log_type", max_chars=16
        )
        if candidate_log_type and candidate_log_type not in {"IN", "OUT"}:
            raise EventLedgerValidationError(
                "candidate_log_type must be empty, IN, or OUT"
            )
        decision_id = make_recognition_decision_id(
            event_id, face_index, decision_version
        )
        created_at = utc_now()
        values = (
            decision_id,
            event_id,
            decision_version,
            face_index,
            face_count,
            *bbox_values,
            _finite(face_width, "face_width", minimum=0),
            _finite(face_height, "face_height", minimum=0),
            _finite(detection_score, "detection_score", minimum=0, maximum=1),
            _text(best_employee, "best_employee", max_chars=180),
            _finite(best_score, "best_score", minimum=-1, maximum=1),
            _finite(runner_up_score, "runner_up_score", minimum=-1, maximum=1),
            _finite(score_margin, "score_margin", minimum=-2, maximum=2),
            int(pad_passed),
            int(pad_skipped),
            pad_score,
            _text(pad_provider, "pad_provider", max_chars=128),
            _text(pad_model, "pad_model", max_chars=128),
            _text(pad_evidence_id, "pad_evidence_id", max_chars=512),
            _text(pad_binding_id, "pad_binding_id", max_chars=128),
            int(accepted),
            reason_code,
            candidate_log_type,
            _text(policy_version, "policy_version", max_chars=128),
            _text(gallery_version, "gallery_version", max_chars=128),
            _timestamp(
                gallery_generated_at, "gallery_generated_at", required=False
            ),
            _text(gallery_model, "gallery_model", max_chars=128),
            _text(
                gallery_model_version, "gallery_model_version", max_chars=128
            ),
            _text(recognition_model, "recognition_model", max_chars=128),
            _text(
                recognition_model_version,
                "recognition_model_version",
                max_chars=128,
            ),
            _text(preprocessing_version, "preprocessing_version", max_chars=128),
            retention_state,
            created_at,
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM camera_events WHERE event_id = ?", (event_id,)
            ).fetchone() is None:
                raise EventLedgerValidationError(f"event does not exist: {event_id}")
            connection.execute(
                """
                INSERT INTO recognition_decisions (
                    decision_id, event_id, decision_version, face_index, face_count,
                    bbox_x1, bbox_y1, bbox_x2, bbox_y2, face_width, face_height,
                    detection_score, best_employee, best_score, runner_up_score,
                    score_margin, pad_passed, pad_skipped, pad_score, pad_provider,
                    pad_model, pad_evidence_id, pad_binding_id, accepted, reason_code,
                    candidate_log_type, policy_version, gallery_version,
                    gallery_generated_at, gallery_model, gallery_model_version,
                    recognition_model, recognition_model_version,
                    preprocessing_version, retention_state, created_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                values,
            )
            connection.commit()
            return decision_id
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise EventLedgerValidationError(
                f"recognition decision already exists or is invalid: {decision_id}"
            ) from exc
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def record_operator_action(
        self,
        *,
        event_id,
        actor,
        action,
        reason_code="operator_action",
        detail=None,
    ):
        event_id = _identifier(event_id, "event_id")
        actor = _text(actor, "actor", required=True, max_chars=256)
        action = _text(action, "action", required=True, max_chars=128)
        reason_code = _enum(reason_code, "reason_code", EVENT_REASON_CODES)
        detail_json = _json_text(detail, "operator action detail")
        created_at = utc_now()
        action_id = hashlib.sha256(
            "\0".join((event_id, actor, action, reason_code, created_at)).encode(
                "utf-8"
            )
        ).hexdigest()
        connection = self._connect()
        try:
            connection.execute(
                """
                INSERT INTO operator_actions (
                    action_id, event_id, actor, action, reason_code,
                    detail_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action_id,
                    event_id,
                    actor,
                    action,
                    reason_code,
                    detail_json,
                    created_at,
                ),
            )
            connection.commit()
            return action_id
        finally:
            connection.close()

    def event_details(self, event_id, *, include_history=True):
        event_id = _identifier(event_id, "event_id")
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM camera_events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["receipt"] = _parse_json_column(result, "receipt_json")
            if not include_history:
                return result
            transitions = [
                dict(item)
                for item in connection.execute(
                    """
                    SELECT sequence, from_state, to_state, reason_code,
                           actor_type, actor_id, detail_json, created_at
                    FROM event_transitions
                    WHERE event_id = ? ORDER BY sequence
                    """,
                    (event_id,),
                ).fetchall()
            ]
            for item in transitions:
                item["detail"] = _parse_json_column(item, "detail_json")
            decisions = [
                dict(item)
                for item in connection.execute(
                    """
                    SELECT * FROM recognition_decisions
                    WHERE event_id = ? ORDER BY face_index, decision_version
                    """,
                    (event_id,),
                ).fetchall()
            ]
            actions = [
                dict(item)
                for item in connection.execute(
                    """
                    SELECT action_id, actor, action, reason_code,
                           detail_json, created_at
                    FROM operator_actions
                    WHERE event_id = ? ORDER BY created_at, action_id
                    """,
                    (event_id,),
                ).fetchall()
            ]
            for item in actions:
                item["detail"] = _parse_json_column(item, "detail_json")
            result["transitions"] = transitions
            result["decisions"] = decisions
            result["operator_actions"] = actions
            return result
        finally:
            connection.close()
