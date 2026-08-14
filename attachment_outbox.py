"""Durable private-crop attachment jobs.

P2-05 keeps Employee Checkin creation and private crop upload as independent
operations.  An accepted recognition decision may create one attachment job in
the same SQLite transaction that creates its Employee Checkin delivery job.
The attachment is not claimable until that parent delivery is confirmed.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from data_contract import validate_erp_docname


HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
ATTACHMENT_ID_SCHEME = "face-attendance-private-crop-v1"
ATTACHMENT_JOB_STATES = frozenset(
    {
        "waiting_for_checkin",
        "pending",
        "leased",
        "retry_wait",
        "attached",
        "permanent_failure",
        "uncertain",
        "cancelled",
    }
)
TERMINAL_ATTACHMENT_JOB_STATES = frozenset(
    {"attached", "permanent_failure", "cancelled"}
)


def _sql_values(values):
    return ", ".join(
        "'" + value.replace("'", "''") + "'" for value in sorted(values)
    )


_ATTACHMENT_STATE_SQL = _sql_values(ATTACHMENT_JOB_STATES)


ATTACHMENT_OUTBOX_SCHEMA_STATEMENTS = (
    f"""
    CREATE TABLE attachment_jobs (
        attachment_id TEXT PRIMARY KEY,
        attachment_id_scheme TEXT NOT NULL
            CHECK(attachment_id_scheme <> ''),
        delivery_id TEXT NOT NULL UNIQUE,
        decision_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        source_path TEXT NOT NULL DEFAULT '',
        source_sha256 TEXT NOT NULL DEFAULT '',
        source_size INTEGER NOT NULL DEFAULT 0 CHECK(source_size >= 0),
        filename TEXT NOT NULL DEFAULT '',
        content_type TEXT NOT NULL DEFAULT 'image/jpeg',
        source_state TEXT NOT NULL DEFAULT 'available'
            CHECK(source_state IN ('available', 'deleted', 'missing')),
        source_deleted_at TEXT NOT NULL DEFAULT '',
        delete_after_success INTEGER NOT NULL DEFAULT 1
            CHECK(delete_after_success IN (0, 1)),
        state TEXT NOT NULL DEFAULT 'waiting_for_checkin'
            CHECK(state IN ({_ATTACHMENT_STATE_SQL})),
        parent_docname TEXT NOT NULL DEFAULT '',
        transport TEXT NOT NULL DEFAULT '',
        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
        next_attempt_unix REAL NOT NULL DEFAULT 0,
        lease_owner TEXT NOT NULL DEFAULT '',
        lease_acquired_at TEXT NOT NULL DEFAULT '',
        lease_heartbeat_at TEXT NOT NULL DEFAULT '',
        lease_expires_unix REAL NOT NULL DEFAULT 0,
        submission_started_at TEXT NOT NULL DEFAULT '',
        retry_delay_seconds REAL NOT NULL DEFAULT 0
            CHECK(retry_delay_seconds >= 0),
        last_error_class TEXT NOT NULL DEFAULT '',
        last_error TEXT NOT NULL DEFAULT '',
        remote_file_docname TEXT NOT NULL DEFAULT '',
        remote_file_url TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        attached_at TEXT NOT NULL DEFAULT '',
        FOREIGN KEY(delivery_id) REFERENCES delivery_jobs(delivery_id)
    )
    """,
    """
    CREATE UNIQUE INDEX attachment_jobs_delivery_id
    ON attachment_jobs(delivery_id)
    """,
    """
    CREATE UNIQUE INDEX attachment_jobs_source_path
    ON attachment_jobs(source_path)
    WHERE source_path <> ''
    """,
    """
    CREATE INDEX attachment_jobs_state_due
    ON attachment_jobs(state, next_attempt_unix, created_at)
    """,
    """
    CREATE INDEX attachment_jobs_lease
    ON attachment_jobs(state, lease_expires_unix)
    """,
    """
    CREATE INDEX attachment_jobs_event
    ON attachment_jobs(event_id, created_at)
    """,
    """
    CREATE INDEX attachment_jobs_parent_docname
    ON attachment_jobs(parent_docname)
    """,
    """
    CREATE TRIGGER attachment_jobs_identity_immutable
    BEFORE UPDATE OF
        attachment_id, attachment_id_scheme, delivery_id, decision_id,
        event_id, source_path, source_sha256, source_size, filename,
        content_type, delete_after_success, created_at
    ON attachment_jobs
    WHEN
        NEW.attachment_id <> OLD.attachment_id
        OR NEW.attachment_id_scheme <> OLD.attachment_id_scheme
        OR NEW.delivery_id <> OLD.delivery_id
        OR NEW.decision_id <> OLD.decision_id
        OR NEW.event_id <> OLD.event_id
        OR NEW.source_path <> OLD.source_path
        OR NEW.source_sha256 <> OLD.source_sha256
        OR NEW.source_size <> OLD.source_size
        OR NEW.filename <> OLD.filename
        OR NEW.content_type <> OLD.content_type
        OR NEW.delete_after_success <> OLD.delete_after_success
        OR NEW.created_at <> OLD.created_at
    BEGIN
        SELECT RAISE(ABORT, 'attachment job identity is immutable');
    END
    """,
    """
    CREATE TRIGGER attachment_jobs_terminal_state_immutable
    BEFORE UPDATE OF state ON attachment_jobs
    WHEN OLD.state IN ('attached', 'permanent_failure', 'cancelled')
         AND NEW.state <> OLD.state
    BEGIN
        SELECT RAISE(ABORT, 'terminal attachment job state is immutable');
    END
    """,
    """
    CREATE TRIGGER attachment_jobs_submission_requires_active_lease
    BEFORE UPDATE OF submission_started_at ON attachment_jobs
    WHEN NEW.submission_started_at <> ''
         AND (
             OLD.state <> 'leased'
             OR OLD.lease_owner = ''
             OR NEW.state <> 'leased'
             OR NEW.lease_owner <> OLD.lease_owner
         )
    BEGIN
        SELECT RAISE(
            ABORT,
            'attachment submission requires an active attachment lease'
        );
    END
    """,
    """
    CREATE TRIGGER attachment_jobs_source_required
    BEFORE INSERT ON attachment_jobs
    WHEN NEW.state NOT IN ('permanent_failure', 'cancelled') AND (
        NEW.source_path = ''
        OR NEW.source_sha256 = ''
        OR NEW.source_size <= 0
        OR NEW.filename = ''
    )
    BEGIN
        SELECT RAISE(
            ABORT,
            'active attachment job requires complete source evidence'
        );
    END
    """,
    """
    CREATE TRIGGER attachment_jobs_no_delete
    BEFORE DELETE ON attachment_jobs
    BEGIN
        SELECT RAISE(ABORT, 'attachment jobs are durable audit records');
    END
    """,
)


ATTACHMENT_OUTBOX_REQUIRED_TABLE_COLUMNS = {
    "attachment_jobs": {
        "attachment_id": ("TEXT", False, 1),
        "attachment_id_scheme": ("TEXT", True, 0),
        "delivery_id": ("TEXT", True, 0),
        "decision_id": ("TEXT", True, 0),
        "event_id": ("TEXT", True, 0),
        "source_path": ("TEXT", True, 0),
        "source_sha256": ("TEXT", True, 0),
        "source_size": ("INTEGER", True, 0),
        "filename": ("TEXT", True, 0),
        "content_type": ("TEXT", True, 0),
        "source_state": ("TEXT", True, 0),
        "source_deleted_at": ("TEXT", True, 0),
        "delete_after_success": ("INTEGER", True, 0),
        "state": ("TEXT", True, 0),
        "parent_docname": ("TEXT", True, 0),
        "transport": ("TEXT", True, 0),
        "attempt_count": ("INTEGER", True, 0),
        "next_attempt_unix": ("REAL", True, 0),
        "lease_owner": ("TEXT", True, 0),
        "lease_acquired_at": ("TEXT", True, 0),
        "lease_heartbeat_at": ("TEXT", True, 0),
        "lease_expires_unix": ("REAL", True, 0),
        "submission_started_at": ("TEXT", True, 0),
        "retry_delay_seconds": ("REAL", True, 0),
        "last_error_class": ("TEXT", True, 0),
        "last_error": ("TEXT", True, 0),
        "remote_file_docname": ("TEXT", True, 0),
        "remote_file_url": ("TEXT", True, 0),
        "created_at": ("TEXT", True, 0),
        "updated_at": ("TEXT", True, 0),
        "attached_at": ("TEXT", True, 0),
    }
}

ATTACHMENT_OUTBOX_REQUIRED_INDEXES = {
    "attachment_jobs_delivery_id": (True, ("delivery_id",)),
    "attachment_jobs_source_path": (True, ("source_path",)),
    "attachment_jobs_state_due": (
        False,
        ("state", "next_attempt_unix", "created_at"),
    ),
    "attachment_jobs_lease": (False, ("state", "lease_expires_unix")),
    "attachment_jobs_event": (False, ("event_id", "created_at")),
    "attachment_jobs_parent_docname": (False, ("parent_docname",)),
}

ATTACHMENT_OUTBOX_REQUIRED_TRIGGERS = frozenset(
    {
        "attachment_jobs_identity_immutable",
        "attachment_jobs_terminal_state_immutable",
        "attachment_jobs_submission_requires_active_lease",
        "attachment_jobs_source_required",
        "attachment_jobs_no_delete",
    }
)


class AttachmentOutboxError(RuntimeError):
    pass


class AttachmentOutboxValidationError(AttachmentOutboxError, ValueError):
    pass


class AttachmentJobStateError(AttachmentOutboxError):
    pass


@dataclass(frozen=True)
class AttachmentJobLease:
    attachment_id: str
    delivery_id: str
    decision_id: str
    event_id: str
    state: str
    attempt_count: int
    lease_owner: str
    lease_expires_unix: float
    transport: str
    parent_docname: str


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
        raise AttachmentOutboxValidationError(f"{field} must be a string")
    else:
        text = unicodedata.normalize("NFC", value)
    if text != text.strip():
        raise AttachmentOutboxValidationError(
            f"{field} must not contain surrounding whitespace"
        )
    if required and not text:
        raise AttachmentOutboxValidationError(f"{field} is required")
    if len(text) > int(max_chars):
        raise AttachmentOutboxValidationError(
            f"{field} exceeds {int(max_chars)} characters"
        )
    for character in text:
        if unicodedata.category(character) in {"Cc", "Cf", "Cs"}:
            raise AttachmentOutboxValidationError(
                f"{field} contains a control or formatting character"
            )
    return text


def _identifier(value, field):
    text = _text(value, field, required=True, max_chars=64).lower()
    if not HEX64_RE.fullmatch(text):
        raise AttachmentOutboxValidationError(
            f"{field} must be a lowercase 64-character SHA-256 identifier"
        )
    return text


def _strict_int(value, field, *, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int):
        raise AttachmentOutboxValidationError(f"{field} must be an integer")
    if value < int(minimum) or value > int(maximum):
        raise AttachmentOutboxValidationError(
            f"{field} must be between {int(minimum)} and {int(maximum)}"
        )
    return value


def _finite(value, field, *, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AttachmentOutboxValidationError(f"{field} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < float(minimum):
        raise AttachmentOutboxValidationError(
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


def make_attachment_id(decision_id):
    decision_id = _identifier(decision_id, "decision_id")
    payload = f"{ATTACHMENT_ID_SCHEME}\0{decision_id}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _normal_attachment_source(attachment, attachment_id):
    if not isinstance(attachment, dict):
        raise AttachmentOutboxValidationError(
            "attachment metadata must be a mapping"
        )
    source_path = _text(
        attachment.get("source_path"),
        "attachment source_path",
        required=True,
        max_chars=4096,
    )
    path = Path(source_path)
    if not path.is_absolute():
        raise AttachmentOutboxValidationError(
            "attachment source_path must be absolute"
        )
    if path.name != f"{attachment_id}.jpg":
        raise AttachmentOutboxValidationError(
            "attachment source_path basename must match attachment identity"
        )
    source_sha256 = _identifier(
        attachment.get("source_sha256"), "attachment source_sha256"
    )
    source_size = _strict_int(
        attachment.get("source_size"),
        "attachment source_size",
        minimum=1,
        maximum=100 * 1024 * 1024,
    )
    filename = _text(
        attachment.get("filename"),
        "attachment filename",
        required=True,
        max_chars=255,
    )
    if "/" in filename or "\\" in filename or filename in {".", ".."}:
        raise AttachmentOutboxValidationError(
            "attachment filename must be a basename"
        )
    content_type = _text(
        attachment.get("content_type") or "image/jpeg",
        "attachment content_type",
        required=True,
        max_chars=128,
    )
    if content_type != "image/jpeg":
        raise AttachmentOutboxValidationError(
            "private attendance crop content_type must be image/jpeg"
        )
    delete_after_success = attachment.get("delete_after_success", True)
    if not isinstance(delete_after_success, bool):
        raise AttachmentOutboxValidationError(
            "delete_after_success must be a boolean"
        )
    return {
        "source_path": source_path,
        "source_sha256": source_sha256,
        "source_size": source_size,
        "filename": filename,
        "content_type": content_type,
        "delete_after_success": int(delete_after_success),
    }


def insert_attachment_job_tx(connection, decision_id, attachment, created_at):
    """Insert one attachment job inside a decision/outbox transaction."""

    decision_id = _identifier(decision_id, "decision_id")
    if attachment is None:
        return None
    if not isinstance(attachment, dict):
        raise AttachmentOutboxValidationError(
            "attachment metadata must be a mapping"
        )
    parent = connection.execute(
        "SELECT * FROM delivery_jobs WHERE decision_id = ?",
        (decision_id,),
    ).fetchone()
    if parent is None:
        raise AttachmentOutboxValidationError(
            "attachment requires the transactional Employee Checkin delivery job"
        )
    attachment_id = make_attachment_id(decision_id)
    error_class = _text(
        attachment.get("error_class"),
        "attachment error_class",
        max_chars=128,
    )
    if error_class:
        source = {
            "source_path": "",
            "source_sha256": "",
            "source_size": 0,
            "filename": "",
            "content_type": "image/jpeg",
            "source_state": "missing",
            "delete_after_success": 1,
        }
        state = "permanent_failure"
        error = _safe_error(attachment.get("error"))
    else:
        source = _normal_attachment_source(attachment, attachment_id)
        source["source_state"] = "available"
        if parent["state"] == "delivered" and parent["remote_docname"]:
            state = "pending"
        elif parent["state"] in {"permanent_failure", "cancelled"}:
            state = "cancelled"
        else:
            state = "waiting_for_checkin"
        error = ""
    source_state = "missing" if error_class else "available"
    parent_docname = (
        validate_erp_docname(parent["remote_docname"])
        if parent["remote_docname"]
        else ""
    )
    connection.execute(
        """
        INSERT INTO attachment_jobs (
            attachment_id, attachment_id_scheme, delivery_id, decision_id,
            event_id, source_path, source_sha256, source_size, filename,
            content_type, source_state, source_deleted_at,
            delete_after_success, state, parent_docname,
            transport, attempt_count, next_attempt_unix, lease_owner,
            lease_acquired_at, lease_heartbeat_at, lease_expires_unix,
            submission_started_at, retry_delay_seconds, last_error_class,
            last_error, remote_file_docname, remote_file_url,
            created_at, updated_at, attached_at
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, '', 0, 0,
            '', '', '', 0, '', 0, ?, ?, '', '', ?, ?, ''
        )
        """,
        (
            attachment_id,
            ATTACHMENT_ID_SCHEME,
            parent["delivery_id"],
            decision_id,
            parent["event_id"],
            source["source_path"],
            source["source_sha256"],
            source["source_size"],
            source["filename"],
            source["content_type"],
            source["source_state"],
            source["delete_after_success"],
            state,
            parent_docname,
            error_class,
            error,
            created_at,
            created_at,
        ),
    )
    return attachment_id


class AttachmentOutboxMixin:
    def get_attachment_job(self, attachment_id):
        attachment_id = _identifier(attachment_id, "attachment_id")
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM attachment_jobs WHERE attachment_id = ?",
                (attachment_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            connection.close()

    def attachment_job_for_delivery(self, delivery_id):
        delivery_id = _identifier(delivery_id, "delivery_id")
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM attachment_jobs WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            connection.close()

    def attachment_job_for_decision(self, decision_id):
        decision_id = _identifier(decision_id, "decision_id")
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM attachment_jobs WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            connection.close()

    def list_attachment_jobs(self, *, state="", limit=50, offset=0):
        limit = _strict_int(limit, "limit", minimum=1, maximum=500)
        offset = _strict_int(offset, "offset", minimum=0, maximum=10_000_000)
        values = []
        where = ""
        if state:
            state = _text(state, "state", required=True, max_chars=64)
            if state not in ATTACHMENT_JOB_STATES:
                raise AttachmentOutboxValidationError(
                    "state must be one of: "
                    + ", ".join(sorted(ATTACHMENT_JOB_STATES))
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
                    SELECT * FROM attachment_jobs {where}
                    ORDER BY created_at DESC, attachment_id DESC
                    LIMIT ? OFFSET ?
                    """,
                    values,
                ).fetchall()
            ]
        finally:
            connection.close()

    def attachment_queue_summary(self, *, now=None):
        now = _finite(
            now if now is not None else datetime.now(timezone.utc).timestamp(),
            "now",
        )
        connection = self._connect()
        try:
            counts = {
                row["state"]: int(row["count"])
                for row in connection.execute(
                    "SELECT state, COUNT(*) AS count FROM attachment_jobs GROUP BY state"
                ).fetchall()
            }
            due = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM attachment_jobs a
                JOIN delivery_jobs d ON d.delivery_id = a.delivery_id
                WHERE a.state IN ('pending', 'retry_wait')
                  AND a.next_attempt_unix <= ?
                  AND d.state = 'delivered'
                  AND d.remote_docname <> ''
                """,
                (now,),
            ).fetchone()
            return {
                "counts": {
                    state: counts.get(state, 0)
                    for state in sorted(ATTACHMENT_JOB_STATES)
                },
                "due": int(due["count"]),
                "total": sum(counts.values()),
            }
        finally:
            connection.close()

    def active_attachment_job_count(self):
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count FROM attachment_jobs
                WHERE state IN (
                    'waiting_for_checkin', 'pending', 'leased',
                    'retry_wait', 'uncertain'
                )
                """
            ).fetchone()
            return int(row["count"])
        finally:
            connection.close()

    def attachment_source_paths(self):
        connection = self._connect()
        try:
            return {
                str(row["source_path"])
                for row in connection.execute(
                    """
                    SELECT source_path FROM attachment_jobs
                    WHERE source_path <> '' AND source_state = 'available'
                    """
                ).fetchall()
            }
        finally:
            connection.close()

    def attachment_jobs_for_source_cleanup(self, *, limit=500):
        limit = _strict_int(limit, "limit", minimum=1, maximum=5000)
        connection = self._connect()
        try:
            return [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM attachment_jobs
                    WHERE source_state = 'available'
                      AND source_path <> ''
                      AND (
                          state = 'cancelled'
                          OR (state = 'attached' AND delete_after_success = 1)
                      )
                    ORDER BY updated_at, attachment_id
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            ]
        finally:
            connection.close()

    def mark_attachment_source_state(
        self, attachment_id, *, source_state, now=None
    ):
        attachment_id = _identifier(attachment_id, "attachment_id")
        source_state = _text(
            source_state, "attachment source state", required=True, max_chars=32
        )
        if source_state not in {"available", "deleted", "missing"}:
            raise AttachmentOutboxValidationError(
                "attachment source state must be available, deleted, or missing"
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
                UPDATE attachment_jobs
                SET source_state = ?,
                    source_deleted_at = CASE
                        WHEN ? = 'available' THEN '' ELSE ?
                    END,
                    updated_at = ?
                WHERE attachment_id = ?
                """,
                (source_state, source_state, stamp, stamp, attachment_id),
            )
            connection.commit()
            if cursor.rowcount != 1:
                raise AttachmentJobStateError(
                    f"attachment job does not exist: {attachment_id}"
                )
            return self.get_attachment_job(attachment_id)
        finally:
            connection.close()

    @staticmethod
    def _attachment_job_row_tx(connection, attachment_id):
        return connection.execute(
            "SELECT * FROM attachment_jobs WHERE attachment_id = ?",
            (attachment_id,),
        ).fetchone()

    def _refresh_attachment_dependencies_tx(self, connection, *, now):
        stamp = timestamp_from_unix(now)
        connection.execute(
            """
            UPDATE attachment_jobs
            SET state = 'pending',
                parent_docname = (
                    SELECT d.remote_docname FROM delivery_jobs d
                    WHERE d.delivery_id = attachment_jobs.delivery_id
                ),
                updated_at = ?
            WHERE state = 'waiting_for_checkin'
              AND EXISTS (
                  SELECT 1 FROM delivery_jobs d
                  WHERE d.delivery_id = attachment_jobs.delivery_id
                    AND d.state = 'delivered'
                    AND d.remote_docname <> ''
              )
            """,
            (stamp,),
        )
        connection.execute(
            """
            UPDATE attachment_jobs
            SET state = 'cancelled',
                last_error_class = 'parent_delivery_not_created',
                last_error = 'Employee Checkin delivery reached a terminal non-success state',
                updated_at = ?
            WHERE state IN ('waiting_for_checkin', 'pending', 'retry_wait')
              AND EXISTS (
                  SELECT 1 FROM delivery_jobs d
                  WHERE d.delivery_id = attachment_jobs.delivery_id
                    AND d.state IN ('permanent_failure', 'cancelled')
              )
            """,
            (stamp,),
        )

    def _recover_expired_attachment_job_leases_tx(
        self,
        connection,
        *,
        max_attempts,
        now,
    ):
        max_attempts = _strict_int(
            max_attempts,
            "attachment max attempts",
            minimum=1,
            maximum=100,
        )
        stamp = timestamp_from_unix(now)
        rows = connection.execute(
            """
            SELECT * FROM attachment_jobs
            WHERE state = 'leased' AND lease_expires_unix <= ?
            ORDER BY lease_expires_unix, created_at, attachment_id
            """,
            (now,),
        ).fetchall()
        results = []
        for row in rows:
            if row["submission_started_at"]:
                state = "uncertain"
                error_class = "attachment_lease_expired_after_submission"
                error = "attachment worker lease expired after upload began"
                next_attempt = 0.0
            elif int(row["attempt_count"]) >= max_attempts:
                state = "permanent_failure"
                error_class = "attachment_retry_budget_exhausted"
                error = "attachment retry budget exhausted after lease expiry"
                next_attempt = 0.0
            else:
                state = "retry_wait"
                error_class = "attachment_lease_expired_before_submission"
                error = "attachment worker lease expired before upload began"
                next_attempt = now
            connection.execute(
                """
                UPDATE attachment_jobs
                SET state = ?, next_attempt_unix = ?, lease_owner = '',
                    lease_acquired_at = '', lease_heartbeat_at = '',
                    lease_expires_unix = 0, submission_started_at = '',
                    retry_delay_seconds = 0, last_error_class = ?,
                    last_error = ?, updated_at = ?
                WHERE attachment_id = ?
                """,
                (
                    state,
                    next_attempt,
                    error_class,
                    error,
                    stamp,
                    row["attachment_id"],
                ),
            )
            current = self._attachment_job_row_tx(
                connection, row["attachment_id"]
            )
            results.append(dict(current))
        return results

    def recover_attachment_jobs(self, *, max_attempts, now=None):
        now = _finite(
            now if now is not None else datetime.now(timezone.utc).timestamp(),
            "now",
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._refresh_attachment_dependencies_tx(connection, now=now)
            results = self._recover_expired_attachment_job_leases_tx(
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

    def claim_next_attachment_job(
        self,
        *,
        owner,
        lease_seconds,
        transport,
        max_attempts,
        now=None,
    ):
        owner = _text(owner, "attachment lease owner", required=True, max_chars=256)
        transport = _text(
            transport, "attachment transport", required=True, max_chars=64
        )
        lease_seconds = _strict_int(
            lease_seconds,
            "attachment lease seconds",
            minimum=30,
            maximum=3600,
        )
        max_attempts = _strict_int(
            max_attempts,
            "attachment max attempts",
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
            self._refresh_attachment_dependencies_tx(connection, now=now)
            self._recover_expired_attachment_job_leases_tx(
                connection,
                max_attempts=max_attempts,
                now=now,
            )
            exhausted = connection.execute(
                """
                SELECT attachment_id FROM attachment_jobs
                WHERE state IN ('pending', 'retry_wait')
                  AND next_attempt_unix <= ?
                  AND attempt_count >= ?
                """,
                (now, max_attempts),
            ).fetchall()
            for row in exhausted:
                connection.execute(
                    """
                    UPDATE attachment_jobs
                    SET state = 'permanent_failure', next_attempt_unix = 0,
                        retry_delay_seconds = 0,
                        last_error_class = 'attachment_retry_budget_exhausted',
                        last_error = 'attachment retry budget exhausted',
                        updated_at = ?
                    WHERE attachment_id = ?
                    """,
                    (stamp, row["attachment_id"]),
                )
            row = connection.execute(
                """
                SELECT a.*, d.remote_docname AS confirmed_parent_docname
                FROM attachment_jobs a
                JOIN delivery_jobs d ON d.delivery_id = a.delivery_id
                WHERE a.state IN ('pending', 'retry_wait')
                  AND a.next_attempt_unix <= ?
                  AND a.attempt_count < ?
                  AND d.state = 'delivered'
                  AND d.remote_docname <> ''
                ORDER BY a.next_attempt_unix, a.created_at, a.attachment_id
                LIMIT 1
                """,
                (now, max_attempts),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            expires = now + lease_seconds
            parent_docname = validate_erp_docname(row["confirmed_parent_docname"])
            connection.execute(
                """
                UPDATE attachment_jobs
                SET state = 'leased', parent_docname = ?, transport = ?,
                    attempt_count = attempt_count + 1,
                    next_attempt_unix = 0, lease_owner = ?,
                    lease_acquired_at = ?, lease_heartbeat_at = ?,
                    lease_expires_unix = ?, submission_started_at = '',
                    retry_delay_seconds = 0, last_error_class = '',
                    last_error = '', updated_at = ?
                WHERE attachment_id = ?
                """,
                (
                    parent_docname,
                    transport,
                    owner,
                    stamp,
                    stamp,
                    expires,
                    stamp,
                    row["attachment_id"],
                ),
            )
            current = self._attachment_job_row_tx(
                connection, row["attachment_id"]
            )
            connection.commit()
            return dict(current)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def renew_attachment_job_lease(
        self,
        attachment_id,
        *,
        owner,
        lease_seconds,
        now=None,
    ):
        attachment_id = _identifier(attachment_id, "attachment_id")
        owner = _text(owner, "attachment lease owner", required=True, max_chars=256)
        lease_seconds = _strict_int(
            lease_seconds,
            "attachment lease seconds",
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
                UPDATE attachment_jobs
                SET lease_heartbeat_at = ?, lease_expires_unix = ?, updated_at = ?
                WHERE attachment_id = ? AND state = 'leased'
                  AND lease_owner = ? AND lease_expires_unix > ?
                """,
                (
                    stamp,
                    now + lease_seconds,
                    stamp,
                    attachment_id,
                    owner,
                    now,
                ),
            )
            connection.commit()
            if cursor.rowcount != 1:
                raise AttachmentJobStateError(
                    "renewal requires the current attachment lease"
                )
            return self.get_attachment_job(attachment_id)
        finally:
            connection.close()

    def _current_attachment_lease_tx(
        self,
        connection,
        *,
        attachment_id,
        owner,
        now,
    ):
        attachment_id = _identifier(attachment_id, "attachment_id")
        owner = _text(owner, "attachment lease owner", required=True, max_chars=256)
        row = self._attachment_job_row_tx(connection, attachment_id)
        if row is None:
            raise AttachmentJobStateError(
                f"attachment job does not exist: {attachment_id}"
            )
        if (
            row["state"] != "leased"
            or row["lease_owner"] != owner
            or float(row["lease_expires_unix"] or 0) <= now
        ):
            raise AttachmentJobStateError(
                "operation requires the current unexpired attachment lease"
            )
        return row

    def mark_attachment_submission_started(
        self,
        attachment_id,
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
            row = self._current_attachment_lease_tx(
                connection,
                attachment_id=attachment_id,
                owner=owner,
                now=now,
            )
            if not row["submission_started_at"]:
                connection.execute(
                    """
                    UPDATE attachment_jobs
                    SET submission_started_at = ?, lease_heartbeat_at = ?,
                        updated_at = ? WHERE attachment_id = ?
                    """,
                    (stamp, stamp, stamp, row["attachment_id"]),
                )
            current = self._attachment_job_row_tx(
                connection, row["attachment_id"]
            )
            connection.commit()
            return dict(current)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def mark_attachment_job_retry_by_lease(
        self,
        attachment_id,
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
            "attachment error class",
            required=True,
            max_chars=128,
        )
        if not isinstance(safe_after_submission, bool):
            raise AttachmentOutboxValidationError(
                "safe_after_submission must be a boolean"
            )
        delay_seconds = _finite(delay_seconds, "attachment retry delay")
        if delay_seconds > 86400:
            raise AttachmentOutboxValidationError(
                "attachment retry delay must not exceed 86400 seconds"
            )
        max_attempts = _strict_int(
            max_attempts,
            "attachment max attempts",
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
            row = self._current_attachment_lease_tx(
                connection,
                attachment_id=attachment_id,
                owner=owner,
                now=now,
            )
            if row["submission_started_at"] and not safe_after_submission:
                raise AttachmentJobStateError(
                    "ambiguous post-submission attachment failures cannot be retried"
                )
            if int(row["attempt_count"]) >= max_attempts:
                state = "permanent_failure"
                error_class = "attachment_retry_budget_exhausted"
                next_attempt = 0.0
                delay_seconds = 0.0
            else:
                state = "retry_wait"
                next_attempt = now + delay_seconds
            connection.execute(
                """
                UPDATE attachment_jobs
                SET state = ?, next_attempt_unix = ?, lease_owner = '',
                    lease_acquired_at = '', lease_heartbeat_at = '',
                    lease_expires_unix = 0, submission_started_at = '',
                    retry_delay_seconds = ?, last_error_class = ?,
                    last_error = ?, updated_at = ?
                WHERE attachment_id = ?
                """,
                (
                    state,
                    next_attempt,
                    delay_seconds,
                    error_class,
                    _safe_error(error),
                    stamp,
                    row["attachment_id"],
                ),
            )
            current = self._attachment_job_row_tx(
                connection, row["attachment_id"]
            )
            connection.commit()
            return dict(current)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def mark_attachment_job_permanent_failure_by_lease(
        self,
        attachment_id,
        *,
        owner,
        error_class,
        error="",
        now=None,
    ):
        error_class = _text(
            error_class,
            "attachment error class",
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
            row = self._current_attachment_lease_tx(
                connection,
                attachment_id=attachment_id,
                owner=owner,
                now=now,
            )
            connection.execute(
                """
                UPDATE attachment_jobs
                SET state = 'permanent_failure', next_attempt_unix = 0,
                    lease_owner = '', lease_acquired_at = '',
                    lease_heartbeat_at = '', lease_expires_unix = 0,
                    retry_delay_seconds = 0, last_error_class = ?,
                    last_error = ?, updated_at = ?
                WHERE attachment_id = ?
                """,
                (
                    error_class,
                    _safe_error(error),
                    stamp,
                    row["attachment_id"],
                ),
            )
            current = self._attachment_job_row_tx(
                connection, row["attachment_id"]
            )
            connection.commit()
            return dict(current)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def mark_attachment_job_uncertain_by_lease(
        self,
        attachment_id,
        *,
        owner,
        error_class,
        error="",
        now=None,
    ):
        error_class = _text(
            error_class,
            "attachment error class",
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
            row = self._current_attachment_lease_tx(
                connection,
                attachment_id=attachment_id,
                owner=owner,
                now=now,
            )
            connection.execute(
                """
                UPDATE attachment_jobs
                SET state = 'uncertain', next_attempt_unix = 0,
                    lease_owner = '', lease_acquired_at = '',
                    lease_heartbeat_at = '', lease_expires_unix = 0,
                    retry_delay_seconds = 0, last_error_class = ?,
                    last_error = ?, updated_at = ?
                WHERE attachment_id = ?
                """,
                (
                    error_class,
                    _safe_error(error),
                    stamp,
                    row["attachment_id"],
                ),
            )
            current = self._attachment_job_row_tx(
                connection, row["attachment_id"]
            )
            connection.commit()
            return dict(current)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def mark_attachment_job_attached_by_lease(
        self,
        attachment_id,
        *,
        owner,
        transport,
        remote_file_docname="",
        remote_file_url="",
        now=None,
    ):
        transport = _text(
            transport, "attachment transport", required=True, max_chars=64
        )
        remote_file_docname = _text(
            remote_file_docname,
            "remote_file_docname",
            max_chars=255,
        )
        if remote_file_docname:
            remote_file_docname = validate_erp_docname(remote_file_docname)
        remote_file_url = _text(
            remote_file_url, "remote_file_url", max_chars=2048
        )
        now = _finite(
            now if now is not None else datetime.now(timezone.utc).timestamp(),
            "now",
        )
        stamp = timestamp_from_unix(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = self._current_attachment_lease_tx(
                connection,
                attachment_id=attachment_id,
                owner=owner,
                now=now,
            )
            if not row["submission_started_at"]:
                raise AttachmentJobStateError(
                    "attachment submission has not started"
                )
            connection.execute(
                """
                UPDATE attachment_jobs
                SET state = 'attached', transport = ?, next_attempt_unix = 0,
                    lease_owner = '', lease_acquired_at = '',
                    lease_heartbeat_at = '', lease_expires_unix = 0,
                    retry_delay_seconds = 0, last_error_class = '',
                    last_error = '', remote_file_docname = ?,
                    remote_file_url = ?, updated_at = ?, attached_at = ?
                WHERE attachment_id = ?
                """,
                (
                    transport,
                    remote_file_docname,
                    remote_file_url,
                    stamp,
                    stamp,
                    row["attachment_id"],
                ),
            )
            current = self._attachment_job_row_tx(
                connection, row["attachment_id"]
            )
            connection.commit()
            return dict(current)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
