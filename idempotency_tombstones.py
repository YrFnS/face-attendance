"""Long-lived replay tombstones for pruned camera-event details."""

import math
from datetime import datetime, timezone

from event_identity import IDENTITY_CONTRACT_VERSION


PRUNABLE_EVENT_STATES = (
    "processed",
    "checkin_created",
    "rejected",
    "failed",
    "dismissed",
)


TOMBSTONE_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE event_idempotency_tombstones (
        event_id TEXT PRIMARY KEY,
        capture_id TEXT NOT NULL,
        camera_id TEXT NOT NULL,
        log_type TEXT NOT NULL CHECK(log_type IN ('IN', 'OUT')),
        source_sha256 TEXT NOT NULL,
        identity_contract_version TEXT NOT NULL,
        received_at TEXT NOT NULL,
        received_unix REAL NOT NULL,
        pruned_at TEXT NOT NULL,
        pruned_unix REAL NOT NULL,
        final_state TEXT NOT NULL,
        final_reason_code TEXT NOT NULL
    )
    """,
    """
    CREATE UNIQUE INDEX event_tombstones_camera_content
        ON event_idempotency_tombstones(camera_id, source_sha256)
    """,
    """
    CREATE UNIQUE INDEX event_tombstones_capture
        ON event_idempotency_tombstones(capture_id)
    """,
    """
    CREATE INDEX event_tombstones_pruned
        ON event_idempotency_tombstones(pruned_unix)
    """,
    """
    CREATE TRIGGER event_idempotency_tombstones_no_update
    BEFORE UPDATE ON event_idempotency_tombstones
    BEGIN
        SELECT RAISE(ABORT, 'event idempotency tombstones are immutable');
    END
    """,
    """
    CREATE TRIGGER camera_events_replay_tombstone_guard
    BEFORE INSERT ON camera_events
    WHEN EXISTS (
        SELECT 1
        FROM event_idempotency_tombstones tombstone
        WHERE tombstone.event_id = NEW.event_id
           OR tombstone.capture_id = NEW.capture_id
           OR (
               tombstone.camera_id = NEW.camera_id
               AND tombstone.source_sha256 = NEW.source_sha256
           )
    )
    BEGIN
        SELECT RAISE(ABORT, 'camera event replay is tombstoned');
    END
    """,
)


TOMBSTONE_REQUIRED_TABLE_COLUMNS = {
    "event_idempotency_tombstones": {
        "event_id": ("TEXT", False, 1),
        "capture_id": ("TEXT", True, 0),
        "camera_id": ("TEXT", True, 0),
        "log_type": ("TEXT", True, 0),
        "source_sha256": ("TEXT", True, 0),
        "identity_contract_version": ("TEXT", True, 0),
        "received_at": ("TEXT", True, 0),
        "received_unix": ("REAL", True, 0),
        "pruned_at": ("TEXT", True, 0),
        "pruned_unix": ("REAL", True, 0),
        "final_state": ("TEXT", True, 0),
        "final_reason_code": ("TEXT", True, 0),
    }
}


TOMBSTONE_REQUIRED_INDEXES = {
    "event_tombstones_camera_content": (
        True,
        ("camera_id", "source_sha256"),
    ),
    "event_tombstones_capture": (True, ("capture_id",)),
    "event_tombstones_pruned": (False, ("pruned_unix",)),
}


TOMBSTONE_REQUIRED_TRIGGERS = frozenset(
    {
        "event_idempotency_tombstones_no_update",
        "camera_events_replay_tombstone_guard",
    }
)


class IdempotencyTombstoneError(RuntimeError):
    pass


def _text(value, field, *, required=False, max_chars=4096):
    if value is None:
        text = ""
    elif not isinstance(value, str):
        raise IdempotencyTombstoneError(f"{field} must be a string")
    else:
        text = value
    if required and not text:
        raise IdempotencyTombstoneError(f"{field} is required")
    if len(text) > int(max_chars):
        raise IdempotencyTombstoneError(
            f"{field} exceeds {int(max_chars)} characters"
        )
    return text


def _now_unix(value=None):
    if value is None:
        result = datetime.now(timezone.utc).timestamp()
    elif isinstance(value, bool) or not isinstance(value, (int, float)):
        raise IdempotencyTombstoneError(
            "now must be a finite Unix timestamp"
        )
    else:
        result = float(value)
    if not math.isfinite(result) or result < 0:
        raise IdempotencyTombstoneError(
            "now must be a finite non-negative Unix timestamp"
        )
    return result


def _timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


class IdempotencyTombstoneMixin:
    def idempotency_tombstone(
        self,
        *,
        event_id="",
        capture_id="",
        camera_id="",
        source_sha256="",
    ):
        clauses = []
        values = []
        if event_id:
            clauses.append("event_id = ?")
            values.append(_text(event_id, "event_id", max_chars=64))
        if capture_id:
            clauses.append("capture_id = ?")
            values.append(_text(capture_id, "capture_id", max_chars=64))
        if camera_id or source_sha256:
            camera_id = _text(
                camera_id, "camera_id", required=True, max_chars=128
            )
            source_sha256 = _text(
                source_sha256,
                "source_sha256",
                required=True,
                max_chars=128,
            )
            clauses.append("(camera_id = ? AND source_sha256 = ?)")
            values.extend([camera_id, source_sha256])
        if not clauses:
            raise IdempotencyTombstoneError(
                "event_id, capture_id, or camera/content scope is required"
            )
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT *
                FROM event_idempotency_tombstones
                WHERE %s
                LIMIT 1
                """
                % " OR ".join(clauses),
                values,
            ).fetchone()
            return dict(row) if row else None
        finally:
            connection.close()

    def prune_events_with_tombstones(
        self,
        retention_days,
        *,
        now=None,
    ):
        if isinstance(retention_days, bool):
            raise IdempotencyTombstoneError(
                "retention_days must be an integer"
            )
        try:
            retention_days = int(retention_days or 0)
        except (TypeError, ValueError) as exc:
            raise IdempotencyTombstoneError(
                "retention_days must be an integer"
            ) from exc
        if retention_days <= 0:
            return 0
        now = _now_unix(now)
        cutoff = now - retention_days * 86400
        pruned_at = _timestamp(now)
        states = ", ".join("?" for _ in PRUNABLE_EVENT_STATES)
        eligibility = f"""
            received_unix < ?
            AND lifecycle_state IN ({states})
            AND processing_phase = 'terminal'
            AND lease_owner = ''
            AND retention_state != 'quarantined'
            AND NOT EXISTS (
                SELECT 1
                FROM attendance_policy_state policy_state
                WHERE policy_state.reservation_event_id = camera_events.event_id
                  AND policy_state.reservation_state IN ('pending', 'uncertain')
            )
        """
        parameters = [cutoff, *PRUNABLE_EVENT_STATES]
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                f"""
                INSERT INTO event_idempotency_tombstones (
                    event_id, capture_id, camera_id, log_type, source_sha256,
                    identity_contract_version, received_at, received_unix,
                    pruned_at, pruned_unix, final_state, final_reason_code
                )
                SELECT
                    event_id, capture_id, camera_id, log_type, source_sha256,
                    ?, received_at, received_unix, ?, ?,
                    lifecycle_state, reason_code
                FROM camera_events
                WHERE {eligibility}
                """,
                [
                    IDENTITY_CONTRACT_VERSION,
                    pruned_at,
                    now,
                    *parameters,
                ],
            )
            cursor = connection.execute(
                f"DELETE FROM camera_events WHERE {eligibility}",
                parameters,
            )
            connection.commit()
            return max(0, int(cursor.rowcount))
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
