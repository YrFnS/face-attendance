import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def resolve_runtime_path(root, value, default):
    path = Path(value or default)
    return path if path.is_absolute() else Path(root) / path


def file_sha256(path, max_bytes=0, chunk_size=1024 * 1024):
    path = Path(path)
    size = path.stat().st_size
    if max_bytes and size > int(max_bytes):
        raise ValueError(f"file exceeds maximum size of {int(max_bytes)} bytes")
    digest = hashlib.sha256()
    read = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            read += len(chunk)
            if max_bytes and read > int(max_bytes):
                raise ValueError(f"file exceeds maximum size of {int(max_bytes)} bytes")
            digest.update(chunk)
    return digest.hexdigest(), size


def make_event_id(camera_id, log_type, source_sha256):
    value = "\0".join((str(camera_id), str(log_type), str(source_sha256)))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EventClaim:
    accepted: bool
    event_id: str
    reason: str = ""
    existing_status: str = ""


class RuntimeState:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _initialize(self):
        connection = self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS camera_events (
                    event_id TEXT PRIMARY KEY,
                    camera_id TEXT NOT NULL,
                    log_type TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    source_mtime REAL,
                    source_size INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_unix REAL NOT NULL,
                    updated_unix REAL NOT NULL,
                    completed_at TEXT,
                    error TEXT NOT NULL DEFAULT ''
                );
                CREATE UNIQUE INDEX IF NOT EXISTS camera_events_camera_hash
                    ON camera_events(camera_id, source_sha256);
                CREATE INDEX IF NOT EXISTS camera_events_created
                    ON camera_events(created_unix);

                CREATE TABLE IF NOT EXISTS login_limits (
                    limiter_key TEXT PRIMARY KEY,
                    window_started REAL NOT NULL,
                    failures INTEGER NOT NULL,
                    locked_until REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS admin_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    remote_addr TEXT NOT NULL,
                    detail_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS admin_audit_created
                    ON admin_audit(id DESC);
                """
            )
            connection.commit()
        finally:
            connection.close()

    def claim_event(
        self,
        *,
        event_id,
        camera_id,
        log_type,
        source_sha256,
        source_name,
        source_mtime,
        source_size,
    ):
        now = time.time()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT event_id, status
                FROM camera_events
                WHERE event_id = ? OR (camera_id = ? AND source_sha256 = ?)
                LIMIT 1
                """,
                (event_id, camera_id, source_sha256),
            ).fetchone()
            if existing:
                connection.rollback()
                return EventClaim(
                    False,
                    existing["event_id"],
                    reason="duplicate",
                    existing_status=existing["status"],
                )
            connection.execute(
                """
                INSERT INTO camera_events (
                    event_id, camera_id, log_type, source_sha256, source_name,
                    source_mtime, source_size, status, created_unix, updated_unix
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'processing', ?, ?)
                """,
                (
                    event_id,
                    camera_id,
                    log_type,
                    source_sha256,
                    source_name,
                    source_mtime,
                    int(source_size),
                    now,
                    now,
                ),
            )
            connection.commit()
            return EventClaim(True, event_id)
        finally:
            connection.close()

    def finish_event(self, event_id, status="processed", error=""):
        now = time.time()
        connection = self._connect()
        try:
            connection.execute(
                """
                UPDATE camera_events
                SET status = ?, error = ?, updated_unix = ?, completed_at = ?
                WHERE event_id = ?
                """,
                (status, str(error or "")[:2000], now, utc_now(), event_id),
            )
            connection.commit()
        finally:
            connection.close()

    def get_event(self, event_id):
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM camera_events WHERE event_id = ?", (event_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            connection.close()

    def prune_events(self, retention_days):
        retention_days = int(retention_days or 0)
        if retention_days <= 0:
            return 0
        cutoff = time.time() - retention_days * 86400
        connection = self._connect()
        try:
            cursor = connection.execute(
                "DELETE FROM camera_events WHERE created_unix < ?", (cutoff,)
            )
            connection.commit()
            return cursor.rowcount
        finally:
            connection.close()

    def login_allowed(self, limiter_key):
        now = time.time()
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT locked_until FROM login_limits WHERE limiter_key = ?",
                (limiter_key,),
            ).fetchone()
        finally:
            connection.close()
        if not row or float(row["locked_until"] or 0) <= now:
            return True, 0
        return False, max(1, int(float(row["locked_until"]) - now))

    def record_login_failure(
        self,
        limiter_key,
        *,
        max_attempts=5,
        window_seconds=300,
        lockout_seconds=900,
    ):
        now = time.time()
        max_attempts = max(1, int(max_attempts))
        window_seconds = max(1, int(window_seconds))
        lockout_seconds = max(1, int(lockout_seconds))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM login_limits WHERE limiter_key = ?", (limiter_key,)
            ).fetchone()
            if not row or now - float(row["window_started"]) > window_seconds:
                window_started = now
                failures = 1
                locked_until = 0.0
            else:
                window_started = float(row["window_started"])
                failures = int(row["failures"]) + 1
                locked_until = float(row["locked_until"] or 0)
            if failures >= max_attempts:
                locked_until = max(locked_until, now + lockout_seconds)
            connection.execute(
                """
                INSERT INTO login_limits (
                    limiter_key, window_started, failures, locked_until
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(limiter_key) DO UPDATE SET
                    window_started = excluded.window_started,
                    failures = excluded.failures,
                    locked_until = excluded.locked_until
                """,
                (limiter_key, window_started, failures, locked_until),
            )
            connection.commit()
            return failures, locked_until
        finally:
            connection.close()

    def clear_login_failures(self, limiter_key):
        connection = self._connect()
        try:
            connection.execute(
                "DELETE FROM login_limits WHERE limiter_key = ?", (limiter_key,)
            )
            connection.commit()
        finally:
            connection.close()

    def audit(self, *, actor, action, remote_addr="", detail=None):
        detail_json = json.dumps(detail or {}, ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute(
                """
                INSERT INTO admin_audit (
                    created_at, actor, action, remote_addr, detail_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    str(actor or "unknown")[:120],
                    str(action or "unknown")[:120],
                    str(remote_addr or "")[:120],
                    detail_json[:8000],
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def recent_audit(self, limit=20):
        limit = min(max(1, int(limit)), 200)
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT created_at, actor, action, remote_addr, detail_json
                FROM admin_audit
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        finally:
            connection.close()
        output = []
        for row in rows:
            item = dict(row)
            try:
                item["detail"] = json.loads(item.pop("detail_json"))
            except json.JSONDecodeError:
                item["detail"] = {}
                item.pop("detail_json", None)
            output.append(item)
        return output
