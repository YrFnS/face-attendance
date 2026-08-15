"""Durable ERPNext delivery outbox state.

Schema version 6 creates one delivery job in the same SQLite transaction that
persists an accepted recognition decision. Schema version 7 adds the leased
single-node worker boundary, retry scheduling, and crash recovery.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone

from data_contract import validate_erp_docname
from event_identity import DELIVERY_ID_SCHEME
from erpnext_idempotency import job_row_has_verified_idempotency


HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
DELIVERY_JOB_STATES = frozenset(
    {
        "pending",
        "leased",
        "retry_wait",
        "delivered",
        "permanent_failure",
        "uncertain",
        "cancelled",
    }
)
TERMINAL_DELIVERY_JOB_STATES = frozenset(
    {"delivered", "permanent_failure", "cancelled"}
)


def _sql_values(values):
    return ", ".join(
        "'" + value.replace("'", "''") + "'" for value in sorted(values)
    )


_DELIVERY_STATE_SQL = _sql_values(DELIVERY_JOB_STATES)


DELIVERY_OUTBOX_SCHEMA_STATEMENTS = (
    f"""
    CREATE TABLE delivery_jobs (
        delivery_id TEXT PRIMARY KEY,
        decision_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        employee TEXT NOT NULL CHECK(employee <> ''),
        log_type TEXT NOT NULL CHECK(log_type IN ('IN', 'OUT')),
        effective_at TEXT NOT NULL,
        camera_id TEXT NOT NULL,
        branch TEXT NOT NULL DEFAULT '',
        delivery_contract_version TEXT NOT NULL
            CHECK(delivery_contract_version <> ''),
        delivery_id_scheme TEXT NOT NULL CHECK(delivery_id_scheme <> ''),
        state TEXT NOT NULL DEFAULT 'pending'
            CHECK(state IN ({_DELIVERY_STATE_SQL})),
        transport TEXT NOT NULL DEFAULT '',
        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
        next_attempt_unix REAL NOT NULL DEFAULT 0,
        lease_owner TEXT NOT NULL DEFAULT '',
        lease_acquired_at TEXT NOT NULL DEFAULT '',
        lease_heartbeat_at TEXT NOT NULL DEFAULT '',
        lease_expires_unix REAL NOT NULL DEFAULT 0,
        last_error_class TEXT NOT NULL DEFAULT '',
        last_error TEXT NOT NULL DEFAULT '',
        remote_docname TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        delivered_at TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE UNIQUE INDEX delivery_jobs_decision_id
    ON delivery_jobs(decision_id)
    """,
    """
    CREATE INDEX delivery_jobs_state_due
    ON delivery_jobs(state, next_attempt_unix, created_at)
    """,
    """
    CREATE INDEX delivery_jobs_lease
    ON delivery_jobs(state, lease_expires_unix)
    """,
    """
    CREATE INDEX delivery_jobs_event
    ON delivery_jobs(event_id, created_at)
    """,
    """
    CREATE INDEX delivery_jobs_remote_docname
    ON delivery_jobs(remote_docname)
    """,
    """
    INSERT INTO delivery_jobs (
        delivery_id, decision_id, event_id, employee, log_type,
        effective_at, camera_id, branch, delivery_contract_version,
        delivery_id_scheme, state, transport, attempt_count,
        next_attempt_unix, lease_owner, lease_acquired_at,
        lease_heartbeat_at, lease_expires_unix, last_error_class,
        last_error, remote_docname, created_at, updated_at, delivered_at
    )
    SELECT
        d.delivery_id,
        d.decision_id,
        d.event_id,
        d.best_employee,
        CASE
            WHEN d.candidate_log_type IN ('IN', 'OUT')
            THEN d.candidate_log_type
            ELSE e.log_type
        END,
        e.effective_at,
        e.camera_id,
        e.branch,
        d.delivery_contract_version,
        d.delivery_id_scheme,
        CASE
            WHEN e.lifecycle_state = 'checkin_created' THEN 'delivered'
            WHEN e.lifecycle_state = 'uncertain'
                 OR e.delivery_started_at <> '' THEN 'uncertain'
            ELSE 'pending'
        END,
        'legacy-synchronous',
        CASE
            WHEN e.lifecycle_state = 'checkin_created'
                 OR e.lifecycle_state = 'uncertain'
                 OR e.delivery_started_at <> ''
            THEN 1 ELSE 0
        END,
        0,
        '', '', '', 0,
        CASE
            WHEN e.lifecycle_state = 'uncertain'
                 OR e.delivery_started_at <> ''
            THEN 'legacy_delivery_ambiguous'
            ELSE ''
        END,
        CASE
            WHEN e.lifecycle_state = 'uncertain'
                 OR e.delivery_started_at <> ''
            THEN 'migrated from the synchronous delivery boundary'
            ELSE ''
        END,
        '',
        d.created_at,
        CASE
            WHEN e.completed_at IS NOT NULL AND e.completed_at <> ''
            THEN e.completed_at
            ELSE d.created_at
        END,
        CASE
            WHEN e.lifecycle_state = 'checkin_created'
            THEN COALESCE(e.completed_at, '')
            ELSE ''
        END
    FROM recognition_decisions d
    JOIN camera_events e ON e.event_id = d.event_id
    WHERE d.accepted = 1
      AND d.delivery_id <> ''
      AND d.delivery_id_scheme <> ''
      AND d.delivery_contract_version <> ''
      AND d.best_employee <> ''
    """,
    f"""
    CREATE TRIGGER recognition_decisions_delivery_identity_required
    BEFORE INSERT ON recognition_decisions
    WHEN NEW.accepted = 1 AND (
        NEW.delivery_id = ''
        OR NEW.delivery_id_scheme <> '{DELIVERY_ID_SCHEME}'
        OR NEW.delivery_contract_version = ''
        OR NEW.best_employee = ''
    )
    BEGIN
        SELECT RAISE(
            ABORT,
            'accepted recognition decisions require complete delivery identity'
        );
    END
    """,
    """
    CREATE TRIGGER recognition_decisions_rejected_delivery_empty
    BEFORE INSERT ON recognition_decisions
    WHEN NEW.accepted = 0 AND (
        NEW.delivery_id <> ''
        OR NEW.delivery_id_scheme <> ''
        OR NEW.delivery_contract_version <> ''
    )
    BEGIN
        SELECT RAISE(
            ABORT,
            'rejected recognition decisions must not have delivery identity'
        );
    END
    """,
    """
    CREATE TRIGGER recognition_decisions_enqueue_delivery_job
    AFTER INSERT ON recognition_decisions
    WHEN NEW.accepted = 1
    BEGIN
        INSERT INTO delivery_jobs (
            delivery_id, decision_id, event_id, employee, log_type,
            effective_at, camera_id, branch, delivery_contract_version,
            delivery_id_scheme, state, transport, attempt_count,
            next_attempt_unix, lease_owner, lease_acquired_at,
            lease_heartbeat_at, lease_expires_unix, last_error_class,
            last_error, remote_docname, created_at, updated_at, delivered_at
        )
        SELECT
            NEW.delivery_id,
            NEW.decision_id,
            NEW.event_id,
            NEW.best_employee,
            CASE
                WHEN NEW.candidate_log_type IN ('IN', 'OUT')
                THEN NEW.candidate_log_type
                ELSE e.log_type
            END,
            e.effective_at,
            e.camera_id,
            e.branch,
            NEW.delivery_contract_version,
            NEW.delivery_id_scheme,
            'pending',
            '',
            0,
            0,
            '', '', '', 0,
            '', '', '',
            NEW.created_at,
            NEW.created_at,
            ''
        FROM camera_events e
        WHERE e.event_id = NEW.event_id;

        SELECT CASE
            WHEN changes() <> 1 THEN RAISE(
                ABORT,
                'accepted decision could not create exactly one delivery job'
            )
        END;
    END
    """,
    """
    CREATE TRIGGER delivery_jobs_identity_immutable
    BEFORE UPDATE OF
        delivery_id, decision_id, event_id, employee, log_type,
        effective_at, camera_id, branch, delivery_contract_version,
        delivery_id_scheme, created_at
    ON delivery_jobs
    WHEN
        NEW.delivery_id <> OLD.delivery_id
        OR NEW.decision_id <> OLD.decision_id
        OR NEW.event_id <> OLD.event_id
        OR NEW.employee <> OLD.employee
        OR NEW.log_type <> OLD.log_type
        OR NEW.effective_at <> OLD.effective_at
        OR NEW.camera_id <> OLD.camera_id
        OR NEW.branch <> OLD.branch
        OR NEW.delivery_contract_version <> OLD.delivery_contract_version
        OR NEW.delivery_id_scheme <> OLD.delivery_id_scheme
        OR NEW.created_at <> OLD.created_at
    BEGIN
        SELECT RAISE(ABORT, 'delivery job identity is immutable');
    END
    """,
    """
    CREATE TRIGGER delivery_jobs_terminal_state_immutable
    BEFORE UPDATE OF state ON delivery_jobs
    WHEN OLD.state IN ('delivered', 'permanent_failure', 'cancelled')
         AND NEW.state <> OLD.state
    BEGIN
        SELECT RAISE(ABORT, 'terminal delivery job state is immutable');
    END
    """,
    """
    CREATE TRIGGER delivery_jobs_no_delete
    BEFORE DELETE ON delivery_jobs
    BEGIN
        SELECT RAISE(ABORT, 'delivery jobs are durable audit records');
    END
    """,
)


DELIVERY_OUTBOX_REQUIRED_TABLE_COLUMNS = {
    "delivery_jobs": {
        "delivery_id": ("TEXT", False, 1),
        "decision_id": ("TEXT", True, 0),
        "event_id": ("TEXT", True, 0),
        "employee": ("TEXT", True, 0),
        "log_type": ("TEXT", True, 0),
        "effective_at": ("TEXT", True, 0),
        "camera_id": ("TEXT", True, 0),
        "branch": ("TEXT", True, 0),
        "delivery_contract_version": ("TEXT", True, 0),
        "delivery_id_scheme": ("TEXT", True, 0),
        "state": ("TEXT", True, 0),
        "transport": ("TEXT", True, 0),
        "attempt_count": ("INTEGER", True, 0),
        "next_attempt_unix": ("REAL", True, 0),
        "lease_owner": ("TEXT", True, 0),
        "lease_acquired_at": ("TEXT", True, 0),
        "lease_heartbeat_at": ("TEXT", True, 0),
        "lease_expires_unix": ("REAL", True, 0),
        "last_error_class": ("TEXT", True, 0),
        "last_error": ("TEXT", True, 0),
        "remote_docname": ("TEXT", True, 0),
        "created_at": ("TEXT", True, 0),
        "updated_at": ("TEXT", True, 0),
        "delivered_at": ("TEXT", True, 0),
    }
}


DELIVERY_OUTBOX_REQUIRED_INDEXES = {
    "delivery_jobs_decision_id": (True, ("decision_id",)),
    "delivery_jobs_state_due": (
        False,
        ("state", "next_attempt_unix", "created_at"),
    ),
    "delivery_jobs_lease": (False, ("state", "lease_expires_unix")),
    "delivery_jobs_event": (False, ("event_id", "created_at")),
    "delivery_jobs_remote_docname": (False, ("remote_docname",)),
}


DELIVERY_OUTBOX_REQUIRED_TRIGGERS = frozenset(
    {
        "recognition_decisions_delivery_identity_required",
        "recognition_decisions_rejected_delivery_empty",
        "recognition_decisions_enqueue_delivery_job",
        "delivery_jobs_identity_immutable",
        "delivery_jobs_terminal_state_immutable",
        "delivery_jobs_no_delete",
    }
)


DELIVERY_WORKER_SCHEMA_STATEMENTS = (
    "ALTER TABLE delivery_jobs ADD COLUMN submission_started_at TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE delivery_jobs ADD COLUMN retry_delay_seconds REAL NOT NULL DEFAULT 0 CHECK(retry_delay_seconds >= 0)",
    """
    CREATE INDEX delivery_jobs_submission_lease
    ON delivery_jobs(state, submission_started_at, lease_expires_unix)
    """,
    """
    CREATE TRIGGER delivery_jobs_submission_requires_active_lease
    BEFORE UPDATE OF submission_started_at ON delivery_jobs
    WHEN NEW.submission_started_at <> ''
         AND (OLD.state <> 'leased' OR OLD.lease_owner = '')
    BEGIN
        SELECT RAISE(
            ABORT,
            'delivery submission requires an active delivery lease'
        );
    END
    """,
)

DELIVERY_WORKER_REQUIRED_TABLE_COLUMNS = {
    "delivery_jobs": {
        "submission_started_at": ("TEXT", True, 0),
        "retry_delay_seconds": ("REAL", True, 0),
    }
}

DELIVERY_WORKER_REQUIRED_INDEXES = {
    "delivery_jobs_submission_lease": (
        False,
        ("state", "submission_started_at", "lease_expires_unix"),
    )
}

DELIVERY_WORKER_REQUIRED_TRIGGERS = frozenset(
    {"delivery_jobs_submission_requires_active_lease"}
)


class DeliveryOutboxError(RuntimeError):
    pass


class DeliveryOutboxValidationError(DeliveryOutboxError, ValueError):
    pass


class DeliveryJobStateError(DeliveryOutboxError):
    pass


@dataclass(frozen=True)
class DeliveryJobLease:
    delivery_id: str
    decision_id: str
    event_id: str
    state: str
    attempt_count: int
    lease_owner: str
    lease_expires_unix: float
    transport: str


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
        raise DeliveryOutboxValidationError(f"{field} must be a string")
    else:
        text = unicodedata.normalize("NFC", value)
    if text != text.strip():
        raise DeliveryOutboxValidationError(
            f"{field} must not contain surrounding whitespace"
        )
    if required and not text:
        raise DeliveryOutboxValidationError(f"{field} is required")
    if len(text) > int(max_chars):
        raise DeliveryOutboxValidationError(
            f"{field} exceeds {int(max_chars)} characters"
        )
    for character in text:
        if unicodedata.category(character) in {"Cc", "Cf", "Cs"}:
            raise DeliveryOutboxValidationError(
                f"{field} contains a control or formatting character"
            )
    return text


def _identifier(value, field):
    text = _text(value, field, required=True, max_chars=64).lower()
    if not HEX64_RE.fullmatch(text):
        raise DeliveryOutboxValidationError(
            f"{field} must be a lowercase 64-character SHA-256 identifier"
        )
    return text


def _strict_int(value, field, *, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int):
        raise DeliveryOutboxValidationError(f"{field} must be an integer")
    if value < int(minimum) or value > int(maximum):
        raise DeliveryOutboxValidationError(
            f"{field} must be between {int(minimum)} and {int(maximum)}"
        )
    return value


def _finite(value, field, *, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DeliveryOutboxValidationError(f"{field} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < float(minimum):
        raise DeliveryOutboxValidationError(
            f"{field} must be finite and at least {minimum}"
        )
    return result


def _safe_error(value, max_chars=2000):
    raw = str(value or "")[: int(max_chars)]
    return "".join(
        character
        if unicodedata.category(character) not in {"Cc", "Cf", "Cs"}
        else f"\\u{ord(character):04x}"
        for character in raw
    )


def _job_lease(row):
    return DeliveryJobLease(
        row["delivery_id"],
        row["decision_id"],
        row["event_id"],
        row["state"],
        int(row["attempt_count"]),
        row["lease_owner"],
        float(row["lease_expires_unix"]),
        row["transport"],
    )


class DeliveryOutboxMixin:
    def get_delivery_job(self, delivery_id):
        delivery_id = _identifier(delivery_id, "delivery_id")
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM delivery_jobs WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            connection.close()

    def delivery_job_for_decision(self, decision_id):
        decision_id = _identifier(decision_id, "decision_id")
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM delivery_jobs WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            connection.close()

    def list_delivery_jobs(self, *, state="", limit=50, offset=0):
        limit = _strict_int(limit, "limit", minimum=1, maximum=500)
        offset = _strict_int(
            offset,
            "offset",
            minimum=0,
            maximum=10_000_000,
        )
        values = []
        where = ""
        if state:
            state = _text(state, "state", required=True, max_chars=64)
            if state not in DELIVERY_JOB_STATES:
                raise DeliveryOutboxValidationError(
                    "state must be one of: "
                    + ", ".join(sorted(DELIVERY_JOB_STATES))
                )
            where = "WHERE state = ?"
            values.append(state)
        values.extend([limit, offset])
        connection = self._connect()
        try:
            return [
                dict(row)
                for row in connection.execute(
                    f"""
                    SELECT *
                    FROM delivery_jobs
                    {where}
                    ORDER BY created_at DESC, delivery_id DESC
                    LIMIT ? OFFSET ?
                    """,
                    values,
                ).fetchall()
            ]
        finally:
            connection.close()

    def delivery_queue_summary(self, *, now=None):
        now = _finite(
            now if now is not None else datetime.now(timezone.utc).timestamp(),
            "now",
        )
        connection = self._connect()
        try:
            counts = {
                row["state"]: int(row["count"])
                for row in connection.execute(
                    """
                    SELECT state, COUNT(*) AS count
                    FROM delivery_jobs
                    GROUP BY state
                    """
                ).fetchall()
            }
            due = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM delivery_jobs
                WHERE state IN ('pending', 'retry_wait')
                  AND next_attempt_unix <= ?
                """,
                (now,),
            ).fetchone()
            return {
                "counts": {
                    state: counts.get(state, 0)
                    for state in sorted(DELIVERY_JOB_STATES)
                },
                "due": int(due["count"]),
                "total": sum(counts.values()),
            }
        finally:
            connection.close()

    def _lease_delivery_job_tx(
        self,
        connection,
        *,
        decision_id,
        owner,
        lease_seconds,
        transport,
        now,
    ):
        decision_id = _identifier(decision_id, "decision_id")
        owner = _text(owner, "delivery lease owner", required=True, max_chars=256)
        transport = _text(
            transport,
            "delivery transport",
            required=True,
            max_chars=64,
        )
        lease_seconds = _strict_int(
            lease_seconds,
            "delivery lease seconds",
            minimum=30,
            maximum=3600,
        )
        now = _finite(now, "now")
        row = connection.execute(
            "SELECT * FROM delivery_jobs WHERE decision_id = ?",
            (decision_id,),
        ).fetchone()
        if row is None:
            raise DeliveryJobStateError(
                f"delivery job does not exist for decision: {decision_id}"
            )
        if row["state"] in TERMINAL_DELIVERY_JOB_STATES:
            raise DeliveryJobStateError(
                f"delivery job is already terminal: {row['state']}"
            )
        if row["state"] == "uncertain":
            raise DeliveryJobStateError(
                "uncertain delivery job requires reconciliation before retry"
            )

        active = (
            row["state"] == "leased"
            and bool(row["lease_owner"])
            and float(row["lease_expires_unix"] or 0) > now
        )
        stamp = timestamp_from_unix(now)
        expires = now + lease_seconds
        if active:
            if row["lease_owner"] != owner:
                raise DeliveryJobStateError(
                    "delivery job has an active lease owned by another worker"
                )
            connection.execute(
                """
                UPDATE delivery_jobs
                SET lease_heartbeat_at = ?, lease_expires_unix = ?,
                    transport = ?, updated_at = ?
                WHERE delivery_id = ?
                """,
                (stamp, expires, transport, stamp, row["delivery_id"]),
            )
        else:
            if row["state"] == "leased":
                raise DeliveryJobStateError(
                    "expired delivery lease requires reconciliation"
                )
            if (
                row["state"] == "retry_wait"
                and float(row["next_attempt_unix"] or 0) > now
            ):
                raise DeliveryJobStateError(
                    "delivery job retry time has not arrived"
                )
            connection.execute(
                """
                UPDATE delivery_jobs
                SET state = 'leased',
                    transport = ?,
                    attempt_count = attempt_count + 1,
                    next_attempt_unix = 0,
                    lease_owner = ?,
                    lease_acquired_at = ?,
                    lease_heartbeat_at = ?,
                    lease_expires_unix = ?,
                    submission_started_at = '',
                    retry_delay_seconds = 0,
                    last_error_class = '',
                    last_error = '',
                    updated_at = ?
                WHERE delivery_id = ?
                """,
                (
                    transport,
                    owner,
                    stamp,
                    stamp,
                    expires,
                    stamp,
                    row["delivery_id"],
                ),
            )
        current = connection.execute(
            "SELECT * FROM delivery_jobs WHERE delivery_id = ?",
            (row["delivery_id"],),
        ).fetchone()
        return _job_lease(current)

    def _mark_delivery_job_uncertain_tx(
        self,
        connection,
        *,
        decision_id,
        error_class,
        error,
        now,
        missing_ok=False,
    ):
        decision_id = _identifier(decision_id, "decision_id")
        error_class = _text(
            error_class,
            "delivery error class",
            required=True,
            max_chars=128,
        )
        now = _finite(now, "now")
        row = connection.execute(
            "SELECT * FROM delivery_jobs WHERE decision_id = ?",
            (decision_id,),
        ).fetchone()
        if row is None:
            if missing_ok:
                return None
            raise DeliveryJobStateError(
                f"delivery job does not exist for decision: {decision_id}"
            )
        if row["state"] == "delivered":
            return dict(row)
        if row["state"] in {"permanent_failure", "cancelled"}:
            raise DeliveryJobStateError(
                f"delivery job is terminal: {row['state']}"
            )
        stamp = timestamp_from_unix(now)
        connection.execute(
            """
            UPDATE delivery_jobs
            SET state = 'uncertain',
                next_attempt_unix = 0,
                lease_owner = '',
                lease_acquired_at = '',
                lease_heartbeat_at = '',
                lease_expires_unix = 0,
                last_error_class = ?,
                last_error = ?,
                updated_at = ?
            WHERE delivery_id = ?
            """,
            (
                error_class,
                _safe_error(error),
                stamp,
                row["delivery_id"],
            ),
        )
        current = connection.execute(
            "SELECT * FROM delivery_jobs WHERE delivery_id = ?",
            (row["delivery_id"],),
        ).fetchone()
        return dict(current)

    def mark_delivery_job_uncertain(
        self,
        *,
        decision_id,
        error_class,
        error="",
        now=None,
    ):
        now = _finite(
            now if now is not None else datetime.now(timezone.utc).timestamp(),
            "now",
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            result = self._mark_delivery_job_uncertain_tx(
                connection,
                decision_id=decision_id,
                error_class=error_class,
                error=error,
                now=now,
            )
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def mark_delivery_job_delivered(
        self,
        *,
        decision_id,
        remote_docname,
        transport,
        now=None,
    ):
        decision_id = _identifier(decision_id, "decision_id")
        remote_docname = validate_erp_docname(remote_docname)
        transport = _text(
            transport,
            "delivery transport",
            required=True,
            max_chars=64,
        )
        now = _finite(
            now if now is not None else datetime.now(timezone.utc).timestamp(),
            "now",
        )
        stamp = timestamp_from_unix(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM delivery_jobs WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
            if row is None:
                raise DeliveryJobStateError(
                    f"delivery job does not exist for decision: {decision_id}"
                )
            if row["state"] == "delivered":
                existing = str(row["remote_docname"] or "")
                if existing and existing != remote_docname:
                    raise DeliveryJobStateError(
                        "delivered job is bound to a different ERPNext document"
                    )
                if (
                    not existing
                    or row["transport"] != transport
                    or not row["delivered_at"]
                ):
                    connection.execute(
                        """
                        UPDATE delivery_jobs
                        SET remote_docname = ?, transport = ?,
                            delivered_at = CASE
                                WHEN delivered_at = '' THEN ? ELSE delivered_at
                            END,
                            updated_at = ?
                        WHERE delivery_id = ?
                        """,
                        (
                            remote_docname,
                            transport,
                            stamp,
                            stamp,
                            row["delivery_id"],
                        ),
                    )
                connection.commit()
                return self.delivery_job_for_decision(decision_id)
            if row["state"] in {"permanent_failure", "cancelled"}:
                raise DeliveryJobStateError(
                    f"delivery job is terminal: {row['state']}"
                )
            if row["state"] not in {"leased", "uncertain"}:
                raise DeliveryJobStateError(
                    "delivery job must be leased or uncertain before it can "
                    "be marked delivered"
                )
            connection.execute(
                """
                UPDATE delivery_jobs
                SET state = 'delivered',
                    transport = ?,
                    next_attempt_unix = 0,
                    lease_owner = '',
                    lease_acquired_at = '',
                    lease_heartbeat_at = '',
                    lease_expires_unix = 0,
                    last_error_class = '',
                    last_error = '',
                    remote_docname = ?,
                    updated_at = ?,
                    delivered_at = ?
                WHERE delivery_id = ?
                """,
                (
                    transport,
                    remote_docname,
                    stamp,
                    stamp,
                    row["delivery_id"],
                ),
            )
            current = connection.execute(
                "SELECT * FROM delivery_jobs WHERE delivery_id = ?",
                (row["delivery_id"],),
            ).fetchone()
            connection.commit()
            return dict(current)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _policy_release_for_decision_tx(connection, decision_id):
        connection.execute(
            """
            UPDATE attendance_policy_state
            SET reservation_event_id = '', reservation_decision_id = '',
                reservation_effective_at = '', reservation_effective_unix = 0,
                reservation_state = 'none', reservation_expires_unix = 0,
                updated_at = ?
            WHERE reservation_decision_id = ? AND reservation_state = 'pending'
            """,
            (utc_now(), decision_id),
        )

    @staticmethod
    def _policy_uncertain_for_decision_tx(connection, decision_id):
        connection.execute(
            """
            UPDATE attendance_policy_state
            SET reservation_state = 'uncertain',
                reservation_expires_unix = 0,
                updated_at = ?
            WHERE reservation_decision_id = ?
              AND reservation_state IN ('pending', 'uncertain')
            """,
            (utc_now(), decision_id),
        )

    @staticmethod
    def _policy_commit_for_decision_tx(connection, decision_id):
        connection.execute(
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
            WHERE reservation_decision_id = ? AND reservation_state = 'pending'
            """,
            (utc_now(), decision_id),
        )

    @staticmethod
    def _policy_extend_for_decision_tx(connection, decision_id, expires_unix):
        connection.execute(
            """
            UPDATE attendance_policy_state
            SET reservation_expires_unix = CASE
                    WHEN reservation_expires_unix < ? THEN ?
                    ELSE reservation_expires_unix
                END,
                updated_at = ?
            WHERE reservation_decision_id = ? AND reservation_state = 'pending'
            """,
            (expires_unix, expires_unix, utc_now(), decision_id),
        )

    @staticmethod
    def _clear_delivery_lease_values():
        return {
            "lease_owner": "",
            "lease_acquired_at": "",
            "lease_heartbeat_at": "",
            "lease_expires_unix": 0.0,
        }

    @staticmethod
    def _delivery_job_row_tx(connection, delivery_id):
        return connection.execute(
            "SELECT * FROM delivery_jobs WHERE delivery_id = ?",
            (delivery_id,),
        ).fetchone()

    def _current_delivery_lease_tx(
        self,
        connection,
        *,
        delivery_id,
        owner,
        now,
        require_submission=None,
    ):
        delivery_id = _identifier(delivery_id, "delivery_id")
        owner = _text(owner, "delivery lease owner", required=True, max_chars=256)
        now = _finite(now, "now")
        row = self._delivery_job_row_tx(connection, delivery_id)
        if row is None:
            raise DeliveryJobStateError(f"delivery job does not exist: {delivery_id}")
        if (
            row["state"] != "leased"
            or row["lease_owner"] != owner
            or float(row["lease_expires_unix"] or 0) <= now
        ):
            raise DeliveryJobStateError(
                "operation requires the current unexpired delivery lease"
            )
        started = bool(row["submission_started_at"])
        if require_submission is True and not started:
            raise DeliveryJobStateError(
                "delivery submission has not started for the current lease"
            )
        if require_submission is False and started:
            raise DeliveryJobStateError(
                "delivery submission already started for the current lease"
            )
        return row

    def _recover_expired_delivery_job_leases_tx(
        self,
        connection,
        *,
        max_attempts,
        now,
    ):
        max_attempts = _strict_int(
            max_attempts,
            "delivery max attempts",
            minimum=1,
            maximum=100,
        )
        now = _finite(now, "now")
        stamp = timestamp_from_unix(now)
        rows = connection.execute(
            """
            SELECT * FROM delivery_jobs
            WHERE state = 'leased' AND lease_expires_unix <= ?
            ORDER BY lease_expires_unix, created_at, delivery_id
            """,
            (now,),
        ).fetchall()
        results = []
        for row in rows:
            if int(row["attempt_count"]) >= max_attempts:
                state = "permanent_failure"
                error_class = "retry_budget_exhausted"
                error = "delivery retry budget exhausted after lease expiry"
                next_attempt = 0.0
                self._policy_release_for_decision_tx(
                    connection, row["decision_id"]
                )
            elif row["submission_started_at"] and job_row_has_verified_idempotency(row):
                state = "retry_wait"
                error_class = "delivery_lease_expired_after_idempotent_submission"
                error = (
                    "delivery worker lease expired after submission; the verified "
                    "ERPNext delivery-ID contract permits a safe replay"
                )
                next_attempt = now
                self._policy_extend_for_decision_tx(
                    connection, row["decision_id"], now + 300.0
                )
            elif row["submission_started_at"]:
                state = "uncertain"
                error_class = "delivery_lease_expired_after_submission"
                error = (
                    "delivery worker lease expired after ERPNext submission began"
                )
                next_attempt = 0.0
                self._policy_uncertain_for_decision_tx(
                    connection, row["decision_id"]
                )
            else:
                state = "retry_wait"
                error_class = "delivery_lease_expired_before_submission"
                error = (
                    "delivery worker lease expired before ERPNext submission"
                )
                next_attempt = now
            connection.execute(
                """
                UPDATE delivery_jobs
                SET state = ?, next_attempt_unix = ?,
                    lease_owner = '', lease_acquired_at = '',
                    lease_heartbeat_at = '', lease_expires_unix = 0,
                    submission_started_at = '', retry_delay_seconds = 0,
                    last_error_class = ?, last_error = ?, updated_at = ?
                WHERE delivery_id = ?
                """,
                (
                    state,
                    next_attempt,
                    error_class,
                    error,
                    stamp,
                    row["delivery_id"],
                ),
            )
            current = self._delivery_job_row_tx(connection, row["delivery_id"])
            results.append(dict(current))
        return results

    def recover_expired_delivery_job_leases(self, *, max_attempts, now=None):
        now = _finite(
            now if now is not None else datetime.now(timezone.utc).timestamp(),
            "now",
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            results = self._recover_expired_delivery_job_leases_tx(
                connection,
                max_attempts=max_attempts,
                now=now,
            )
            connection.commit()
            return results
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def claim_next_delivery_job(
        self,
        *,
        owner,
        lease_seconds,
        transport,
        max_attempts,
        now=None,
    ):
        owner = _text(owner, "delivery lease owner", required=True, max_chars=256)
        transport = _text(
            transport,
            "delivery transport",
            required=True,
            max_chars=64,
        )
        lease_seconds = _strict_int(
            lease_seconds,
            "delivery lease seconds",
            minimum=30,
            maximum=3600,
        )
        max_attempts = _strict_int(
            max_attempts,
            "delivery max attempts",
            minimum=1,
            maximum=100,
        )
        now = _finite(
            now if now is not None else datetime.now(timezone.utc).timestamp(),
            "now",
        )
        stamp = timestamp_from_unix(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._recover_expired_delivery_job_leases_tx(
                connection,
                max_attempts=max_attempts,
                now=now,
            )
            exhausted = connection.execute(
                """
                SELECT delivery_id, decision_id
                FROM delivery_jobs
                WHERE state IN ('pending', 'retry_wait')
                  AND next_attempt_unix <= ?
                  AND attempt_count >= ?
                """,
                (now, max_attempts),
            ).fetchall()
            for row in exhausted:
                connection.execute(
                    """
                    UPDATE delivery_jobs
                    SET state = 'permanent_failure', next_attempt_unix = 0,
                        retry_delay_seconds = 0,
                        last_error_class = 'retry_budget_exhausted',
                        last_error = 'delivery retry budget exhausted',
                        updated_at = ?
                    WHERE delivery_id = ?
                    """,
                    (stamp, row["delivery_id"]),
                )
                self._policy_release_for_decision_tx(
                    connection, row["decision_id"]
                )
            row = connection.execute(
                """
                SELECT * FROM delivery_jobs
                WHERE state IN ('pending', 'retry_wait')
                  AND next_attempt_unix <= ?
                  AND attempt_count < ?
                ORDER BY next_attempt_unix, created_at, delivery_id
                LIMIT 1
                """,
                (now, max_attempts),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            expires = now + lease_seconds
            connection.execute(
                """
                UPDATE delivery_jobs
                SET state = 'leased', transport = ?,
                    attempt_count = attempt_count + 1,
                    next_attempt_unix = 0,
                    lease_owner = ?, lease_acquired_at = ?,
                    lease_heartbeat_at = ?, lease_expires_unix = ?,
                    submission_started_at = '', retry_delay_seconds = 0,
                    last_error_class = '', last_error = '', updated_at = ?
                WHERE delivery_id = ?
                """,
                (
                    transport,
                    owner,
                    stamp,
                    stamp,
                    expires,
                    stamp,
                    row["delivery_id"],
                ),
            )
            current = self._delivery_job_row_tx(connection, row["delivery_id"])
            connection.commit()
            return dict(current)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def renew_delivery_job_lease(
        self,
        delivery_id,
        *,
        owner,
        lease_seconds,
        now=None,
    ):
        delivery_id = _identifier(delivery_id, "delivery_id")
        owner = _text(owner, "delivery lease owner", required=True, max_chars=256)
        lease_seconds = _strict_int(
            lease_seconds,
            "delivery lease seconds",
            minimum=30,
            maximum=3600,
        )
        now = _finite(
            now if now is not None else datetime.now(timezone.utc).timestamp(),
            "now",
        )
        stamp = timestamp_from_unix(now)
        connection = self._connect()
        try:
            cursor = connection.execute(
                """
                UPDATE delivery_jobs
                SET lease_heartbeat_at = ?, lease_expires_unix = ?,
                    updated_at = ?
                WHERE delivery_id = ? AND state = 'leased'
                  AND lease_owner = ? AND lease_expires_unix > ?
                """,
                (
                    stamp,
                    now + lease_seconds,
                    stamp,
                    delivery_id,
                    owner,
                    now,
                ),
            )
            connection.commit()
            if cursor.rowcount != 1:
                raise DeliveryJobStateError(
                    "renewal requires the current delivery lease"
                )
            return self.get_delivery_job(delivery_id)
        finally:
            connection.close()

    def mark_delivery_submission_started(
        self,
        delivery_id,
        *,
        owner,
        now=None,
    ):
        now = _finite(
            now if now is not None else datetime.now(timezone.utc).timestamp(),
            "now",
        )
        stamp = timestamp_from_unix(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = self._current_delivery_lease_tx(
                connection,
                delivery_id=delivery_id,
                owner=owner,
                now=now,
            )
            if not row["submission_started_at"]:
                connection.execute(
                    """
                    UPDATE delivery_jobs
                    SET submission_started_at = ?, lease_heartbeat_at = ?,
                        updated_at = ?
                    WHERE delivery_id = ?
                    """,
                    (stamp, stamp, stamp, row["delivery_id"]),
                )
            current = self._delivery_job_row_tx(connection, row["delivery_id"])
            connection.commit()
            return dict(current)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def mark_delivery_job_retry_by_lease(
        self,
        delivery_id,
        *,
        owner,
        error_class,
        error,
        delay_seconds,
        max_attempts,
        safe_after_submission=False,
        now=None,
    ):
        error_class = _text(
            error_class,
            "delivery error class",
            required=True,
            max_chars=128,
        )
        if not isinstance(safe_after_submission, bool):
            raise DeliveryOutboxValidationError(
                "safe_after_submission must be a boolean"
            )
        delay_seconds = _finite(delay_seconds, "delivery retry delay")
        if delay_seconds > 86400:
            raise DeliveryOutboxValidationError(
                "delivery retry delay must not exceed 86400 seconds"
            )
        max_attempts = _strict_int(
            max_attempts,
            "delivery max attempts",
            minimum=1,
            maximum=100,
        )
        now = _finite(
            now if now is not None else datetime.now(timezone.utc).timestamp(),
            "now",
        )
        stamp = timestamp_from_unix(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = self._current_delivery_lease_tx(
                connection,
                delivery_id=delivery_id,
                owner=owner,
                now=now,
            )
            if row["submission_started_at"] and not safe_after_submission:
                raise DeliveryJobStateError(
                    "ambiguous post-submission failures cannot be retried"
                )
            if int(row["attempt_count"]) >= max_attempts:
                state = "permanent_failure"
                error_class = "retry_budget_exhausted"
                next_attempt = 0.0
                delay_seconds = 0.0
                self._policy_release_for_decision_tx(
                    connection, row["decision_id"]
                )
            else:
                state = "retry_wait"
                next_attempt = now + delay_seconds
                self._policy_extend_for_decision_tx(
                    connection,
                    row["decision_id"],
                    next_attempt + 300.0,
                )
            connection.execute(
                """
                UPDATE delivery_jobs
                SET state = ?, next_attempt_unix = ?,
                    lease_owner = '', lease_acquired_at = '',
                    lease_heartbeat_at = '', lease_expires_unix = 0,
                    submission_started_at = '', retry_delay_seconds = ?,
                    last_error_class = ?, last_error = ?, updated_at = ?
                WHERE delivery_id = ?
                """,
                (
                    state,
                    next_attempt,
                    delay_seconds,
                    error_class,
                    _safe_error(error),
                    stamp,
                    row["delivery_id"],
                ),
            )
            current = self._delivery_job_row_tx(connection, row["delivery_id"])
            connection.commit()
            return dict(current)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def mark_delivery_job_permanent_failure_by_lease(
        self,
        delivery_id,
        *,
        owner,
        error_class,
        error="",
        now=None,
    ):
        error_class = _text(
            error_class,
            "delivery error class",
            required=True,
            max_chars=128,
        )
        now = _finite(
            now if now is not None else datetime.now(timezone.utc).timestamp(),
            "now",
        )
        stamp = timestamp_from_unix(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = self._current_delivery_lease_tx(
                connection,
                delivery_id=delivery_id,
                owner=owner,
                now=now,
            )
            connection.execute(
                """
                UPDATE delivery_jobs
                SET state = 'permanent_failure', next_attempt_unix = 0,
                    lease_owner = '', lease_acquired_at = '',
                    lease_heartbeat_at = '', lease_expires_unix = 0,
                    retry_delay_seconds = 0,
                    last_error_class = ?, last_error = ?, updated_at = ?
                WHERE delivery_id = ?
                """,
                (
                    error_class,
                    _safe_error(error),
                    stamp,
                    row["delivery_id"],
                ),
            )
            self._policy_release_for_decision_tx(connection, row["decision_id"])
            current = self._delivery_job_row_tx(connection, row["delivery_id"])
            connection.commit()
            return dict(current)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def mark_delivery_job_uncertain_by_lease(
        self,
        delivery_id,
        *,
        owner,
        error_class,
        error="",
        now=None,
    ):
        error_class = _text(
            error_class,
            "delivery error class",
            required=True,
            max_chars=128,
        )
        now = _finite(
            now if now is not None else datetime.now(timezone.utc).timestamp(),
            "now",
        )
        stamp = timestamp_from_unix(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = self._current_delivery_lease_tx(
                connection,
                delivery_id=delivery_id,
                owner=owner,
                now=now,
            )
            connection.execute(
                """
                UPDATE delivery_jobs
                SET state = 'uncertain', next_attempt_unix = 0,
                    lease_owner = '', lease_acquired_at = '',
                    lease_heartbeat_at = '', lease_expires_unix = 0,
                    retry_delay_seconds = 0,
                    last_error_class = ?, last_error = ?, updated_at = ?
                WHERE delivery_id = ?
                """,
                (
                    error_class,
                    _safe_error(error),
                    stamp,
                    row["delivery_id"],
                ),
            )
            self._policy_uncertain_for_decision_tx(
                connection, row["decision_id"]
            )
            current = self._delivery_job_row_tx(connection, row["delivery_id"])
            connection.commit()
            return dict(current)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def mark_delivery_job_delivered_by_lease(
        self,
        delivery_id,
        *,
        owner,
        remote_docname,
        transport,
        now=None,
    ):
        remote_docname = validate_erp_docname(remote_docname)
        transport = _text(
            transport,
            "delivery transport",
            required=True,
            max_chars=64,
        )
        now = _finite(
            now if now is not None else datetime.now(timezone.utc).timestamp(),
            "now",
        )
        stamp = timestamp_from_unix(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = self._current_delivery_lease_tx(
                connection,
                delivery_id=delivery_id,
                owner=owner,
                now=now,
                require_submission=True,
            )
            connection.execute(
                """
                UPDATE delivery_jobs
                SET state = 'delivered', transport = ?,
                    next_attempt_unix = 0,
                    lease_owner = '', lease_acquired_at = '',
                    lease_heartbeat_at = '', lease_expires_unix = 0,
                    retry_delay_seconds = 0,
                    last_error_class = '', last_error = '',
                    remote_docname = ?, updated_at = ?, delivered_at = ?
                WHERE delivery_id = ?
                """,
                (
                    transport,
                    remote_docname,
                    stamp,
                    stamp,
                    row["delivery_id"],
                ),
            )
            self._policy_commit_for_decision_tx(
                connection, row["decision_id"]
            )
            current = self._delivery_job_row_tx(connection, row["delivery_id"])
            connection.commit()
            return dict(current)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def active_delivery_job_count(self):
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM delivery_jobs
                WHERE state IN ('pending', 'retry_wait', 'leased', 'uncertain')
                """
            ).fetchone()
            return int(row["count"])
        finally:
            connection.close()
