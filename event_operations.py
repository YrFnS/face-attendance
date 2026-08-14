import hashlib
import json
import math
import os
import shutil
import sqlite3
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from camera_sources import (
    CameraSourceError,
    load_camera_sources,
    receipt_path,
    verify_source_receipt,
)
from event_ledger import TERMINAL_EVENT_STATES
from processing_recovery import (
    _finite,
    _identifier,
    _json_text,
    _text,
    timestamp_from_unix,
)
from secret_store import (
    ConfigLoadError,
    SecretStoreError,
    load_config_document,
    resolve_secret_reference,
)


READ_ONLY_LIMIT_MAX = 200
QUARANTINE_SCAN_LIMIT_DEFAULT = 5000
QUARANTINE_SCAN_LIMIT_MAX = 100000
OPERATOR_REASON_MIN_CHARS = 4
OPERATOR_REASON_MAX_CHARS = 1000
REPROCESSABLE_STATES = frozenset({"rejected", "failed", "dismissed", "quarantined"})
DISMISSIBLE_STATES = frozenset(
    {"received", "processing", "source_verified", "validating", "recognizing", "rejected", "failed", "quarantined"}
)

_SECRET_KEYS = frozenset(
    {
        "authorization",
        "cookie",
        "password",
        "private_key",
        "secret",
        "session",
        "signature",
        "token",
        "api_key",
        "api_secret",
    }
)
_VECTOR_KEYS = frozenset(
    {
        "embedding",
        "embeddings",
        "vector",
        "vectors",
        "face_vector",
        "template_vector",
    }
)


class EventInspectionError(RuntimeError):
    pass


class EventOperationError(RuntimeError):
    pass


@dataclass(frozen=True)
class QuarantineMove:
    quarantine_image: Path
    quarantine_receipt: Path
    destination_image: Path
    destination_receipt: Path


def _normalized_key(value):
    return str(value or "").strip().lower().replace("-", "_")


def _safe_scalar(value):
    if isinstance(value, str):
        if len(value) > 4096:
            return value[:4096] + "…"
        return value
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def redact_event_value(value, *, key=""):
    """Return a JSON-safe copy with secrets and biometric vectors removed."""

    normalized = _normalized_key(key)
    if normalized in _SECRET_KEYS or normalized.endswith("_token") or normalized.endswith("_secret"):
        return "<redacted>"
    if normalized in _VECTOR_KEYS or normalized.endswith("_embedding") or normalized.endswith("_vector"):
        return "<omitted-biometric-vector>"
    if isinstance(value, dict):
        return {
            str(item_key): redact_event_value(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_event_value(item, key=key) for item in value]
    return _safe_scalar(value)


def _parse_json(value):
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {}


def _readonly_connection(path):
    raw_path = Path(path).expanduser()
    if raw_path.is_symlink():
        raise EventInspectionError(
            f"runtime database must not be a symbolic link: {raw_path}"
        )
    path = raw_path.resolve(strict=False)
    if not path.is_file():
        raise EventInspectionError(f"runtime database does not exist: {path}")
    try:
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    except sqlite3.DatabaseError as exc:
        raise EventInspectionError(f"could not open runtime database read-only: {exc}") from exc
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def _table_exists(connection, table):
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _table_columns(connection, table):
    if not _table_exists(connection, table):
        return set()
    return {
        str(row["name"])
        for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    }


def _timestamp_unix(value, field):
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise EventInspectionError(f"{field} must be an RFC 3339 string")
    text = value.strip()
    if text != value:
        raise EventInspectionError(f"{field} must not contain surrounding whitespace")
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise EventInspectionError(f"{field} must be a valid RFC 3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EventInspectionError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc).timestamp()


class EventInspector:
    """Read event state without migration, writes, or secret resolution."""

    def __init__(self, database_path):
        self.database_path = Path(database_path).expanduser().resolve(strict=False)

    def _connect(self):
        connection = _readonly_connection(self.database_path)
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version < 2:
            connection.close()
            raise EventInspectionError(
                f"event inspection requires runtime schema version 2 or newer; found {version}"
            )
        return connection, version

    def list_events(
        self,
        *,
        lifecycle_state="",
        reason_code="",
        camera_id="",
        branch="",
        direction="",
        employee="",
        from_time="",
        to_time="",
        limit=50,
        offset=0,
        order="newest",
    ):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= READ_ONLY_LIMIT_MAX:
            raise EventInspectionError(
                f"limit must be an integer between 1 and {READ_ONLY_LIMIT_MAX}"
            )
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise EventInspectionError("offset must be a non-negative integer")
        if order not in {"newest", "oldest"}:
            raise EventInspectionError("order must be newest or oldest")

        connection, version = self._connect()
        try:
            columns = _table_columns(connection, "camera_events")
            state_column = "lifecycle_state" if "lifecycle_state" in columns else "status"
            time_column = "received_unix" if "received_unix" in columns else "created_unix"
            where = []
            params = []

            def add_equal(column, value):
                if value not in (None, ""):
                    where.append(f"e.{column} = ?")
                    params.append(str(value))

            add_equal(state_column, lifecycle_state)
            if "reason_code" in columns:
                add_equal("reason_code", reason_code)
            elif reason_code:
                raise EventInspectionError("reason filtering requires runtime schema version 2")
            add_equal("camera_id", camera_id)
            if "branch" in columns:
                add_equal("branch", branch)
            elif branch:
                raise EventInspectionError("branch filtering requires runtime schema version 2")
            add_equal("log_type", direction)
            if employee:
                where.append(
                    "EXISTS (SELECT 1 FROM recognition_decisions d "
                    "WHERE d.event_id = e.event_id AND d.best_employee = ?)"
                )
                params.append(str(employee))
            start_unix = _timestamp_unix(from_time, "from_time")
            end_unix = _timestamp_unix(to_time, "to_time")
            if start_unix is not None:
                where.append(f"e.{time_column} >= ?")
                params.append(start_unix)
            if end_unix is not None:
                where.append(f"e.{time_column} <= ?")
                params.append(end_unix)
            if start_unix is not None and end_unix is not None and start_unix > end_unix:
                raise EventInspectionError("from_time must not be later than to_time")

            predicate = " WHERE " + " AND ".join(where) if where else ""
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM camera_events e{predicate}", params
                ).fetchone()[0]
            )
            processing_phase = (
                "e.processing_phase" if "processing_phase" in columns else "''"
            )
            received_at = "e.received_at" if "received_at" in columns else "''"
            effective_at = "e.effective_at" if "effective_at" in columns else "''"
            branch_expr = "e.branch" if "branch" in columns else "''"
            reason_expr = "e.reason_code" if "reason_code" in columns else "''"
            final_expr = (
                "e.final_disposition" if "final_disposition" in columns else "''"
            )
            capture_expr = "e.capture_id" if "capture_id" in columns else "''"
            rows = connection.execute(
                f"""
                SELECT
                    e.event_id,
                    {capture_expr} AS capture_id,
                    e.camera_id,
                    e.log_type AS direction,
                    {branch_expr} AS branch,
                    e.{state_column} AS lifecycle_state,
                    e.status,
                    {reason_expr} AS reason_code,
                    {processing_phase} AS processing_phase,
                    {received_at} AS received_at,
                    {effective_at} AS effective_at,
                    {final_expr} AS final_disposition,
                    e.source_name,
                    e.source_size,
                    (SELECT COUNT(*) FROM recognition_decisions d
                     WHERE d.event_id = e.event_id) AS decision_count,
                    (SELECT COUNT(*) FROM recognition_decisions d
                     WHERE d.event_id = e.event_id AND d.accepted = 1) AS accepted_decision_count,
                    COALESCE((SELECT d.best_employee FROM recognition_decisions d
                     WHERE d.event_id = e.event_id AND d.accepted = 1
                     ORDER BY d.created_at, d.decision_id LIMIT 1), '') AS employee
                FROM camera_events e
                {predicate}
                ORDER BY e.{time_column} {'DESC' if order == 'newest' else 'ASC'}, e.event_id
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
            items = [redact_event_value(dict(row)) for row in rows]
            return {
                "database_schema_version": version,
                "total": total,
                "limit": limit,
                "offset": offset,
                "order": order,
                "items": items,
                "read_only": True,
            }
        finally:
            connection.close()

    def inspect_event(self, event_id):
        event_id = _identifier(event_id, "event_id")
        connection, version = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM camera_events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if row is None:
                return None
            event = dict(row)
            if "receipt_json" in event:
                event["receipt"] = _parse_json(event.pop("receipt_json"))
            if "receipt_verified" in event:
                event["receipt_verified"] = bool(event["receipt_verified"])

            transitions = []
            if _table_exists(connection, "event_transitions"):
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
                    item["detail"] = _parse_json(item.pop("detail_json"))

            decisions = []
            if _table_exists(connection, "recognition_decisions"):
                decisions = [
                    dict(item)
                    for item in connection.execute(
                        """
                        SELECT * FROM recognition_decisions
                        WHERE event_id = ? ORDER BY decision_version, face_index
                        """,
                        (event_id,),
                    ).fetchall()
                ]
                for item in decisions:
                    for boolean_key in ("pad_passed", "pad_skipped", "accepted"):
                        if boolean_key in item:
                            item[boolean_key] = bool(item[boolean_key])

            actions = []
            if _table_exists(connection, "operator_actions"):
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
                    item["detail"] = _parse_json(item.pop("detail_json"))

            policy = []
            if _table_exists(connection, "attendance_policy_state"):
                policy = [
                    dict(item)
                    for item in connection.execute(
                        """
                        SELECT * FROM attendance_policy_state
                        WHERE committed_event_id = ? OR reservation_event_id = ?
                        ORDER BY scope_key
                        """,
                        (event_id, event_id),
                    ).fetchall()
                ]

            return redact_event_value(
                {
                    "database_schema_version": version,
                    "event": event,
                    "transitions": transitions,
                    "decisions": decisions,
                    "operator_actions": actions,
                    "attendance_policy": policy,
                    "read_only": True,
                    "biometric_vectors_exposed": False,
                }
            )
        finally:
            connection.close()

    def explain_event(self, event_id, *, now=None):
        inspected = self.inspect_event(event_id)
        if inspected is None:
            return None
        now = time.time() if now is None else float(now)
        event = inspected["event"]
        state = str(event.get("lifecycle_state") or event.get("status") or "unknown")
        reason = str(event.get("reason_code") or "")
        delivery_started = bool(
            event.get("delivery_started_at")
            or event.get("delivery_decision_id")
            or event.get("processing_phase") == "delivery_in_progress"
        )
        lease_active = bool(
            event.get("lease_owner")
            and float(event.get("lease_expires_unix") or 0) > now
        )
        uncertain_policy = any(
            item.get("reservation_state") == "uncertain"
            for item in inspected.get("attendance_policy", [])
        )
        confirmed_delivery = state == "checkin_created"
        reprocess_eligible = bool(
            state in REPROCESSABLE_STATES
            and not delivery_started
            and not lease_active
            and not uncertain_policy
        )
        dismiss_eligible = bool(
            state in DISMISSIBLE_STATES
            and not delivery_started
            and not lease_active
            and not uncertain_policy
        )

        if state == "checkin_created":
            headline = "ERPNext check-in creation was reported successful."
            recommended = (
                "Use the ERPNext record for any delivered check-in correction or deletion; "
                "the local event CLI does not cancel delivered records."
            )
        elif state == "uncertain" or delivery_started:
            headline = "ERPNext delivery may have started and its outcome is not proven."
            recommended = (
                "Reconcile this event against ERPNext. Automatic or operator delivery retry "
                "is intentionally unavailable until Phase 2 idempotency is active."
            )
        elif event.get("retention_state") == "quarantined":
            headline = "The event is terminal and its source evidence is quarantined."
            recommended = (
                "Review the source and receipt, then use resolve-quarantine retain or requeue. "
                "Requeue requires the watcher to be stopped."
            )
        elif reprocess_eligible:
            headline = "The event failed or was rejected before remote delivery began."
            recommended = (
                "After correcting the cause, verifying retained source evidence, and stopping "
                "the watcher, use the audited reprocess command."
            )
        elif state == "dismissed":
            headline = "An operator dismissed this event without remote delivery."
            recommended = "Review the operator action history if the dismissal must be revisited."
        elif state == "processed":
            headline = "Recognition processing completed without creating a check-in."
            recommended = "Review face decisions and policy evidence; no delivery retry exists in Phase 1."
        else:
            headline = f"The event is currently {state} with reason {reason or 'unspecified'}."
            recommended = "Review the transition and decision timeline before taking action."

        decision_summary = []
        for decision in inspected.get("decisions", []):
            decision_summary.append(
                {
                    "decision_id": decision.get("decision_id", ""),
                    "decision_version": decision.get("decision_version", 0),
                    "face_index": decision.get("face_index", 0),
                    "accepted": bool(decision.get("accepted")),
                    "employee": decision.get("best_employee", ""),
                    "reason_code": decision.get("reason_code", ""),
                    "best_score": decision.get("best_score"),
                    "runner_up_score": decision.get("runner_up_score"),
                    "score_margin": decision.get("score_margin"),
                    "pad_passed": bool(decision.get("pad_passed")),
                    "pad_score": decision.get("pad_score"),
                    "gallery_version": decision.get("gallery_version", ""),
                    "recognition_model_version": decision.get(
                        "recognition_model_version", ""
                    ),
                }
            )

        timeline = []
        for transition in inspected.get("transitions", []):
            timeline.append(
                {
                    "kind": "transition",
                    "at": transition.get("created_at", ""),
                    "sequence": transition.get("sequence", 0),
                    "summary": (
                        f"{transition.get('from_state') or '<start>'} -> "
                        f"{transition.get('to_state')} ({transition.get('reason_code')})"
                    ),
                    "actor": transition.get("actor_id")
                    or transition.get("actor_type", ""),
                    "detail": transition.get("detail", {}),
                }
            )
        for action in inspected.get("operator_actions", []):
            timeline.append(
                {
                    "kind": "operator_action",
                    "at": action.get("created_at", ""),
                    "sequence": None,
                    "summary": action.get("action", ""),
                    "actor": action.get("actor", ""),
                    "detail": action.get("detail", {}),
                }
            )
        timeline.sort(key=lambda item: (str(item.get("at") or ""), item.get("sequence") or 0))

        return redact_event_value(
            {
                "event_id": event.get("event_id", event_id),
                "headline": headline,
                "current_state": state,
                "current_reason": reason,
                "delivery_safety": {
                    "delivery_started": delivery_started,
                    "confirmed_delivery": confirmed_delivery,
                    "uncertain_policy_scope": uncertain_policy,
                    "automatic_retry_allowed": False,
                    "operator_reprocess_allowed": reprocess_eligible,
                },
                "operator_eligibility": {
                    "reprocess": reprocess_eligible,
                    "dismiss": dismiss_eligible,
                    "delivery_retry": False,
                    "delivery_cancel": False,
                },
                "source": {
                    "camera_id": event.get("camera_id", ""),
                    "branch": event.get("branch", ""),
                    "direction": event.get("log_type", ""),
                    "receipt_state": event.get("receipt_state", ""),
                    "receipt_verified": bool(event.get("receipt_verified")),
                    "retention_state": event.get("retention_state", ""),
                },
                "runtime_versions": {
                    "gallery_version": event.get("gallery_version", ""),
                    "gallery_model": event.get("gallery_model", ""),
                    "gallery_model_version": event.get("gallery_model_version", ""),
                    "recognition_model": event.get("recognition_model", ""),
                    "recognition_model_version": event.get(
                        "recognition_model_version", ""
                    ),
                    "preprocessing_version": event.get("preprocessing_version", ""),
                    "pad_provider": event.get("pad_provider", ""),
                    "pad_model": event.get("pad_model", ""),
                    "policy_version": event.get("policy_version", ""),
                },
                "decision_summary": decision_summary,
                "timeline": timeline,
                "recommended_action": recommended,
                "read_only": True,
                "biometric_vectors_exposed": False,
            }
        )


def _operator_actor_reason(actor, reason):
    actor = _text(actor, "actor", required=True, max_chars=256)
    reason = _text(reason, "operator reason", required=True, max_chars=OPERATOR_REASON_MAX_CHARS)
    if len(reason) < OPERATOR_REASON_MIN_CHARS:
        raise EventOperationError(
            f"operator reason must contain at least {OPERATOR_REASON_MIN_CHARS} characters"
        )
    return actor, reason


class EventOperationsMixin:
    """Atomic, audited Phase 1 operator actions. No delivery retry lives here."""

    @staticmethod
    def _insert_operator_action_tx(
        connection,
        *,
        event_id,
        actor,
        action,
        reason,
        detail,
        created_at,
    ):
        payload = dict(detail or {})
        payload["operator_reason"] = reason
        action_id = hashlib.sha256(
            "\0".join((event_id, actor, action, created_at, reason)).encode("utf-8")
        ).hexdigest()
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
    def _active_lease(row, now):
        return bool(row["lease_owner"]) and float(row["lease_expires_unix"] or 0) > now

    @staticmethod
    def _delivery_started(row):
        return bool(
            row["delivery_started_at"]
            or row["delivery_decision_id"]
            or row["processing_phase"] == "delivery_in_progress"
        )

    @staticmethod
    def _uncertain_policy_tx(connection, event_id):
        return connection.execute(
            """
            SELECT 1 FROM attendance_policy_state
            WHERE reservation_event_id = ? AND reservation_state = 'uncertain'
            LIMIT 1
            """,
            (event_id,),
        ).fetchone() is not None

    def operator_dismiss_event(self, event_id, *, actor, reason, now=None):
        event_id = _identifier(event_id, "event_id")
        actor, reason = _operator_actor_reason(actor, reason)
        now = _finite(
            now if now is not None else datetime.now(timezone.utc).timestamp(),
            "now",
            minimum=0,
        )
        created_at = timestamp_from_unix(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM camera_events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if row is None:
                raise EventOperationError(f"event does not exist: {event_id}")
            if row["lifecycle_state"] not in DISMISSIBLE_STATES:
                raise EventOperationError(
                    f"event state cannot be dismissed: {row['lifecycle_state']}"
                )
            if self._active_lease(row, now):
                raise EventOperationError("event has an active processing lease")
            if self._delivery_started(row):
                raise EventOperationError(
                    "event crossed the delivery boundary; reconcile it instead of dismissing it"
                )
            if self._uncertain_policy_tx(connection, event_id):
                raise EventOperationError(
                    "event has an uncertain attendance-policy reservation"
                )
            self._release_policy_for_event_tx(connection, event_id, uncertain=False)
            action_id = self._insert_operator_action_tx(
                connection,
                event_id=event_id,
                actor=actor,
                action="dismissed",
                reason=reason,
                detail={
                    "previous_state": row["lifecycle_state"],
                    "previous_reason_code": row["reason_code"],
                    "previous_final_disposition": row["final_disposition"],
                },
                created_at=created_at,
            )
            self._transition_tx(
                connection,
                row,
                to_state="dismissed",
                reason_code="operator_action",
                detail={
                    "action": "dismissed",
                    "action_id": action_id,
                    "operator_reason": reason,
                },
                compatibility_status="dismissed",
                actor_type="operator",
                actor_id=actor,
                terminal=True,
                now=now,
            )
            connection.execute(
                "UPDATE camera_events SET final_disposition = 'dismissed' WHERE event_id = ?",
                (event_id,),
            )
            connection.commit()
            return {
                "event_id": event_id,
                "action_id": action_id,
                "action": "dismissed",
                "actor": actor,
                "state": "dismissed",
                "delivery_retry_created": False,
            }
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def operator_reprocess_event(
        self,
        event_id,
        *,
        actor,
        reason,
        source_path,
        action="reprocess_requested",
        now=None,
    ):
        event_id = _identifier(event_id, "event_id")
        actor, reason = _operator_actor_reason(actor, reason)
        action = _text(action, "action", required=True, max_chars=128)
        source_path = _text(str(source_path), "source_path", required=True, max_chars=4096)
        now = _finite(
            now if now is not None else datetime.now(timezone.utc).timestamp(),
            "now",
            minimum=0,
        )
        created_at = timestamp_from_unix(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM camera_events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if row is None:
                raise EventOperationError(f"event does not exist: {event_id}")
            if row["lifecycle_state"] not in REPROCESSABLE_STATES:
                raise EventOperationError(
                    f"event state cannot be reprocessed: {row['lifecycle_state']}"
                )
            if self._active_lease(row, now):
                raise EventOperationError("event has an active processing lease")
            if self._delivery_started(row):
                raise EventOperationError(
                    "event crossed the delivery boundary; delivery retry is unavailable before Phase 2"
                )
            if self._uncertain_policy_tx(connection, event_id):
                raise EventOperationError(
                    "event has an uncertain attendance-policy reservation"
                )
            self._release_policy_for_event_tx(connection, event_id, uncertain=False)
            action_id = self._insert_operator_action_tx(
                connection,
                event_id=event_id,
                actor=actor,
                action=action,
                reason=reason,
                detail={
                    "source_path": source_path,
                    "previous_state": row["lifecycle_state"],
                    "previous_reason_code": row["reason_code"],
                    "previous_processing_attempt": int(row["processing_attempt"]),
                },
                created_at=created_at,
            )
            self._transition_tx(
                connection,
                row,
                to_state="received",
                reason_code="operator_action",
                detail={
                    "action": action,
                    "action_id": action_id,
                    "operator_reason": reason,
                    "source_path": source_path,
                },
                compatibility_status="received",
                actor_type="operator",
                actor_id=actor,
                error="",
                column_updates={
                    "completed_at": None,
                    "final_disposition": "",
                    "processing_phase": "idle",
                    "lease_owner": "",
                    "lease_acquired_at": "",
                    "lease_heartbeat_at": "",
                    "lease_expires_unix": 0.0,
                    "delivery_started_at": "",
                    "delivery_decision_id": "",
                    "retention_state": "retained",
                    "recovery_count": int(row["recovery_count"]) + 1,
                },
                terminal=False,
                now=now,
            )
            connection.commit()
            return {
                "event_id": event_id,
                "action_id": action_id,
                "action": action,
                "actor": actor,
                "state": "received",
                "source_path": source_path,
                "delivery_retry_created": False,
            }
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def operator_record_quarantine_resolution(
        self,
        event_id,
        *,
        actor,
        reason,
        source_path,
        resolution="retain",
        now=None,
    ):
        event_id = _identifier(event_id, "event_id")
        actor, reason = _operator_actor_reason(actor, reason)
        resolution = _text(
            resolution, "quarantine resolution", required=True, max_chars=32
        )
        if resolution != "retain":
            raise EventOperationError(
                "record-only quarantine resolution supports retain; use requeue for reprocessing"
            )
        source_path = _text(str(source_path), "source_path", required=True, max_chars=4096)
        now = _finite(
            now if now is not None else datetime.now(timezone.utc).timestamp(),
            "now",
            minimum=0,
        )
        created_at = timestamp_from_unix(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM camera_events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if row is None:
                raise EventOperationError(f"event does not exist: {event_id}")
            action_id = self._insert_operator_action_tx(
                connection,
                event_id=event_id,
                actor=actor,
                action="quarantine_retained",
                reason=reason,
                detail={
                    "source_path": source_path,
                    "resolution": "retain",
                    "event_state": row["lifecycle_state"],
                },
                created_at=created_at,
            )
            connection.execute(
                """
                UPDATE camera_events
                SET retention_state = 'quarantined', updated_unix = ?
                WHERE event_id = ?
                """,
                (now, event_id),
            )
            connection.commit()
            return {
                "event_id": event_id,
                "action_id": action_id,
                "action": "quarantine_retained",
                "actor": actor,
                "state": row["lifecycle_state"],
                "source_path": source_path,
                "delivery_retry_created": False,
            }
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


class EventSourceResolver:
    """Validate and move retained source evidence without resolving unrelated secrets."""

    def __init__(self, config_path):
        raw_path = Path(config_path).expanduser()
        if raw_path.is_symlink():
            raise EventOperationError(f"config must not be a symbolic link: {raw_path}")
        self.config_path = raw_path.resolve(strict=False)
        self.root = self.config_path.parent
        try:
            self.document = load_config_document(self.config_path)
        except ConfigLoadError as exc:
            raise EventOperationError(str(exc)) from exc

    @staticmethod
    def _safe_source_name(value):
        name = _text(value, "source_name", required=True, max_chars=1024)
        if Path(name).name != name or name in {".", ".."} or name.startswith("."):
            raise EventOperationError("event source_name is not a safe basename")
        return name

    @staticmethod
    def _file_sha256(path, *, max_bytes=1024 * 1024 * 1024):
        path = Path(path)
        metadata = path.lstat()
        if path.is_symlink() or not path.is_file():
            raise EventOperationError(f"source evidence must be a regular non-symlink file: {path}")
        if metadata.st_size > max_bytes:
            raise EventOperationError(f"source evidence exceeds {max_bytes} bytes")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest(), metadata.st_size

    def _resolved_secret(self, value, field):
        try:
            resolved, _source = resolve_secret_reference(value, field=field)
            return resolved
        except SecretStoreError as exc:
            raise EventOperationError(str(exc)) from exc

    def _camera_config(self, event):
        camera_id = _text(event.get("camera_id"), "camera_id", required=True, max_chars=128)
        raw_sources = self.document.get("camera_sources")
        if not isinstance(raw_sources, dict) or camera_id not in raw_sources:
            raise EventOperationError(f"camera is not configured: {camera_id}")
        camera_item = raw_sources[camera_id]
        if not isinstance(camera_item, dict):
            raise EventOperationError(f"camera source is invalid: {camera_id}")
        username = camera_item.get("ftp_username")
        raw_users = self.document.get("ftp_users")
        if not isinstance(raw_users, dict) or username not in raw_users:
            raise EventOperationError(f"camera FTP user is not configured: {username}")
        user_item = raw_users[username]
        if not isinstance(user_item, dict):
            raise EventOperationError(f"camera FTP user is invalid: {username}")
        resolved_user = dict(user_item)
        resolved_user["password"] = self._resolved_secret(
            user_item.get("password"), f"ftp_users.{username}.password"
        )
        minimal = {
            "production_mode": bool(self.document.get("production_mode", False)),
            "branch_name": self.document.get("branch_name", ""),
            "camera_uploads_dir": self.document.get(
                "camera_uploads_dir", str(self.root / "camera_uploads")
            ),
            "camera_sources": {camera_id: dict(camera_item)},
            "ftp_users": {username: resolved_user},
            "ftp_permissions": self.document.get("ftp_permissions", "elw"),
            "camera_source_receipt_required": self.document.get(
                "camera_source_receipt_required", True
            ),
            "camera_source_receipt_secret": self._resolved_secret(
                self.document.get("camera_source_receipt_secret"),
                "camera_source_receipt_secret",
            ),
            "camera_source_receipt_future_tolerance_seconds": self.document.get(
                "camera_source_receipt_future_tolerance_seconds", 300
            ),
        }
        try:
            sources = load_camera_sources(minimal, self.root)
        except CameraSourceError as exc:
            raise EventOperationError(f"camera source configuration is invalid: {exc}") from exc
        source = sources[0]
        if event.get("branch") and event.get("branch") != source.branch:
            raise EventOperationError("event branch does not match the configured camera")
        if event.get("log_type") and event.get("log_type") != source.policy:
            raise EventOperationError("event direction does not match the configured camera")
        if event.get("source_principal") and event.get("source_principal") != source.ftp_username:
            raise EventOperationError("event source principal does not match the configured camera")
        return minimal, source, sources

    def _verify_content(self, event, path):
        digest, size = self._file_sha256(path)
        if digest != str(event.get("source_sha256") or ""):
            raise EventOperationError("source evidence SHA-256 does not match the event")
        if int(size) != int(event.get("source_size") or -1):
            raise EventOperationError("source evidence size does not match the event")
        return digest, size

    def verify_original_source(self, event):
        cfg, source, sources = self._camera_config(event)
        name = self._safe_source_name(event.get("source_name"))
        path = source.upload_dir / name
        digest, size = self._verify_content(event, path)
        try:
            verified_source, source_receipt = verify_source_receipt(
                path,
                cfg,
                self.root,
                source_sha256=digest,
                source_size=size,
                sources=sources,
            )
        except CameraSourceError as exc:
            raise EventOperationError(f"source receipt verification failed: {exc}") from exc
        if verified_source != source or not source_receipt.verified:
            raise EventOperationError("source receipt is not fully verified")
        return path.resolve()

    def find_quarantine_source(self, event, *, max_scan=QUARANTINE_SCAN_LIMIT_DEFAULT):
        if isinstance(max_scan, bool) or not isinstance(max_scan, int):
            raise EventOperationError("max_scan must be an integer")
        if not 1 <= max_scan <= QUARANTINE_SCAN_LIMIT_MAX:
            raise EventOperationError(
                f"max_scan must be between 1 and {QUARANTINE_SCAN_LIMIT_MAX}"
            )
        name = self._safe_source_name(event.get("source_name"))
        quarantine_root = (self.root / "logs" / "quarantine").resolve(strict=False)
        if quarantine_root.is_symlink():
            raise EventOperationError("quarantine root must not be a symbolic link")
        if not quarantine_root.is_dir():
            raise EventOperationError(f"quarantine directory does not exist: {quarantine_root}")
        matches = []
        scanned = 0
        for candidate in quarantine_root.rglob("*"):
            if candidate.name.endswith(".source.json"):
                continue
            if not candidate.is_file() or candidate.is_symlink():
                continue
            scanned += 1
            if scanned > max_scan:
                raise EventOperationError(
                    f"quarantine scan exceeded {max_scan} regular files"
                )
            if candidate.name != name and not candidate.name.endswith("_" + name):
                continue
            try:
                self._verify_content(event, candidate)
            except EventOperationError:
                continue
            matches.append(candidate.resolve())
        if not matches:
            raise EventOperationError("matching source evidence was not found in quarantine")
        if len(matches) != 1:
            raise EventOperationError(
                f"quarantine contains {len(matches)} matching source files; resolve ambiguity manually"
            )
        if not receipt_path(matches[0]).is_file():
            raise EventOperationError("matching quarantine source receipt is missing")
        return matches[0]

    @staticmethod
    def _move(source, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(source, destination)
        except OSError:
            shutil.move(str(source), str(destination))

    def requeue_quarantine_source(self, event, *, max_scan=QUARANTINE_SCAN_LIMIT_DEFAULT):
        cfg, source, sources = self._camera_config(event)
        quarantine_image = self.find_quarantine_source(event, max_scan=max_scan)
        quarantine_receipt = receipt_path(quarantine_image)
        destination_image = source.upload_dir / self._safe_source_name(event.get("source_name"))
        destination_receipt = receipt_path(destination_image)
        if destination_image.exists() or destination_receipt.exists():
            raise EventOperationError(
                "destination upload or receipt already exists; no files were moved"
            )
        self._move(quarantine_image, destination_image)
        try:
            self._move(quarantine_receipt, destination_receipt)
            digest, size = self._verify_content(event, destination_image)
            verified_source, source_receipt = verify_source_receipt(
                destination_image,
                cfg,
                self.root,
                source_sha256=digest,
                source_size=size,
                sources=sources,
            )
            if verified_source != source or not source_receipt.verified:
                raise EventOperationError("requeued source receipt is not fully verified")
        except Exception as exc:
            try:
                if destination_receipt.exists():
                    self._move(destination_receipt, quarantine_receipt)
                if destination_image.exists():
                    self._move(destination_image, quarantine_image)
            except Exception as rollback_exc:
                raise EventOperationError(
                    f"requeue validation failed and file rollback also failed: {rollback_exc}"
                ) from exc
            if isinstance(exc, EventOperationError):
                raise
            if isinstance(exc, CameraSourceError):
                raise EventOperationError(f"requeued source receipt is invalid: {exc}") from exc
            raise EventOperationError(f"could not requeue quarantine source: {exc}") from exc
        return QuarantineMove(
            quarantine_image=quarantine_image,
            quarantine_receipt=quarantine_receipt,
            destination_image=destination_image.resolve(),
            destination_receipt=destination_receipt.resolve(),
        )

    def rollback_requeue(self, move):
        errors = []
        try:
            if move.destination_receipt.exists():
                self._move(move.destination_receipt, move.quarantine_receipt)
        except Exception as exc:
            errors.append(str(exc))
        try:
            if move.destination_image.exists():
                self._move(move.destination_image, move.quarantine_image)
        except Exception as exc:
            errors.append(str(exc))
        if errors:
            raise EventOperationError(
                "database update failed and source rollback was incomplete: " + "; ".join(errors)
            )
