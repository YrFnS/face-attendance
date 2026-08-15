import hashlib
import json
import math
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone

from event_ledger import EVENT_REASON_CODES, EVENT_STATES, TERMINAL_EVENT_STATES


HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
PROCESSING_PHASES = frozenset(
    {"idle", "pre_delivery", "delivery_in_progress", "terminal"}
)
POLICY_RESERVATION_STATES = frozenset({"none", "pending", "uncertain"})


def _sql_values(values):
    return ", ".join("'" + value.replace("'", "''") + "'" for value in sorted(values))


_PHASE_SQL = _sql_values(PROCESSING_PHASES)
_RESERVATION_SQL = _sql_values(POLICY_RESERVATION_STATES)


RECOVERY_SCHEMA_STATEMENTS = (
    "ALTER TABLE camera_events ADD COLUMN processing_attempt INTEGER NOT NULL DEFAULT 0 CHECK(processing_attempt >= 0)",
    "ALTER TABLE camera_events ADD COLUMN lease_owner TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE camera_events ADD COLUMN lease_acquired_at TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE camera_events ADD COLUMN lease_heartbeat_at TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE camera_events ADD COLUMN lease_expires_unix REAL NOT NULL DEFAULT 0",
    "ALTER TABLE camera_events ADD COLUMN recovery_count INTEGER NOT NULL DEFAULT 0 CHECK(recovery_count >= 0)",
    f"ALTER TABLE camera_events ADD COLUMN processing_phase TEXT NOT NULL DEFAULT 'idle' CHECK(processing_phase IN ({_PHASE_SQL}))",
    "ALTER TABLE camera_events ADD COLUMN delivery_started_at TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE camera_events ADD COLUMN delivery_decision_id TEXT NOT NULL DEFAULT ''",
    """
    UPDATE camera_events
    SET processing_phase = CASE
        WHEN lifecycle_state IN ('processed', 'checkin_created', 'rejected', 'failed', 'uncertain', 'dismissed')
        THEN 'terminal'
        ELSE 'idle'
    END
    """,
    f"""
    CREATE TABLE attendance_policy_state (
        scope_key TEXT PRIMARY KEY,
        employee TEXT NOT NULL,
        branch TEXT NOT NULL,
        direction TEXT NOT NULL CHECK(direction IN ('IN', 'OUT')),
        policy_version TEXT NOT NULL,
        committed_event_id TEXT NOT NULL DEFAULT '',
        committed_decision_id TEXT NOT NULL DEFAULT '',
        committed_effective_at TEXT NOT NULL DEFAULT '',
        committed_effective_unix REAL NOT NULL DEFAULT 0,
        reservation_event_id TEXT NOT NULL DEFAULT '',
        reservation_decision_id TEXT NOT NULL DEFAULT '',
        reservation_effective_at TEXT NOT NULL DEFAULT '',
        reservation_effective_unix REAL NOT NULL DEFAULT 0,
        reservation_state TEXT NOT NULL DEFAULT 'none' CHECK(reservation_state IN ({_RESERVATION_SQL})),
        reservation_expires_unix REAL NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX camera_events_processing_lease ON camera_events(lifecycle_state, lease_expires_unix)",
    "CREATE INDEX attendance_policy_reservation ON attendance_policy_state(reservation_state, reservation_expires_unix)",
    "CREATE INDEX attendance_policy_event ON attendance_policy_state(reservation_event_id)",
)


RECOVERY_REQUIRED_TABLE_COLUMNS = {
    "camera_events": {
        "processing_attempt": ("INTEGER", True, 0),
        "lease_owner": ("TEXT", True, 0),
        "lease_acquired_at": ("TEXT", True, 0),
        "lease_heartbeat_at": ("TEXT", True, 0),
        "lease_expires_unix": ("REAL", True, 0),
        "recovery_count": ("INTEGER", True, 0),
        "processing_phase": ("TEXT", True, 0),
        "delivery_started_at": ("TEXT", True, 0),
        "delivery_decision_id": ("TEXT", True, 0),
    },
    "attendance_policy_state": {
        "scope_key": ("TEXT", False, 1),
        "employee": ("TEXT", True, 0),
        "branch": ("TEXT", True, 0),
        "direction": ("TEXT", True, 0),
        "policy_version": ("TEXT", True, 0),
        "committed_event_id": ("TEXT", True, 0),
        "committed_decision_id": ("TEXT", True, 0),
        "committed_effective_at": ("TEXT", True, 0),
        "committed_effective_unix": ("REAL", True, 0),
        "reservation_event_id": ("TEXT", True, 0),
        "reservation_decision_id": ("TEXT", True, 0),
        "reservation_effective_at": ("TEXT", True, 0),
        "reservation_effective_unix": ("REAL", True, 0),
        "reservation_state": ("TEXT", True, 0),
        "reservation_expires_unix": ("REAL", True, 0),
        "updated_at": ("TEXT", True, 0),
    },
}

RECOVERY_REQUIRED_INDEXES = {
    "camera_events_processing_lease": (
        False,
        ("lifecycle_state", "lease_expires_unix"),
    ),
    "attendance_policy_reservation": (
        False,
        ("reservation_state", "reservation_expires_unix"),
    ),
    "attendance_policy_event": (False, ("reservation_event_id",)),
}


class ProcessingRecoveryError(RuntimeError):
    pass


class ProcessingLeaseError(ProcessingRecoveryError):
    pass


class AttendancePolicyError(ProcessingRecoveryError):
    pass


@dataclass(frozen=True)
class ProcessingLeaseClaim:
    accepted: bool
    event_id: str
    attempt: int = 0
    reason: str = ""
    existing_status: str = ""
    lease_owner: str = ""
    lease_expires_unix: float = 0.0
    recovered: bool = False


@dataclass(frozen=True)
class RecoveryOutcome:
    event_id: str
    outcome: str
    camera_id: str
    source_name: str
    lifecycle_state: str
    processing_attempt: int
    delivery_decision_id: str = ""
    source_path: str = ""
    retention_path: str = ""


@dataclass(frozen=True)
class PolicyReservation:
    accepted: bool
    scope_key: str
    reason: str
    remaining_seconds: int = 0
    existing_event_id: str = ""
    existing_decision_id: str = ""


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
        raise ProcessingRecoveryError(f"{field} must be a string")
    else:
        text = unicodedata.normalize("NFC", value)
    if text != text.strip():
        raise ProcessingRecoveryError(
            f"{field} must not contain surrounding whitespace"
        )
    if required and not text:
        raise ProcessingRecoveryError(f"{field} is required")
    if len(text) > int(max_chars):
        raise ProcessingRecoveryError(
            f"{field} exceeds {int(max_chars)} characters"
        )
    for character in text:
        if unicodedata.category(character) in {"Cc", "Cf", "Cs"}:
            raise ProcessingRecoveryError(
                f"{field} contains a control or formatting character"
            )
    return text


def _safe_error(value, max_chars=2000):
    raw = str(value or "")[: int(max_chars)]
    return "".join(
        character
        if unicodedata.category(character) not in {"Cc", "Cf", "Cs"}
        else f"\\u{ord(character):04x}"
        for character in raw
    )


def _identifier(value, field):
    text = _text(value, field, required=True, max_chars=64).lower()
    if not HEX64_RE.fullmatch(text):
        raise ProcessingRecoveryError(
            f"{field} must be a lowercase 64-character SHA-256 identifier"
        )
    return text


def _strict_int(value, field, *, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProcessingRecoveryError(f"{field} must be an integer")
    if value < int(minimum) or value > int(maximum):
        raise ProcessingRecoveryError(
            f"{field} must be between {int(minimum)} and {int(maximum)}"
        )
    return value


def _finite(value, field, *, minimum=None, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProcessingRecoveryError(f"{field} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ProcessingRecoveryError(f"{field} must be finite")
    if minimum is not None and result < float(minimum):
        raise ProcessingRecoveryError(f"{field} is below {minimum}")
    if maximum is not None and result > float(maximum):
        raise ProcessingRecoveryError(f"{field} exceeds {maximum}")
    return result


def _timestamp(value, field, *, required=True):
    text = _text(value, field, required=required, max_chars=64)
    if not text:
        return ""
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ProcessingRecoveryError(
            f"{field} must be a valid RFC 3339 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProcessingRecoveryError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _timestamp_unix(value, field):
    normalized = _timestamp(value, field)
    return normalized, datetime.fromisoformat(
        normalized.replace("Z", "+00:00")
    ).timestamp()


def _json_text(value, field, *, max_bytes=32768):
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ProcessingRecoveryError(f"{field} must be a JSON object")
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ProcessingRecoveryError(f"{field} is not JSON-safe") from exc
    if len(encoded.encode("utf-8")) > int(max_bytes):
        raise ProcessingRecoveryError(
            f"{field} exceeds {int(max_bytes)} UTF-8 bytes"
        )
    return encoded


def attendance_policy_scope_key(employee, direction, branch, policy_version):
    employee = _text(employee, "employee", required=True, max_chars=180)
    direction = _text(direction, "direction", required=True, max_chars=16)
    if direction not in {"IN", "OUT"}:
        raise AttendancePolicyError("direction must be IN or OUT")
    branch = _text(branch, "branch", required=True, max_chars=128)
    policy_version = _text(
        policy_version, "policy_version", required=True, max_chars=128
    )
    payload = "\0".join((employee, direction, branch, policy_version))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def configuration_issues(cfg):
    issues = []

    def configured_int(key, default, minimum, maximum):
        value = cfg.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int):
            issues.append(f"{key} must be an integer")
            return default
        if value < minimum or value > maximum:
            issues.append(f"{key} must be between {minimum} and {maximum}")
            return default
        return value

    lease_seconds = configured_int(
        "event_processing_lease_seconds", 180, 30, 3600
    )
    reservation_seconds = configured_int(
        "attendance_policy_reservation_seconds", 300, 30, 86400
    )
    configured_int("cooldown_seconds", 600, 0, 86400)
    if reservation_seconds < lease_seconds:
        issues.append(
            "attendance_policy_reservation_seconds must be at least "
            "event_processing_lease_seconds"
        )
    recovery_enabled = cfg.get("event_startup_recovery_enabled", True)
    if not isinstance(recovery_enabled, bool):
        issues.append("event_startup_recovery_enabled must be a boolean")
    elif bool(cfg.get("production_mode", False)) and not recovery_enabled:
        issues.append("event_startup_recovery_enabled must be true in production")
    return issues


class ProcessingRecoveryMixin:
    @staticmethod
    def _transition_tx(
        connection,
        row,
        *,
        to_state,
        reason_code,
        detail,
        compatibility_status,
        actor_type="watcher",
        actor_id="",
        error="",
        column_updates=None,
        terminal=False,
        now=None,
    ):
        if to_state not in EVENT_STATES:
            raise ProcessingLeaseError(f"unsupported event state: {to_state}")
        if reason_code not in EVENT_REASON_CODES:
            raise ProcessingLeaseError(f"unsupported event reason: {reason_code}")
        now = float(now if now is not None else datetime.now(timezone.utc).timestamp())
        created_at = timestamp_from_unix(now)
        sequence = int(row["state_version"]) + 1
        assignments = [
            "status = ?",
            "lifecycle_state = ?",
            "state_version = ?",
            "reason_code = ?",
            "updated_unix = ?",
            "error = ?",
        ]
        values = [
            compatibility_status,
            to_state,
            sequence,
            reason_code,
            now,
            _safe_error(error),
        ]
        for key, value in (column_updates or {}).items():
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
            values.extend([created_at, reason_code])
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
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["event_id"],
                sequence,
                row["lifecycle_state"],
                to_state,
                reason_code,
                actor_type,
                actor_id,
                _json_text(detail, "transition detail"),
                created_at,
            ),
        )
        return sequence

    @staticmethod
    def _release_policy_for_event_tx(connection, event_id, *, uncertain=False):
        state = "uncertain" if uncertain else "none"
        expires = 0.0
        connection.execute(
            """
            UPDATE attendance_policy_state
            SET reservation_state = ?, reservation_expires_unix = ?,
                reservation_event_id = CASE WHEN ? THEN reservation_event_id ELSE '' END,
                reservation_decision_id = CASE WHEN ? THEN reservation_decision_id ELSE '' END,
                reservation_effective_at = CASE WHEN ? THEN reservation_effective_at ELSE '' END,
                reservation_effective_unix = CASE WHEN ? THEN reservation_effective_unix ELSE 0 END,
                updated_at = ?
            WHERE reservation_event_id = ? AND reservation_state IN ('pending', 'uncertain')
            """,
            (
                state,
                expires,
                int(uncertain),
                int(uncertain),
                int(uncertain),
                int(uncertain),
                utc_now(),
                event_id,
            ),
        )

    def acquire_event_lease(
        self,
        event_id,
        *,
        owner,
        lease_seconds,
        now=None,
    ):
        event_id = _identifier(event_id, "event_id")
        owner = _text(owner, "lease owner", required=True, max_chars=256)
        lease_seconds = _strict_int(
            lease_seconds,
            "lease_seconds",
            minimum=30,
            maximum=3600,
        )
        now = _finite(
            now if now is not None else datetime.now(timezone.utc).timestamp(),
            "now",
            minimum=0,
        )
        expires = now + lease_seconds
        stamp = timestamp_from_unix(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM camera_events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                return ProcessingLeaseClaim(False, event_id, reason="missing")
            if row["lifecycle_state"] in TERMINAL_EVENT_STATES:
                connection.rollback()
                return ProcessingLeaseClaim(
                    False,
                    event_id,
                    attempt=int(row["processing_attempt"]),
                    reason="terminal",
                    existing_status=row["status"],
                )

            active = bool(row["lease_owner"]) and float(
                row["lease_expires_unix"] or 0
            ) > now
            if row["processing_phase"] == "delivery_in_progress":
                if active and row["lease_owner"] == owner:
                    connection.execute(
                        """
                        UPDATE camera_events
                        SET lease_heartbeat_at = ?, lease_expires_unix = ?
                        WHERE event_id = ?
                        """,
                        (stamp, expires, event_id),
                    )
                    connection.commit()
                    return ProcessingLeaseClaim(
                        True,
                        event_id,
                        attempt=int(row["processing_attempt"]),
                        reason="already_owned",
                        existing_status=row["status"],
                        lease_owner=owner,
                        lease_expires_unix=expires,
                    )
                if active:
                    connection.rollback()
                    return ProcessingLeaseClaim(
                        False,
                        event_id,
                        attempt=int(row["processing_attempt"]),
                        reason="active_lease",
                        existing_status=row["status"],
                        lease_owner=row["lease_owner"],
                        lease_expires_unix=float(row["lease_expires_unix"]),
                    )
                self._transition_tx(
                    connection,
                    row,
                    to_state="uncertain",
                    reason_code="generic_failed",
                    detail={
                        "kind": "delivery_ambiguous_after_lease_expiry",
                        "delivery_decision_id": row["delivery_decision_id"],
                    },
                    compatibility_status="uncertain",
                    error="delivery outcome is ambiguous after an expired processing lease",
                    terminal=True,
                    now=now,
                )
                if row["delivery_decision_id"]:
                    self._mark_delivery_job_uncertain_tx(
                        connection,
                        decision_id=row["delivery_decision_id"],
                        error_class="delivery_lease_expired",
                        error=(
                            "delivery outcome is ambiguous after an expired "
                            "processing lease"
                        ),
                        now=now,
                        missing_ok=True,
                    )
                self._release_policy_for_event_tx(
                    connection, event_id, uncertain=True
                )
                connection.commit()
                return ProcessingLeaseClaim(
                    False,
                    event_id,
                    attempt=int(row["processing_attempt"]),
                    reason="delivery_ambiguous",
                    existing_status="uncertain",
                )

            if active:
                if row["lease_owner"] != owner:
                    connection.rollback()
                    return ProcessingLeaseClaim(
                        False,
                        event_id,
                        attempt=int(row["processing_attempt"]),
                        reason="active_lease",
                        existing_status=row["status"],
                        lease_owner=row["lease_owner"],
                        lease_expires_unix=float(row["lease_expires_unix"]),
                    )
                connection.execute(
                    """
                    UPDATE camera_events
                    SET lease_heartbeat_at = ?, lease_expires_unix = ?
                    WHERE event_id = ?
                    """,
                    (stamp, expires, event_id),
                )
                connection.commit()
                return ProcessingLeaseClaim(
                    True,
                    event_id,
                    attempt=int(row["processing_attempt"]),
                    reason="already_owned",
                    existing_status=row["status"],
                    lease_owner=owner,
                    lease_expires_unix=expires,
                )

            previous_attempt = int(row["processing_attempt"])
            recovered = bool(
                previous_attempt
                or row["lease_owner"]
                or row["lifecycle_state"] != "received"
            )
            recovering_expired_lease = bool(
                row["lease_owner"]
                or float(row["lease_expires_unix"] or 0)
                or row["processing_phase"] != "idle"
            )
            if recovered:
                self._release_policy_for_event_tx(
                    connection, event_id, uncertain=False
                )
            attempt = previous_attempt + 1
            recovery_count = int(row["recovery_count"]) + int(
                recovering_expired_lease
            )
            self._transition_tx(
                connection,
                row,
                to_state="processing",
                reason_code="processing_started",
                detail={
                    "kind": "lease_recovered" if recovered else "lease_acquired",
                    "attempt": attempt,
                    "owner": owner,
                    "lease_seconds": lease_seconds,
                },
                compatibility_status="processing",
                column_updates={
                    "processing_attempt": attempt,
                    "lease_owner": owner,
                    "lease_acquired_at": stamp,
                    "lease_heartbeat_at": stamp,
                    "lease_expires_unix": expires,
                    "recovery_count": recovery_count,
                    "processing_phase": "pre_delivery",
                    "delivery_started_at": "",
                    "delivery_decision_id": "",
                },
                now=now,
            )
            connection.commit()
            return ProcessingLeaseClaim(
                True,
                event_id,
                attempt=attempt,
                reason="recovered" if recovered else "acquired",
                existing_status="processing",
                lease_owner=owner,
                lease_expires_unix=expires,
                recovered=recovered,
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def event_lease_is_current(self, event_id, *, owner, now=None):
        event_id = _identifier(event_id, "event_id")
        owner = _text(owner, "lease owner", required=True, max_chars=256)
        now = _finite(
            now if now is not None else datetime.now(timezone.utc).timestamp(),
            "now",
            minimum=0,
        )
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT lease_owner, lease_expires_unix, lifecycle_state
                FROM camera_events WHERE event_id = ?
                """,
                (event_id,),
            ).fetchone()
            return bool(
                row
                and row["lifecycle_state"] not in TERMINAL_EVENT_STATES
                and row["lease_owner"] == owner
                and float(row["lease_expires_unix"] or 0) > now
            )
        finally:
            connection.close()

    def finalize_event_with_lease(
        self,
        event_id,
        *,
        owner,
        to_state,
        reason_code,
        detail=None,
        event_updates=None,
        compatibility_status,
        error="",
        now=None,
    ):
        event_id = _identifier(event_id, "event_id")
        owner = _text(owner, "lease owner", required=True, max_chars=256)
        if to_state not in TERMINAL_EVENT_STATES:
            raise ProcessingLeaseError(
                f"lease finalization requires a terminal state, received: {to_state}"
            )
        if reason_code not in EVENT_REASON_CODES:
            raise ProcessingLeaseError(f"unsupported event reason: {reason_code}")
        now = _finite(
            now if now is not None else datetime.now(timezone.utc).timestamp(),
            "now",
            minimum=0,
        )
        updates = self._event_update_values(event_updates or {})
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM camera_events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if row is None:
                raise ProcessingLeaseError(f"event does not exist: {event_id}")
            if row["lifecycle_state"] in TERMINAL_EVENT_STATES:
                raise ProcessingLeaseError(
                    f"event is already terminal: {row['lifecycle_state']}"
                )
            if row["lease_owner"] != owner or float(
                row["lease_expires_unix"] or 0
            ) <= now:
                raise ProcessingLeaseError(
                    "cannot finalize without the current unexpired processing lease"
                )
            self._transition_tx(
                connection,
                row,
                to_state=to_state,
                reason_code=reason_code,
                detail=detail or {},
                compatibility_status=compatibility_status,
                actor_type="watcher",
                error=error,
                column_updates=updates,
                terminal=True,
                now=now,
            )
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def renew_event_lease(self, event_id, *, owner, lease_seconds, now=None):
        event_id = _identifier(event_id, "event_id")
        owner = _text(owner, "lease owner", required=True, max_chars=256)
        lease_seconds = _strict_int(
            lease_seconds, "lease_seconds", minimum=30, maximum=3600
        )
        now = _finite(
            now if now is not None else datetime.now(timezone.utc).timestamp(),
            "now",
            minimum=0,
        )
        stamp = timestamp_from_unix(now)
        connection = self._connect()
        try:
            cursor = connection.execute(
                """
                UPDATE camera_events
                SET lease_heartbeat_at = ?, lease_expires_unix = ?
                WHERE event_id = ? AND lease_owner = ?
                  AND lease_expires_unix >= ?
                  AND lifecycle_state NOT IN ('processed', 'checkin_created', 'rejected', 'failed', 'uncertain', 'dismissed')
                """,
                (stamp, now + lease_seconds, event_id, owner, now),
            )
            connection.commit()
            if cursor.rowcount != 1:
                raise ProcessingLeaseError(
                    f"processing lease is missing, expired, or owned by another worker: {event_id}"
                )
            return now + lease_seconds
        finally:
            connection.close()

    def begin_delivery_attempt(
        self,
        event_id,
        *,
        owner,
        decision_id,
        lease_seconds,
        transport="compatibility",
        now=None,
    ):
        event_id = _identifier(event_id, "event_id")
        decision_id = _identifier(decision_id, "decision_id")
        owner = _text(owner, "lease owner", required=True, max_chars=256)
        lease_seconds = _strict_int(
            lease_seconds, "lease_seconds", minimum=30, maximum=3600
        )
        now = _finite(
            now if now is not None else datetime.now(timezone.utc).timestamp(),
            "now",
            minimum=0,
        )
        stamp = timestamp_from_unix(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM camera_events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if row is None:
                raise ProcessingLeaseError(f"event does not exist: {event_id}")
            if row["lease_owner"] != owner or float(
                row["lease_expires_unix"] or 0
            ) < now:
                raise ProcessingLeaseError(
                    "cannot begin delivery without the active processing lease"
                )
            decision = connection.execute(
                """
                SELECT event_id, accepted, delivery_id
                FROM recognition_decisions
                WHERE decision_id = ?
                """,
                (decision_id,),
            ).fetchone()
            if (
                decision is None
                or decision["event_id"] != event_id
                or int(decision["accepted"] or 0) != 1
                or not str(decision["delivery_id"] or "")
            ):
                raise ProcessingLeaseError(
                    "delivery requires a persisted accepted recognition decision "
                    "with a stable delivery_id"
                )
            delivery_id = str(decision["delivery_id"])
            self._lease_delivery_job_tx(
                connection,
                decision_id=decision_id,
                owner=owner,
                lease_seconds=lease_seconds,
                transport=transport,
                now=now,
            )
            delivery_cursor = connection.execute(
                """
                UPDATE delivery_jobs
                SET submission_started_at = ?, lease_heartbeat_at = ?,
                    updated_at = ?
                WHERE decision_id = ? AND state = 'leased'
                  AND lease_owner = ?
                """,
                (stamp, stamp, stamp, decision_id, owner),
            )
            if delivery_cursor.rowcount != 1:
                raise ProcessingLeaseError(
                    "delivery job submission boundary could not be recorded"
                )
            self._transition_tx(
                connection,
                row,
                to_state="processing",
                reason_code="accepted_candidate",
                detail={
                    "kind": "delivery_started",
                    "decision_id": decision_id,
                    "delivery_id": delivery_id,
                    "attempt": int(row["processing_attempt"]),
                },
                compatibility_status="processing",
                column_updates={
                    "processing_phase": "delivery_in_progress",
                    "delivery_started_at": stamp,
                    "delivery_decision_id": decision_id,
                    "lease_heartbeat_at": stamp,
                    "lease_expires_unix": now + lease_seconds,
                },
                now=now,
            )
            connection.commit()
            return stamp
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def release_event_lease(self, event_id, *, owner, terminal=False):
        event_id = _identifier(event_id, "event_id")
        owner = _text(owner, "lease owner", required=True, max_chars=256)
        connection = self._connect()
        try:
            cursor = connection.execute(
                """
                UPDATE camera_events
                SET lease_owner = '', lease_acquired_at = '',
                    lease_heartbeat_at = '', lease_expires_unix = 0,
                    processing_phase = CASE WHEN ? THEN 'terminal' ELSE 'idle' END
                WHERE event_id = ? AND lease_owner = ?
                """,
                (int(bool(terminal)), event_id, owner),
            )
            connection.commit()
            return cursor.rowcount == 1
        finally:
            connection.close()

    def recover_expired_event_leases(self, *, now=None):
        now = _finite(
            now if now is not None else datetime.now(timezone.utc).timestamp(),
            "now",
            minimum=0,
        )
        outcomes = []
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT * FROM camera_events
                WHERE lifecycle_state NOT IN ('processed', 'checkin_created', 'rejected', 'failed', 'uncertain', 'dismissed')
                  AND (lease_owner = '' OR lease_expires_unix <= ?)
                ORDER BY received_unix, event_id
                """,
                (now,),
            ).fetchall()
            for row in rows:
                if (
                    row["processing_phase"] == "delivery_in_progress"
                    or row["delivery_started_at"]
                ):
                    self._transition_tx(
                        connection,
                        row,
                        to_state="uncertain",
                        reason_code="generic_failed",
                        detail={
                            "kind": "delivery_ambiguous_after_restart",
                            "delivery_decision_id": row["delivery_decision_id"],
                        },
                        compatibility_status="uncertain",
                        error="watcher restarted after delivery submission began",
                        terminal=True,
                        now=now,
                    )
                    if row["delivery_decision_id"]:
                        self._mark_delivery_job_uncertain_tx(
                            connection,
                            decision_id=row["delivery_decision_id"],
                            error_class="delivery_lease_expired",
                            error=(
                                "watcher restarted after delivery submission "
                                "began"
                            ),
                            now=now,
                            missing_ok=True,
                        )
                    self._release_policy_for_event_tx(
                        connection, row["event_id"], uncertain=True
                    )
                    outcome = "uncertain"
                else:
                    self._release_policy_for_event_tx(
                        connection, row["event_id"], uncertain=False
                    )
                    self._transition_tx(
                        connection,
                        row,
                        to_state="processing",
                        reason_code="processing_started",
                        detail={
                            "kind": "startup_recovery_ready",
                            "previous_phase": row["processing_phase"],
                            "previous_owner": row["lease_owner"],
                            "attempt": int(row["processing_attempt"]),
                        },
                        compatibility_status="processing",
                        column_updates={
                            "lease_owner": "",
                            "lease_acquired_at": "",
                            "lease_heartbeat_at": "",
                            "lease_expires_unix": 0.0,
                            "processing_phase": "idle",
                            "recovery_count": int(row["recovery_count"]) + 1,
                        },
                        now=now,
                    )
                    outcome = "retry"
                outcomes.append(
                    RecoveryOutcome(
                        row["event_id"],
                        outcome,
                        row["camera_id"],
                        row["source_name"],
                        row["lifecycle_state"],
                        int(row["processing_attempt"]),
                        row["delivery_decision_id"],
                        row["source_path"] if "source_path" in row.keys() else "",
                        row["retention_path"] if "retention_path" in row.keys() else "",
                    )
                )
            connection.commit()
            return outcomes
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def mark_recovery_source_missing(self, event_id, *, now=None):
        event_id = _identifier(event_id, "event_id")
        now = _finite(
            now if now is not None else datetime.now(timezone.utc).timestamp(),
            "now",
            minimum=0,
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM camera_events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if row is None or row["lifecycle_state"] in TERMINAL_EVENT_STATES:
                connection.rollback()
                return False
            self._release_policy_for_event_tx(connection, event_id, uncertain=False)
            self._transition_tx(
                connection,
                row,
                to_state="failed",
                reason_code="generic_failed",
                detail={"kind": "recovery_source_missing"},
                compatibility_status="failed",
                error="source upload is unavailable for safe pre-delivery recovery",
                terminal=True,
                now=now,
            )
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def reserve_attendance_policy(
        self,
        *,
        employee,
        direction,
        branch,
        policy_version,
        event_id,
        decision_id,
        effective_at,
        cooldown_seconds,
        reservation_seconds,
        now=None,
    ):
        employee = _text(employee, "employee", required=True, max_chars=180)
        direction = _text(direction, "direction", required=True, max_chars=16)
        if direction not in {"IN", "OUT"}:
            raise AttendancePolicyError("direction must be IN or OUT")
        branch = _text(branch, "branch", required=True, max_chars=128)
        policy_version = _text(
            policy_version, "policy_version", required=True, max_chars=128
        )
        event_id = _identifier(event_id, "event_id")
        decision_id = _identifier(decision_id, "decision_id")
        effective_at, effective_unix = _timestamp_unix(
            effective_at, "effective_at"
        )
        cooldown_seconds = _strict_int(
            cooldown_seconds,
            "cooldown_seconds",
            minimum=0,
            maximum=86400,
        )
        reservation_seconds = _strict_int(
            reservation_seconds,
            "reservation_seconds",
            minimum=30,
            maximum=86400,
        )
        now = _finite(
            now if now is not None else datetime.now(timezone.utc).timestamp(),
            "now",
            minimum=0,
        )
        scope_key = attendance_policy_scope_key(
            employee, direction, branch, policy_version
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM attendance_policy_state WHERE scope_key = ?",
                (scope_key,),
            ).fetchone()
            if row is not None:
                if row["reservation_state"] == "uncertain":
                    connection.rollback()
                    return PolicyReservation(
                        False,
                        scope_key,
                        "uncertain_reservation",
                        existing_event_id=row["reservation_event_id"],
                        existing_decision_id=row["reservation_decision_id"],
                    )
                if row["reservation_state"] == "pending":
                    if (
                        row["reservation_event_id"] == event_id
                        and row["reservation_decision_id"] == decision_id
                    ):
                        connection.rollback()
                        return PolicyReservation(
                            True,
                            scope_key,
                            "already_reserved",
                            existing_event_id=event_id,
                            existing_decision_id=decision_id,
                        )
                    if float(row["reservation_expires_unix"] or 0) > now:
                        remaining = max(
                            1,
                            int(float(row["reservation_expires_unix"]) - now),
                        )
                        connection.rollback()
                        return PolicyReservation(
                            False,
                            scope_key,
                            "reservation_active",
                            remaining_seconds=remaining,
                            existing_event_id=row["reservation_event_id"],
                            existing_decision_id=row["reservation_decision_id"],
                        )
                    reserved_event = connection.execute(
                        """
                        SELECT lifecycle_state, processing_phase,
                               delivery_started_at, lease_expires_unix
                        FROM camera_events WHERE event_id = ?
                        """,
                        (row["reservation_event_id"],),
                    ).fetchone()
                    if (
                        reserved_event is not None
                        and reserved_event["lifecycle_state"]
                        not in TERMINAL_EVENT_STATES
                        and (
                            reserved_event["processing_phase"]
                            == "delivery_in_progress"
                            or bool(reserved_event["delivery_started_at"])
                        )
                    ):
                        connection.execute(
                            """
                            UPDATE attendance_policy_state
                            SET reservation_state = 'uncertain',
                                reservation_expires_unix = 0,
                                updated_at = ?
                            WHERE scope_key = ?
                              AND reservation_state = 'pending'
                            """,
                            (utc_now(), scope_key),
                        )
                        connection.commit()
                        return PolicyReservation(
                            False,
                            scope_key,
                            "uncertain_reservation",
                            existing_event_id=row["reservation_event_id"],
                            existing_decision_id=row["reservation_decision_id"],
                        )
                    reserved_job = connection.execute(
                        """
                        SELECT state, next_attempt_unix, lease_expires_unix
                        FROM delivery_jobs WHERE decision_id = ?
                        """,
                        (row["reservation_decision_id"],),
                    ).fetchone()
                    if reserved_job is not None:
                        job_state = reserved_job["state"]
                        if job_state in {"pending", "retry_wait", "leased"}:
                            active_until = max(
                                float(reserved_job["next_attempt_unix"] or 0),
                                float(reserved_job["lease_expires_unix"] or 0),
                                now + 1,
                            )
                            connection.rollback()
                            return PolicyReservation(
                                False,
                                scope_key,
                                "reservation_active",
                                remaining_seconds=max(1, int(active_until - now)),
                                existing_event_id=row["reservation_event_id"],
                                existing_decision_id=row["reservation_decision_id"],
                            )
                        if job_state in {"uncertain", "delivered"}:
                            connection.execute(
                                """
                                UPDATE attendance_policy_state
                                SET reservation_state = 'uncertain',
                                    reservation_expires_unix = 0,
                                    updated_at = ?
                                WHERE scope_key = ?
                                  AND reservation_state = 'pending'
                                """,
                                (utc_now(), scope_key),
                            )
                            connection.commit()
                            return PolicyReservation(
                                False,
                                scope_key,
                                "uncertain_reservation",
                                existing_event_id=row["reservation_event_id"],
                                existing_decision_id=row["reservation_decision_id"],
                            )
                        if job_state in {"permanent_failure", "cancelled"}:
                            connection.execute(
                                """
                                UPDATE attendance_policy_state
                                SET reservation_event_id = '',
                                    reservation_decision_id = '',
                                    reservation_effective_at = '',
                                    reservation_effective_unix = 0,
                                    reservation_state = 'none',
                                    reservation_expires_unix = 0,
                                    updated_at = ?
                                WHERE scope_key = ?
                                  AND reservation_state = 'pending'
                                """,
                                (utc_now(), scope_key),
                            )
                            row = connection.execute(
                                "SELECT * FROM attendance_policy_state "
                                "WHERE scope_key = ?",
                                (scope_key,),
                            ).fetchone()
                    reserved_event = connection.execute(
                        """
                        SELECT lifecycle_state, processing_phase,
                               delivery_started_at, lease_expires_unix
                        FROM camera_events WHERE event_id = ?
                        """,
                        (row["reservation_event_id"],),
                    ).fetchone()
                    if (
                        reserved_event is not None
                        and reserved_event["lifecycle_state"]
                        not in TERMINAL_EVENT_STATES
                        and (
                            reserved_event["processing_phase"]
                            == "delivery_in_progress"
                            or bool(reserved_event["delivery_started_at"])
                        )
                    ):
                        connection.execute(
                            """
                            UPDATE attendance_policy_state
                            SET reservation_state = 'uncertain',
                                reservation_expires_unix = 0,
                                updated_at = ?
                            WHERE scope_key = ? AND reservation_state = 'pending'
                            """,
                            (utc_now(), scope_key),
                        )
                        connection.commit()
                        return PolicyReservation(
                            False,
                            scope_key,
                            "uncertain_reservation",
                            existing_event_id=row["reservation_event_id"],
                            existing_decision_id=row["reservation_decision_id"],
                        )
                committed_unix = float(row["committed_effective_unix"] or 0)
                if committed_unix:
                    delta = effective_unix - committed_unix
                    if delta <= 0 or delta < cooldown_seconds:
                        remaining = max(1, int(cooldown_seconds - delta))
                        connection.rollback()
                        return PolicyReservation(
                            False,
                            scope_key,
                            "cooldown",
                            remaining_seconds=remaining,
                            existing_event_id=row["committed_event_id"],
                            existing_decision_id=row["committed_decision_id"],
                        )

            updated_at = utc_now()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO attendance_policy_state (
                        scope_key, employee, branch, direction, policy_version,
                        reservation_event_id, reservation_decision_id,
                        reservation_effective_at, reservation_effective_unix,
                        reservation_state, reservation_expires_unix, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        scope_key,
                        employee,
                        branch,
                        direction,
                        policy_version,
                        event_id,
                        decision_id,
                        effective_at,
                        effective_unix,
                        now + reservation_seconds,
                        updated_at,
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE attendance_policy_state
                    SET reservation_event_id = ?, reservation_decision_id = ?,
                        reservation_effective_at = ?, reservation_effective_unix = ?,
                        reservation_state = 'pending', reservation_expires_unix = ?,
                        updated_at = ?
                    WHERE scope_key = ?
                    """,
                    (
                        event_id,
                        decision_id,
                        effective_at,
                        effective_unix,
                        now + reservation_seconds,
                        updated_at,
                        scope_key,
                    ),
                )
            connection.commit()
            return PolicyReservation(True, scope_key, "reserved")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def commit_attendance_policy_reservation(
        self, *, scope_key, event_id, decision_id
    ):
        scope_key = _identifier(scope_key, "scope_key")
        event_id = _identifier(event_id, "event_id")
        decision_id = _identifier(decision_id, "decision_id")
        connection = self._connect()
        try:
            cursor = connection.execute(
                """
                UPDATE attendance_policy_state
                SET committed_event_id = reservation_event_id,
                    committed_decision_id = reservation_decision_id,
                    committed_effective_at = reservation_effective_at,
                    committed_effective_unix = reservation_effective_unix,
                    reservation_event_id = '', reservation_decision_id = '',
                    reservation_effective_at = '', reservation_effective_unix = 0,
                    reservation_state = 'none', reservation_expires_unix = 0,
                    updated_at = ?
                WHERE scope_key = ? AND reservation_state = 'pending'
                  AND reservation_event_id = ? AND reservation_decision_id = ?
                """,
                (utc_now(), scope_key, event_id, decision_id),
            )
            connection.commit()
            if cursor.rowcount != 1:
                raise AttendancePolicyError(
                    "attendance policy reservation is missing or no longer pending"
                )
            return True
        finally:
            connection.close()

    def release_attendance_policy_reservation(
        self, *, scope_key, event_id, decision_id
    ):
        scope_key = _identifier(scope_key, "scope_key")
        event_id = _identifier(event_id, "event_id")
        decision_id = _identifier(decision_id, "decision_id")
        connection = self._connect()
        try:
            cursor = connection.execute(
                """
                UPDATE attendance_policy_state
                SET reservation_event_id = '', reservation_decision_id = '',
                    reservation_effective_at = '', reservation_effective_unix = 0,
                    reservation_state = 'none', reservation_expires_unix = 0,
                    updated_at = ?
                WHERE scope_key = ? AND reservation_state = 'pending'
                  AND reservation_event_id = ? AND reservation_decision_id = ?
                """,
                (utc_now(), scope_key, event_id, decision_id),
            )
            connection.commit()
            return cursor.rowcount == 1
        finally:
            connection.close()

    def mark_attendance_policy_uncertain(
        self, *, scope_key, event_id, decision_id
    ):
        scope_key = _identifier(scope_key, "scope_key")
        event_id = _identifier(event_id, "event_id")
        decision_id = _identifier(decision_id, "decision_id")
        connection = self._connect()
        try:
            cursor = connection.execute(
                """
                UPDATE attendance_policy_state
                SET reservation_state = 'uncertain',
                    reservation_expires_unix = 0,
                    updated_at = ?
                WHERE scope_key = ?
                  AND reservation_event_id = ? AND reservation_decision_id = ?
                  AND reservation_state IN ('pending', 'uncertain')
                """,
                (utc_now(), scope_key, event_id, decision_id),
            )
            connection.commit()
            if cursor.rowcount != 1:
                raise AttendancePolicyError(
                    "attendance policy reservation cannot be marked uncertain"
                )
            return True
        finally:
            connection.close()

    def release_event_policy_reservations(self, event_id):
        event_id = _identifier(event_id, "event_id")
        connection = self._connect()
        try:
            cursor = connection.execute(
                """
                UPDATE attendance_policy_state
                SET reservation_event_id = '', reservation_decision_id = '',
                    reservation_effective_at = '', reservation_effective_unix = 0,
                    reservation_state = 'none', reservation_expires_unix = 0,
                    updated_at = ?
                WHERE reservation_event_id = ? AND reservation_state = 'pending'
                """,
                (utc_now(), event_id),
            )
            connection.commit()
            return cursor.rowcount
        finally:
            connection.close()

    def attendance_policy_state(self, scope_key):
        scope_key = _identifier(scope_key, "scope_key")
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM attendance_policy_state WHERE scope_key = ?",
                (scope_key,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            connection.close()
