"""Safe event inspection and audited local operator workflows.

The event ledger intentionally keeps operator reads separate from biometric
material and secret-bearing runtime configuration. Mutations in this module do
not retry or cancel ERPNext delivery; those controls belong to the Phase 2
outbox and reconciliation workflow.
"""

import hashlib
import json
import math
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from event_identity import identity_contract
from event_ledger import EVENT_REASON_CODES, EVENT_STATES, RETENTION_STATES


HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
OPERATOR_STAGE_NAME_RE = re.compile(
    r"^\.reprocess-(?P<event_id>[0-9a-f]{64})-.+\.operator\.incoming$"
)
OPERATOR_STAGE_SUFFIX = ".operator.incoming"
MAX_FILESYSTEM_NAME_BYTES = 255
MAX_OPERATOR_TARGET_NAME_BYTES = (
    MAX_FILESYSTEM_NAME_BYTES
    - len(("." + OPERATOR_STAGE_SUFFIX).encode("utf-8"))
)
REPROCESSABLE_STATES = frozenset(
    {"rejected", "failed", "dismissed", "processed", "quarantined"}
)
SENSITIVE_KEY_PARTS = frozenset(
    {
        "authorization",
        "cookie",
        "credential",
        "embedding",
        "password",
        "private_key",
        "secret",
        "signature",
        "template",
        "token",
        "vector",
    }
)


OPERATION_SCHEMA_STATEMENTS = (
    "ALTER TABLE camera_events ADD COLUMN source_path TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE camera_events ADD COLUMN retention_path TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE camera_events ADD COLUMN operator_revision INTEGER NOT NULL DEFAULT 0 CHECK(operator_revision >= 0)",
    "ALTER TABLE camera_events ADD COLUMN last_operator_action_id TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE camera_events ADD COLUMN last_operator_action_at TEXT NOT NULL DEFAULT ''",
    "CREATE INDEX camera_events_operator_revision ON camera_events(operator_revision, received_unix)",
    "CREATE INDEX camera_events_retention_state ON camera_events(retention_state, lifecycle_state)",
    """
    CREATE TRIGGER camera_events_source_path_immutable
    BEFORE UPDATE OF source_path ON camera_events
    WHEN OLD.source_path != '' AND NEW.source_path != OLD.source_path
    BEGIN
        SELECT RAISE(ABORT, 'event source path is immutable');
    END
    """,
)

OPERATION_REQUIRED_TABLE_COLUMNS = {
    "camera_events": {
        "source_path": ("TEXT", True, 0),
        "retention_path": ("TEXT", True, 0),
        "operator_revision": ("INTEGER", True, 0),
        "last_operator_action_id": ("TEXT", True, 0),
        "last_operator_action_at": ("TEXT", True, 0),
    }
}

OPERATION_REQUIRED_INDEXES = {
    "camera_events_operator_revision": (
        False,
        ("operator_revision", "received_unix"),
    ),
    "camera_events_retention_state": (
        False,
        ("retention_state", "lifecycle_state"),
    ),
}

OPERATION_REQUIRED_TRIGGERS = frozenset({"camera_events_source_path_immutable"})


class EventOperationError(RuntimeError):
    pass


class EventOperationValidationError(EventOperationError, ValueError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def operator_staging_path(target_path):
    target = Path(target_path)
    if not target.name or target.name.startswith("."):
        raise EventOperationValidationError(
            "reprocess target must have a visible filename"
        )
    if len(target.name.encode("utf-8")) > MAX_OPERATOR_TARGET_NAME_BYTES:
        raise EventOperationValidationError(
            "reprocess target filename is too long for its hidden staging name"
        )
    stage = target.with_name(f".{target.name}{OPERATOR_STAGE_SUFFIX}")
    if len(stage.name.encode("utf-8")) > MAX_FILESYSTEM_NAME_BYTES:
        raise EventOperationValidationError(
            "operator staging filename exceeds the filesystem limit"
        )
    return stage


def operator_target_from_staging(stage_path):
    stage = Path(stage_path)
    if not (
        stage.name.startswith(".")
        and stage.name.endswith(OPERATOR_STAGE_SUFFIX)
    ):
        return None
    name = stage.name[1 : -len(OPERATOR_STAGE_SUFFIX)]
    return stage.with_name(name) if name else None


def event_id_from_operator_staging(stage_path):
    match = OPERATOR_STAGE_NAME_RE.fullmatch(Path(stage_path).name)
    return match.group("event_id") if match else ""


def _text(value, field, *, required=False, min_chars=0, max_chars=4096):
    if value is None:
        text = ""
    elif not isinstance(value, str):
        raise EventOperationValidationError(f"{field} must be a string")
    else:
        text = unicodedata.normalize("NFC", value)
    if text != text.strip():
        raise EventOperationValidationError(
            f"{field} must not contain surrounding whitespace"
        )
    if required and not text:
        raise EventOperationValidationError(f"{field} is required")
    if len(text) < int(min_chars):
        raise EventOperationValidationError(
            f"{field} must contain at least {int(min_chars)} characters"
        )
    if len(text) > int(max_chars):
        raise EventOperationValidationError(
            f"{field} exceeds {int(max_chars)} characters"
        )
    for character in text:
        if unicodedata.category(character) in {"Cc", "Cf", "Cs"}:
            raise EventOperationValidationError(
                f"{field} contains a control or formatting character"
            )
    return text


def _identifier(value, field):
    text = _text(value, field, required=True, max_chars=64).lower()
    if not HEX64_RE.fullmatch(text):
        raise EventOperationValidationError(
            f"{field} must be a lowercase 64-character SHA-256 identifier"
        )
    return text


def _strict_int(value, field, *, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int):
        raise EventOperationValidationError(f"{field} must be an integer")
    if value < int(minimum) or value > int(maximum):
        raise EventOperationValidationError(
            f"{field} must be between {int(minimum)} and {int(maximum)}"
        )
    return value


def _now_unix(value=None):
    if value is None:
        result = datetime.now(timezone.utc).timestamp()
    elif isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EventOperationValidationError("now must be a finite Unix timestamp")
    else:
        result = float(value)
    if not math.isfinite(result) or result < 0:
        raise EventOperationValidationError("now must be a finite non-negative Unix timestamp")
    return result


def _timestamp_unix(value, field):
    text = _text(value, field, required=True, max_chars=64)
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise EventOperationValidationError(
            f"{field} must be an RFC 3339 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EventOperationValidationError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc).timestamp()


def _json_text(value, field, *, max_bytes=32768):
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise EventOperationValidationError(f"{field} must be a JSON object")
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise EventOperationValidationError(f"{field} is not JSON-safe") from exc
    if len(encoded.encode("utf-8")) > int(max_bytes):
        raise EventOperationValidationError(
            f"{field} exceeds {int(max_bytes)} UTF-8 bytes"
        )
    return encoded


def _is_sensitive_key(key):
    normalized = str(key or "").strip().lower().replace("-", "_")
    return any(part in normalized for part in SENSITIVE_KEY_PARTS)


_SECRET_TEXT_PATTERNS = (
    (
        re.compile(r"(?i)(authorization\s*[:=]\s*)([^,;\r\n]+)"),
        r"\1<redacted>",
    ),
    (
        re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._~+/=-]{8,})"),
        r"\1<redacted>",
    ),
    (
        re.compile(
            r"(?i)(token|secret|password|api[_-]?key)(\s*[:=]\s*)([^\s,;&]+)"
        ),
        r"\1\2<redacted>",
    ),
    (
        re.compile(r"(?i)([?&](?:token|secret|password|api[_-]?key)=)([^&#\s]+)"),
        r"\1<redacted>",
    ),
    (
        re.compile(r"(?i)(https?://[^/@:\s]+:)([^@/\s]+)(@)"),
        r"\1<redacted>\3",
    ),
)


def redact_text(value):
    text = str(value)
    for pattern, replacement in _SECRET_TEXT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_value(value, *, include_paths=False, key=""):
    """Return a JSON-safe event view without secrets or biometric vectors."""

    if _is_sensitive_key(key):
        return "<redacted>"
    if isinstance(value, dict):
        return {
            str(item_key): redact_value(
                item_value,
                include_paths=include_paths,
                key=str(item_key),
            )
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [
            redact_value(item, include_paths=include_paths, key=key)
            for item in value
        ]
    if key in {"source_path", "retention_path"} and isinstance(value, str):
        if include_paths or not value:
            return redact_text(value)
        return Path(value).name
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, str):
        return redact_text(value)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return redact_text(value)


def sanitized_event(event, *, include_paths=False):
    if event is None:
        return None
    payload = redact_value(dict(event), include_paths=include_paths)
    # Raw JSON columns duplicate parsed forms and are easier to misuse.
    payload.pop("receipt_json", None)
    for transition in payload.get("transitions", []):
        transition.pop("detail_json", None)
    for action in payload.get("operator_actions", []):
        action.pop("detail_json", None)
    return payload


def explain_event(event, *, include_paths=False):
    event = sanitized_event(event, include_paths=include_paths)
    if event is None:
        return None
    return {
        "identifier_contract": identity_contract(),
        "event": {
            key: event.get(key)
            for key in (
                "event_id",
                "capture_id",
                "camera_id",
                "branch",
                "policy",
                "source_name",
                "source_path",
                "retention_path",
                "received_at",
                "effective_at",
                "lifecycle_state",
                "reason_code",
                "final_disposition",
                "processing_attempt",
                "recovery_count",
                "processing_phase",
                "retention_state",
                "operator_revision",
            )
            if key in event
        },
        "receipt": event.get("receipt", {}),
        "timeline": [
            {
                "sequence": item.get("sequence"),
                "from": item.get("from_state"),
                "to": item.get("to_state"),
                "reason": item.get("reason_code"),
                "actor_type": item.get("actor_type"),
                "actor_id": item.get("actor_id"),
                "at": item.get("created_at"),
                "detail": item.get("detail", {}),
            }
            for item in event.get("transitions", [])
        ],
        "decisions": [
            {
                "decision_id": item.get("decision_id"),
                "delivery_id": item.get("delivery_id"),
                "version": item.get("decision_version"),
                "face": f"{item.get('face_index')}/{item.get('face_count')}",
                "accepted": bool(item.get("accepted")),
                "reason": item.get("reason_code"),
                "employee": item.get("best_employee"),
                "best_score": item.get("best_score"),
                "runner_up_score": item.get("runner_up_score"),
                "margin": item.get("score_margin"),
                "detection_score": item.get("detection_score"),
                "pad": {
                    "passed": bool(item.get("pad_passed")),
                    "skipped": bool(item.get("pad_skipped")),
                    "score": item.get("pad_score"),
                    "provider": item.get("pad_provider"),
                    "model": item.get("pad_model"),
                    "evidence_id": item.get("pad_evidence_id"),
                    "binding_id": item.get("pad_binding_id"),
                },
                "versions": {
                    "gallery": item.get("gallery_version"),
                    "gallery_model": item.get("gallery_model"),
                    "gallery_model_version": item.get("gallery_model_version"),
                    "recognition_model": item.get("recognition_model"),
                    "recognition_model_version": item.get(
                        "recognition_model_version"
                    ),
                    "preprocessing": item.get("preprocessing_version"),
                    "policy": item.get("policy_version"),
                },
                "retention": item.get("retention_state"),
            }
            for item in event.get("decisions", [])
        ],
        "operator_actions": [
            {
                "action_id": item.get("action_id"),
                "actor": item.get("actor"),
                "action": item.get("action"),
                "reason_code": item.get("reason_code"),
                "at": item.get("created_at"),
                "detail": item.get("detail", {}),
            }
            for item in event.get("operator_actions", [])
        ],
    }


class EventOperationsMixin:
    @staticmethod
    def _operator_action_id(event_id, actor, action, created_at, revision):
        return hashlib.sha256(
            "\0".join(
                (event_id, actor, action, created_at, str(int(revision)))
            ).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _insert_operator_action_tx(
        connection,
        *,
        event_id,
        actor,
        action,
        reason,
        created_at,
        revision,
        detail=None,
    ):
        action_id = EventOperationsMixin._operator_action_id(
            event_id, actor, action, created_at, revision
        )
        payload = dict(detail or {})
        payload["reason"] = reason
        connection.execute(
            """
            INSERT INTO operator_actions (
                action_id, event_id, actor, action, reason_code,
                detail_json, created_at
            ) VALUES (?, ?, ?, ?, 'operator_action', ?, ?)
            """,
            (
                action_id,
                event_id,
                actor,
                action,
                _json_text(payload, "operator action detail"),
                created_at,
            ),
        )
        return action_id

    @staticmethod
    def _append_operator_transition_tx(
        connection,
        row,
        *,
        to_state,
        action_id,
        actor,
        action,
        reason,
        created_at,
        now,
        terminal,
        updates=None,
    ):
        sequence = int(row["state_version"]) + 1
        revision = int(row["operator_revision"] or 0) + 1
        assignments = [
            "status = ?",
            "lifecycle_state = ?",
            "state_version = ?",
            "reason_code = 'operator_action'",
            "updated_unix = ?",
            "error = ''",
            "operator_revision = ?",
            "last_operator_action_id = ?",
            "last_operator_action_at = ?",
        ]
        values = [to_state, to_state, sequence, now, revision, action_id, created_at]
        for key, value in (updates or {}).items():
            assignments.append(f"{key} = ?")
            values.append(value)
        if terminal:
            assignments.extend(
                [
                    "completed_at = ?",
                    "final_disposition = ?",
                    "processing_phase = 'terminal'",
                    "lease_owner = ''",
                    "lease_acquired_at = ''",
                    "lease_heartbeat_at = ''",
                    "lease_expires_unix = 0",
                ]
            )
            values.extend([created_at, action])
        else:
            assignments.extend(
                [
                    "completed_at = NULL",
                    "final_disposition = ''",
                    "processing_phase = 'idle'",
                    "lease_owner = ''",
                    "lease_acquired_at = ''",
                    "lease_heartbeat_at = ''",
                    "lease_expires_unix = 0",
                    "delivery_started_at = ''",
                    "delivery_decision_id = ''",
                ]
            )
        values.append(row["event_id"])
        connection.execute(
            f"UPDATE camera_events SET {', '.join(assignments)} WHERE event_id = ?",
            values,
        )
        connection.execute(
            """
            INSERT INTO event_transitions (
                event_id, sequence, from_state, to_state, reason_code,
                actor_type, actor_id, detail_json, created_at
            ) VALUES (?, ?, ?, ?, 'operator_action', 'operator', ?, ?, ?)
            """,
            (
                row["event_id"],
                sequence,
                row["lifecycle_state"],
                to_state,
                actor,
                _json_text(
                    {
                        "action": action,
                        "action_id": action_id,
                        "reason": reason,
                    },
                    "operator transition detail",
                ),
                created_at,
            ),
        )
        return sequence

    def list_events(
        self,
        *,
        state="",
        reason="",
        camera="",
        branch="",
        direction="",
        employee="",
        since="",
        until="",
        limit=50,
        offset=0,
    ):
        limit = _strict_int(limit, "limit", minimum=1, maximum=500)
        offset = _strict_int(offset, "offset", minimum=0, maximum=10_000_000)
        clauses = []
        values = []
        if state:
            state = _text(state, "state", required=True, max_chars=64)
            if state not in EVENT_STATES:
                raise EventOperationValidationError(
                    "state must be one of: " + ", ".join(sorted(EVENT_STATES))
                )
            clauses.append("e.lifecycle_state = ?")
            values.append(state)
        if reason:
            reason = _text(reason, "reason", required=True, max_chars=64)
            if reason not in EVENT_REASON_CODES:
                raise EventOperationValidationError(
                    "reason must be one of: "
                    + ", ".join(sorted(EVENT_REASON_CODES))
                )
            clauses.append("e.reason_code = ?")
            values.append(reason)
        if camera:
            clauses.append("e.camera_id = ?")
            values.append(_text(camera, "camera", required=True, max_chars=128))
        if branch:
            clauses.append("e.branch = ?")
            values.append(_text(branch, "branch", required=True, max_chars=128))
        if direction:
            direction = _text(
                direction, "direction", required=True, max_chars=16
            ).upper()
            if direction not in {"IN", "OUT"}:
                raise EventOperationValidationError("direction must be IN or OUT")
            clauses.append("e.policy = ?")
            values.append(direction)
        if employee:
            employee = _text(employee, "employee", required=True, max_chars=180)
            clauses.append(
                "EXISTS (SELECT 1 FROM recognition_decisions d "
                "WHERE d.event_id = e.event_id AND d.best_employee = ?)"
            )
            values.append(employee)
        if since:
            clauses.append("e.received_unix >= ?")
            values.append(_timestamp_unix(since, "since"))
        if until:
            clauses.append("e.received_unix <= ?")
            values.append(_timestamp_unix(until, "until"))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        query = f"""
            SELECT
                e.event_id, e.capture_id, e.received_at, e.effective_at,
                e.camera_id, e.branch, e.policy, e.source_name,
                e.lifecycle_state, e.reason_code, e.status,
                e.processing_attempt, e.recovery_count, e.processing_phase,
                e.retention_state, e.final_disposition, e.operator_revision,
                (SELECT COUNT(*) FROM recognition_decisions d
                 WHERE d.event_id = e.event_id) AS decision_count,
                (SELECT COUNT(*) FROM recognition_decisions d
                 WHERE d.event_id = e.event_id AND d.accepted = 1) AS accepted_count
            FROM camera_events e
            {where}
            ORDER BY e.received_unix DESC, e.event_id DESC
            LIMIT ? OFFSET ?
        """
        values.extend([limit, offset])
        connection = self._connect()
        try:
            return [dict(row) for row in connection.execute(query, values).fetchall()]
        finally:
            connection.close()

    def inspect_event(self, event_id, *, include_paths=False):
        return sanitized_event(
            self.event_details(
                _identifier(event_id, "event_id"), include_history=True
            ),
            include_paths=include_paths,
        )

    def explain_event(self, event_id, *, include_paths=False):
        return explain_event(
            self.event_details(
                _identifier(event_id, "event_id"), include_history=True
            ),
            include_paths=include_paths,
        )

    def request_event_reprocess(
        self,
        event_id,
        *,
        actor,
        reason,
        media_path,
        action="reprocess_requested",
        publish_lease_seconds=120,
        now=None,
    ):
        event_id = _identifier(event_id, "event_id")
        actor = _text(actor, "actor", required=True, max_chars=256)
        reason = _text(
            reason, "reason", required=True, min_chars=5, max_chars=1000
        )
        action = _text(action, "action", required=True, max_chars=128)
        if action not in {"reprocess_requested", "quarantine_requeued"}:
            raise EventOperationValidationError("unsupported reprocess action")
        media_path = _text(
            str(media_path or ""), "media_path", required=True, max_chars=4096
        )
        publish_lease_seconds = _strict_int(
            publish_lease_seconds,
            "publish_lease_seconds",
            minimum=30,
            maximum=900,
        )
        now = _now_unix(now)
        created_at = datetime.fromtimestamp(now, timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM camera_events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if row is None:
                raise EventOperationValidationError(
                    f"event does not exist: {event_id}"
                )
            if row["lifecycle_state"] not in REPROCESSABLE_STATES:
                raise EventOperationValidationError(
                    f"event state {row['lifecycle_state']} is not reprocessable"
                )
            if row["delivery_started_at"] or row["processing_phase"] == "delivery_in_progress":
                raise EventOperationValidationError(
                    "events that crossed the delivery boundary cannot be reprocessed"
                )
            if row["lease_owner"] and float(row["lease_expires_unix"] or 0) > now:
                raise EventOperationValidationError(
                    "event has an active processing lease"
                )
            revision = int(row["operator_revision"] or 0) + 1
            action_id = self._insert_operator_action_tx(
                connection,
                event_id=event_id,
                actor=actor,
                action=action,
                reason=reason,
                created_at=created_at,
                revision=revision,
                detail={
                    "media_path": Path(media_path).name,
                    "previous_state": row["lifecycle_state"],
                },
            )
            operator_lease_owner = f"operator:{action_id}"
            self._release_policy_for_event_tx(connection, event_id, uncertain=False)
            self._append_operator_transition_tx(
                connection,
                row,
                to_state="received",
                action_id=action_id,
                actor=actor,
                action=action,
                reason=reason,
                created_at=created_at,
                now=now,
                terminal=False,
                updates={
                    "retention_state": "temporary",
                    "retention_path": media_path,
                },
            )
            connection.execute(
                """
                UPDATE camera_events
                SET lease_owner = ?, lease_acquired_at = ?,
                    lease_heartbeat_at = ?, lease_expires_unix = ?,
                    processing_phase = 'idle'
                WHERE event_id = ?
                """,
                (
                    operator_lease_owner,
                    created_at,
                    created_at,
                    now + publish_lease_seconds,
                    event_id,
                ),
            )
            connection.commit()
            return {
                "ok": True,
                "event_id": event_id,
                "action_id": action_id,
                "action": action,
                "state": "received",
                "media_path": media_path,
                "lease_owner": operator_lease_owner,
                "lease_expires_unix": now + publish_lease_seconds,
            }
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def complete_event_reprocess_publish(
        self,
        event_id,
        *,
        action_id,
        media_path,
        now=None,
    ):
        event_id = _identifier(event_id, "event_id")
        action_id = _identifier(action_id, "action_id")
        media_path = _text(
            str(media_path or ""), "media_path", required=True, max_chars=4096
        )
        now = _now_unix(now)
        expected_owner = f"operator:{action_id}"
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM camera_events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if row is None:
                raise EventOperationValidationError(
                    f"event does not exist: {event_id}"
                )
            if row["lifecycle_state"] != "received":
                raise EventOperationValidationError(
                    "reprocess publication can complete only from received state"
                )
            if row["last_operator_action_id"] != action_id:
                raise EventOperationValidationError(
                    "reprocess publication action does not match the event"
                )
            if row["lease_owner"] != expected_owner:
                raise EventOperationValidationError(
                    "reprocess publication lease is no longer owned by this action"
                )
            if float(row["lease_expires_unix"] or 0) <= now:
                raise EventOperationValidationError(
                    "reprocess publication lease has expired"
                )
            connection.execute(
                """
                UPDATE camera_events
                SET retention_state = 'retained', retention_path = ?,
                    lease_owner = '', lease_acquired_at = '',
                    lease_heartbeat_at = '', lease_expires_unix = 0,
                    updated_unix = ?
                WHERE event_id = ?
                """,
                (media_path, now, event_id),
            )
            connection.commit()
            return {
                "ok": True,
                "event_id": event_id,
                "action_id": action_id,
                "media_path": media_path,
                "state": "received",
            }
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def dismiss_event(
        self,
        event_id,
        *,
        actor,
        reason,
        acknowledge_delivery_checked=False,
        action="dismissed",
        now=None,
    ):
        event_id = _identifier(event_id, "event_id")
        actor = _text(actor, "actor", required=True, max_chars=256)
        reason = _text(
            reason, "reason", required=True, min_chars=5, max_chars=1000
        )
        action = _text(action, "action", required=True, max_chars=128)
        if action not in {"dismissed", "quarantine_dismissed"}:
            raise EventOperationValidationError("unsupported dismissal action")
        if not isinstance(acknowledge_delivery_checked, bool):
            raise EventOperationValidationError(
                "acknowledge_delivery_checked must be a boolean"
            )
        now = _now_unix(now)
        created_at = datetime.fromtimestamp(now, timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM camera_events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if row is None:
                raise EventOperationValidationError(
                    f"event does not exist: {event_id}"
                )
            if row["lifecycle_state"] == "dismissed":
                raise EventOperationValidationError("event is already dismissed")
            if row["lifecycle_state"] == "checkin_created":
                raise EventOperationValidationError(
                    "delivered check-ins must be corrected in ERPNext"
                )
            delivery_ambiguous = bool(
                row["lifecycle_state"] == "uncertain"
                or row["delivery_started_at"]
                or row["processing_phase"] == "delivery_in_progress"
            )
            if delivery_ambiguous and not acknowledge_delivery_checked:
                raise EventOperationValidationError(
                    "uncertain delivery dismissal requires ERPNext verification acknowledgement"
                )
            if row["lease_owner"] and float(row["lease_expires_unix"] or 0) > now:
                raise EventOperationValidationError(
                    "event has an active processing lease"
                )
            revision = int(row["operator_revision"] or 0) + 1
            action_id = self._insert_operator_action_tx(
                connection,
                event_id=event_id,
                actor=actor,
                action=action,
                reason=reason,
                created_at=created_at,
                revision=revision,
                detail={
                    "erpnext_checked": bool(acknowledge_delivery_checked),
                    "previous_state": row["lifecycle_state"],
                },
            )
            self._release_policy_for_event_tx(connection, event_id, uncertain=False)
            self._append_operator_transition_tx(
                connection,
                row,
                to_state="dismissed",
                action_id=action_id,
                actor=actor,
                action=action,
                reason=reason,
                created_at=created_at,
                now=now,
                terminal=True,
            )
            connection.commit()
            return {
                "ok": True,
                "event_id": event_id,
                "action_id": action_id,
                "action": action,
                "state": "dismissed",
            }
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def mark_reprocess_publish_failed(
        self,
        event_id,
        *,
        action_id,
        actor,
        reason,
        retention_path,
        retention_state="retained",
        now=None,
    ):
        event_id = _identifier(event_id, "event_id")
        action_id = _identifier(action_id, "action_id")
        actor = _text(actor, "actor", required=True, max_chars=256)
        reason = _text(
            reason, "reason", required=True, min_chars=5, max_chars=1000
        )
        retention_path = _text(
            str(retention_path or ""),
            "retention_path",
            required=True,
            max_chars=4096,
        )
        retention_state = _text(
            retention_state,
            "retention_state",
            required=True,
            max_chars=64,
        )
        if retention_state not in RETENTION_STATES:
            raise EventOperationValidationError(
                "retention_state must be one of: "
                + ", ".join(sorted(RETENTION_STATES))
            )
        now = _now_unix(now)
        created_at = datetime.fromtimestamp(now, timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM camera_events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if row is None:
                raise EventOperationValidationError(
                    f"event does not exist: {event_id}"
                )
            expected_owner = f"operator:{action_id}"
            if row["last_operator_action_id"] != action_id:
                raise EventOperationValidationError(
                    "reprocess failure action does not match the event"
                )
            if row["lease_owner"] not in {"", expected_owner}:
                raise EventOperationValidationError(
                    "reprocess publication lease was acquired by another worker"
                )
            revision = int(row["operator_revision"] or 0) + 1
            failure_action_id = self._insert_operator_action_tx(
                connection,
                event_id=event_id,
                actor=actor,
                action="reprocess_publish_failed",
                reason=reason,
                created_at=created_at,
                revision=revision,
                detail={"retention_path": Path(retention_path).name},
            )
            self._append_operator_transition_tx(
                connection,
                row,
                to_state="failed",
                action_id=failure_action_id,
                actor=actor,
                action="reprocess_publish_failed",
                reason=reason,
                created_at=created_at,
                now=now,
                terminal=True,
                updates={
                    "retention_state": retention_state,
                    "retention_path": retention_path,
                },
            )
            connection.commit()
            return failure_action_id
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
