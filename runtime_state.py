import hashlib
import json
import math
import os
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from event_ledger import (
    EventLedgerMixin,
    LEDGER_REQUIRED_INDEXES,
    LEDGER_REQUIRED_TABLE_COLUMNS,
    LEDGER_REQUIRED_TRIGGERS,
    LEDGER_SCHEMA_STATEMENTS,
    make_capture_id,
)
from processing_recovery import (
    ProcessingRecoveryMixin,
    RECOVERY_REQUIRED_INDEXES,
    RECOVERY_REQUIRED_TABLE_COLUMNS,
    RECOVERY_SCHEMA_STATEMENTS,
)


RUNTIME_SCHEMA_VERSION = 3
MIGRATION_TABLE = "schema_migrations"
DEFAULT_BACKUP_DIRECTORY = "runtime_state_backups"


class RuntimeStateError(RuntimeError):
    pass


class RuntimeStateMigrationError(RuntimeStateError):
    pass


class RuntimeStateBackupError(RuntimeStateError):
    pass


class RuntimeStateVerificationError(RuntimeStateError):
    pass


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


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]

    @property
    def checksum(self):
        payload = json.dumps(
            {
                "version": self.version,
                "name": self.name,
                "statements": list(self.statements),
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


BASELINE_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        checksum TEXT NOT NULL,
        applied_at TEXT NOT NULL
    )
    """,
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
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS camera_events_camera_hash
        ON camera_events(camera_id, source_sha256)
    """,
    """
    CREATE INDEX IF NOT EXISTS camera_events_created
        ON camera_events(created_unix)
    """,
    """
    CREATE TABLE IF NOT EXISTS login_limits (
        limiter_key TEXT PRIMARY KEY,
        window_started REAL NOT NULL,
        failures INTEGER NOT NULL,
        locked_until REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS request_rate_limits (
        bucket_key TEXT PRIMARY KEY,
        window_started REAL NOT NULL,
        request_count INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS admin_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        actor TEXT NOT NULL,
        action TEXT NOT NULL,
        remote_addr TEXT NOT NULL,
        detail_json TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS admin_audit_created
        ON admin_audit(id DESC)
    """,
)


MIGRATIONS = (
    Migration(1, "baseline_runtime_state", BASELINE_SCHEMA_STATEMENTS),
    Migration(2, "versioned_event_ledger", LEDGER_SCHEMA_STATEMENTS),
    Migration(3, "processing_leases_and_policy_state", RECOVERY_SCHEMA_STATEMENTS),
)
MIGRATION_BY_VERSION = {migration.version: migration for migration in MIGRATIONS}


# Column specifications are (declared_type, not_null, primary_key_position).
# SQLite reports TEXT/INTEGER primary keys with not_null=0, so primary-key
# position is verified separately rather than inferred from NOT NULL.
REQUIRED_TABLE_COLUMNS = {
    "schema_migrations": {
        "version": ("INTEGER", False, 1),
        "name": ("TEXT", True, 0),
        "checksum": ("TEXT", True, 0),
        "applied_at": ("TEXT", True, 0),
    },
    "camera_events": {
        "event_id": ("TEXT", False, 1),
        "camera_id": ("TEXT", True, 0),
        "log_type": ("TEXT", True, 0),
        "source_sha256": ("TEXT", True, 0),
        "source_name": ("TEXT", True, 0),
        "source_mtime": ("REAL", False, 0),
        "source_size": ("INTEGER", True, 0),
        "status": ("TEXT", True, 0),
        "created_unix": ("REAL", True, 0),
        "updated_unix": ("REAL", True, 0),
        "completed_at": ("TEXT", False, 0),
        "error": ("TEXT", True, 0),
    },
    "login_limits": {
        "limiter_key": ("TEXT", False, 1),
        "window_started": ("REAL", True, 0),
        "failures": ("INTEGER", True, 0),
        "locked_until": ("REAL", True, 0),
    },
    "request_rate_limits": {
        "bucket_key": ("TEXT", False, 1),
        "window_started": ("REAL", True, 0),
        "request_count": ("INTEGER", True, 0),
    },
    "admin_audit": {
        "id": ("INTEGER", False, 1),
        "created_at": ("TEXT", True, 0),
        "actor": ("TEXT", True, 0),
        "action": ("TEXT", True, 0),
        "remote_addr": ("TEXT", True, 0),
        "detail_json": ("TEXT", True, 0),
    },
}
REQUIRED_INDEXES = {
    # index_name: (unique, ordered columns)
    "camera_events_camera_hash": (True, ("camera_id", "source_sha256")),
    "camera_events_created": (False, ("created_unix",)),
    "admin_audit_created": (False, ("id",)),
}


def _validate_migration_catalog():
    versions = [migration.version for migration in MIGRATIONS]
    expected = list(range(1, RUNTIME_SCHEMA_VERSION + 1))
    if versions != expected:
        raise RuntimeStateMigrationError(
            f"migration catalog must contain contiguous versions {expected}, got {versions}"
        )
    names = [migration.name for migration in MIGRATIONS]
    if len(names) != len(set(names)):
        raise RuntimeStateMigrationError("migration names must be unique")


_validate_migration_catalog()


def _database_present(path):
    path = Path(path)
    return path.exists() and path.is_file() and path.stat().st_size > 0


def _assert_regular_path(path, *, label, must_exist=True):
    path = Path(path)
    if path.is_symlink():
        raise RuntimeStateBackupError(f"{label} must not be a symbolic link: {path}")
    if must_exist and not path.is_file():
        raise RuntimeStateBackupError(f"{label} is not a regular file: {path}")
    return path


def _open_connection(path, *, runtime=False):
    connection = sqlite3.connect(Path(path), timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=10000")
    connection.execute("PRAGMA foreign_keys=ON")
    if runtime:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
    else:
        connection.execute("PRAGMA synchronous=FULL")
    return connection


def _table_exists(connection, table):
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _migration_history(connection):
    if not _table_exists(connection, MIGRATION_TABLE):
        return []
    try:
        rows = connection.execute(
            "SELECT version, name, checksum, applied_at "
            "FROM schema_migrations ORDER BY version"
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise RuntimeStateMigrationError(
            f"cannot read schema migration history: {exc}"
        ) from exc
    return [dict(row) for row in rows]


def _schema_version(connection):
    user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    history = _migration_history(connection)
    if not history:
        if user_version != 0:
            raise RuntimeStateMigrationError(
                "PRAGMA user_version is nonzero but schema_migrations has no history"
            )
        return 0, history

    versions = [int(row["version"]) for row in history]
    expected = list(range(1, versions[-1] + 1))
    if versions != expected:
        raise RuntimeStateMigrationError(
            f"schema migration history is not contiguous: {versions}"
        )
    if versions[-1] > RUNTIME_SCHEMA_VERSION:
        raise RuntimeStateMigrationError(
            f"database schema version {versions[-1]} is newer than supported "
            f"version {RUNTIME_SCHEMA_VERSION}"
        )
    for row in history:
        version = int(row["version"])
        expected_migration = MIGRATION_BY_VERSION.get(version)
        if expected_migration is None:
            raise RuntimeStateMigrationError(
                f"database contains unknown migration version {version}"
            )
        if row["name"] != expected_migration.name:
            raise RuntimeStateMigrationError(
                f"migration {version} name mismatch: {row['name']!r}"
            )
        if row["checksum"] != expected_migration.checksum:
            raise RuntimeStateMigrationError(
                f"migration {version} checksum mismatch"
            )
    current = versions[-1]
    if user_version != current:
        raise RuntimeStateMigrationError(
            f"PRAGMA user_version {user_version} does not match migration history {current}"
        )
    return current, history


def _required_schema_errors(connection, version=None):
    version = RUNTIME_SCHEMA_VERSION if version is None else int(version)
    table_requirements = {
        table: dict(columns) for table, columns in REQUIRED_TABLE_COLUMNS.items()
    }
    index_requirements = dict(REQUIRED_INDEXES)
    trigger_requirements = set()
    if version >= 2:
        for table, columns in LEDGER_REQUIRED_TABLE_COLUMNS.items():
            table_requirements.setdefault(table, {}).update(columns)
        index_requirements.update(LEDGER_REQUIRED_INDEXES)
        trigger_requirements.update(LEDGER_REQUIRED_TRIGGERS)
    if version >= 3:
        for table, columns in RECOVERY_REQUIRED_TABLE_COLUMNS.items():
            table_requirements.setdefault(table, {}).update(columns)
        index_requirements.update(RECOVERY_REQUIRED_INDEXES)

    errors = []
    for table, required in table_requirements.items():
        if not _table_exists(connection, table):
            errors.append(f"required table is missing: {table}")
            continue
        rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        columns = {row["name"]: row for row in rows}
        missing = sorted(set(required) - set(columns))
        if missing:
            errors.append(
                f"table {table} is missing required columns: {', '.join(missing)}"
            )
        for name, (expected_type, expected_not_null, expected_pk) in required.items():
            row = columns.get(name)
            if row is None:
                continue
            actual_type = str(row["type"] or "").strip().upper()
            if actual_type != expected_type:
                errors.append(
                    f"table {table} column {name} has type "
                    f"{actual_type or '<empty>'}; expected {expected_type}"
                )
            if bool(row["notnull"]) is not bool(expected_not_null):
                errors.append(f"table {table} column {name} NOT NULL mismatch")
            if int(row["pk"] or 0) != int(expected_pk):
                errors.append(
                    f"table {table} column {name} primary-key position mismatch"
                )

    index_rows = connection.execute(
        "SELECT name, tbl_name FROM sqlite_master WHERE type = 'index'"
    ).fetchall()
    indexes = {row["name"]: row for row in index_rows}
    for index_name, (expected_unique, expected_columns) in index_requirements.items():
        row = indexes.get(index_name)
        if row is None:
            errors.append(f"required indexes are missing: {index_name}")
            continue
        table = row["tbl_name"]
        listed = {
            item["name"]: bool(item["unique"])
            for item in connection.execute(
                f'PRAGMA index_list("{table}")'
            ).fetchall()
        }
        if listed.get(index_name) is not bool(expected_unique):
            errors.append(f"index {index_name} uniqueness mismatch")
        actual_columns = tuple(
            item["name"]
            for item in connection.execute(
                f'PRAGMA index_info("{index_name}")'
            ).fetchall()
        )
        if actual_columns != expected_columns:
            errors.append(
                f"index {index_name} columns {actual_columns!r}; "
                f"expected {expected_columns!r}"
            )

    if trigger_requirements:
        triggers = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
        }
        missing_triggers = sorted(trigger_requirements - triggers)
        if missing_triggers:
            errors.append(
                "required triggers are missing: " + ", ".join(missing_triggers)
            )
    return errors


def _integrity_errors(connection):
    try:
        rows = connection.execute("PRAGMA quick_check").fetchall()
    except sqlite3.DatabaseError as exc:
        return [f"SQLite quick_check failed: {exc}"]
    messages = [str(row[0]) for row in rows]
    return [] if messages == ["ok"] else [f"SQLite quick_check: {item}" for item in messages]


def inspect_runtime_database(path, *, require_latest=False):
    path = Path(path)
    report = {
        "path": str(path),
        "exists": path.exists(),
        "schema_version": 0,
        "latest_schema_version": RUNTIME_SCHEMA_VERSION,
        "pending_migrations": list(range(1, RUNTIME_SCHEMA_VERSION + 1)),
        "migration_history": [],
        "integrity_ok": False,
        "schema_ok": False,
        "ok": False,
        "errors": [],
    }
    if not path.exists():
        report["errors"].append("database does not exist")
        return report
    if path.is_symlink():
        report["errors"].append("database must not be a symbolic link")
        return report
    if not path.is_file():
        report["errors"].append("database is not a regular file")
        return report
    if path.stat().st_size == 0:
        report["errors"].append("database file is empty")
        return report

    try:
        connection = _open_connection(path)
    except sqlite3.DatabaseError as exc:
        report["errors"].append(f"cannot open database: {exc}")
        return report
    try:
        integrity_errors = _integrity_errors(connection)
        report["integrity_ok"] = not integrity_errors
        report["errors"].extend(integrity_errors)
        try:
            current, history = _schema_version(connection)
            report["schema_version"] = current
            report["migration_history"] = history
            report["pending_migrations"] = list(
                range(current + 1, RUNTIME_SCHEMA_VERSION + 1)
            )
        except RuntimeStateMigrationError as exc:
            report["errors"].append(str(exc))
            return report

        schema_errors = _required_schema_errors(connection, version=current) if current else []
        report["schema_ok"] = not schema_errors
        report["errors"].extend(schema_errors)
        if require_latest and current != RUNTIME_SCHEMA_VERSION:
            report["errors"].append(
                f"database schema version {current} is not current "
                f"({RUNTIME_SCHEMA_VERSION})"
            )
        report["ok"] = not report["errors"]
        return report
    finally:
        connection.close()


def _safe_reason(value):
    cleaned = "".join(
        character if character.isalnum() or character in {"-", "_"} else "-"
        for character in str(value or "backup").strip().lower()
    ).strip("-")
    return cleaned[:48] or "backup"


def _backup_directory(database_path, backup_dir=None):
    database_path = Path(database_path)
    return (
        Path(backup_dir)
        if backup_dir is not None
        else database_path.parent / DEFAULT_BACKUP_DIRECTORY
    )


def _fsync_file(path):
    with Path(path).open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path):
    if os.name == "nt":
        return
    descriptor = os.open(Path(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_json_atomic(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temp_path, 0o600)
        except OSError:
            pass
        os.replace(temp_path, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def create_runtime_backup(
    database_path,
    backup_dir=None,
    *,
    reason="manual",
    source_version=None,
    target_version=None,
):
    database_path = _assert_regular_path(
        database_path, label="runtime database"
    )
    source_report = inspect_runtime_database(database_path, require_latest=False)
    if source_report["errors"]:
        raise RuntimeStateBackupError(
            "runtime database cannot be backed up safely: "
            + "; ".join(source_report["errors"])
        )
    declared_source = None if source_version is None else int(source_version)
    declared_target = None if target_version is None else int(target_version)
    if declared_target is not None and declared_target < 0:
        raise RuntimeStateBackupError("target schema version must not be negative")
    directory = _backup_directory(database_path, backup_dir)
    if directory.is_symlink():
        raise RuntimeStateBackupError(
            f"backup directory must not be a symbolic link: {directory}"
        )
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass

    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{database_path.stem}.backup.",
        suffix=".sqlite3",
        dir=directory,
    )
    os.close(descriptor)
    temp_path = Path(temp_name)
    try:
        temp_path.unlink()
        source = _open_connection(database_path)
        destination = sqlite3.connect(temp_path, timeout=10)
        try:
            source.backup(destination)
            destination.commit()
            errors = _integrity_errors(destination)
            if errors:
                raise RuntimeStateBackupError("; ".join(errors))
        finally:
            destination.close()
            source.close()
        copied_report = inspect_runtime_database(temp_path, require_latest=False)
        if not copied_report["ok"]:
            raise RuntimeStateBackupError(
                "copied backup verification failed: "
                + "; ".join(copied_report["errors"])
            )
        current = int(copied_report["schema_version"])
        if declared_source is not None and declared_source != current:
            raise RuntimeStateBackupError(
                f"declared source schema version {declared_source} does not "
                f"match copied database version {current}"
            )
        target = current if declared_target is None else declared_target
        try:
            os.chmod(temp_path, 0o600)
        except OSError:
            pass
        _fsync_file(temp_path)
        digest, size = file_sha256(temp_path)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        final_path = directory / (
            f"{database_path.stem}.{stamp}.v{current}-to-v{target}."
            f"{_safe_reason(reason)}.{digest[:12]}.sqlite3"
        )
        os.replace(temp_path, final_path)
        try:
            os.chmod(final_path, 0o600)
        except OSError:
            pass
        _fsync_directory(directory)
        metadata = {
            "schema": "face-attendance-runtime-backup/v1",
            "created_at": utc_now(),
            "reason": str(reason),
            "source_path": str(database_path.resolve()),
            "source_schema_version": current,
            "target_schema_version": target,
            "sha256": digest,
            "size": size,
            "backup_path": str(final_path.resolve()),
        }
        _write_json_atomic(final_path.with_suffix(final_path.suffix + ".json"), metadata)
        return {**metadata, "path": str(final_path)}
    except (OSError, sqlite3.DatabaseError, ValueError) as exc:
        if isinstance(exc, RuntimeStateBackupError):
            raise
        raise RuntimeStateBackupError(f"runtime database backup failed: {exc}") from exc
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def verify_runtime_backup(backup_path):
    backup_path = Path(backup_path)
    report = inspect_runtime_database(backup_path, require_latest=False)
    report["backup_path"] = str(backup_path)
    metadata_path = backup_path.with_suffix(backup_path.suffix + ".json")
    report["metadata_path"] = str(metadata_path)
    report["metadata"] = None
    if backup_path.is_symlink():
        report["errors"].append("backup must not be a symbolic link")
    if metadata_path.is_symlink():
        report["errors"].append(
            "backup metadata sidecar must not be a symbolic link"
        )
    elif metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if not isinstance(metadata, dict):
                raise ValueError("metadata must contain a JSON object")
            report["metadata"] = metadata
            digest, size = file_sha256(backup_path)
            if metadata.get("schema") != "face-attendance-runtime-backup/v1":
                report["errors"].append("backup metadata schema is unsupported")
            if metadata.get("sha256") != digest:
                report["errors"].append("backup SHA-256 does not match metadata")
            if int(metadata.get("size", -1)) != size:
                report["errors"].append("backup size does not match metadata")
            if int(metadata.get("source_schema_version", -1)) != int(
                report["schema_version"]
            ):
                report["errors"].append(
                    "backup schema version does not match metadata"
                )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            report["errors"].append(f"backup metadata is invalid: {exc}")
    else:
        report["errors"].append("backup metadata sidecar is missing")
    report["ok"] = not report["errors"]
    return report


def restore_runtime_backup(
    database_path,
    backup_path,
    backup_dir=None,
    *,
    confirm=False,
):
    if not confirm:
        raise RuntimeStateBackupError(
            "restore requires explicit confirmation; stop all services first"
        )
    database_path = Path(database_path)
    backup_path = _assert_regular_path(backup_path, label="runtime backup")
    verification = verify_runtime_backup(backup_path)
    if not verification["ok"]:
        raise RuntimeStateBackupError(
            "runtime backup verification failed: "
            + "; ".join(verification["errors"])
        )
    if database_path.is_symlink():
        raise RuntimeStateBackupError(
            f"runtime database must not be a symbolic link: {database_path}"
        )
    database_path.parent.mkdir(parents=True, exist_ok=True)

    safety_backup = None
    if _database_present(database_path):
        safety_backup = create_runtime_backup(
            database_path,
            backup_dir,
            reason="pre-restore",
            target_version=verification["schema_version"],
        )

    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{database_path.name}.restore.",
        dir=database_path.parent,
    )
    os.close(descriptor)
    temp_path = Path(temp_name)
    try:
        temp_path.unlink()
        source = _open_connection(backup_path)
        destination = sqlite3.connect(temp_path, timeout=10)
        try:
            source.backup(destination)
            destination.commit()
            errors = _integrity_errors(destination)
            if errors:
                raise RuntimeStateBackupError("; ".join(errors))
        finally:
            destination.close()
            source.close()
        copied_report = inspect_runtime_database(temp_path, require_latest=False)
        if not copied_report["ok"]:
            raise RuntimeStateBackupError(
                "restored temporary database verification failed: "
                + "; ".join(copied_report["errors"])
            )
        try:
            os.chmod(temp_path, 0o600)
        except OSError:
            pass
        _fsync_file(temp_path)
        os.replace(temp_path, database_path)
        for suffix in ("-wal", "-shm"):
            try:
                Path(str(database_path) + suffix).unlink()
            except FileNotFoundError:
                pass
        try:
            os.chmod(database_path, 0o600)
        except OSError:
            pass
        _fsync_directory(database_path.parent)
        restored = inspect_runtime_database(database_path, require_latest=False)
        if not restored["ok"]:
            raise RuntimeStateBackupError(
                "restored database verification failed: "
                + "; ".join(restored["errors"])
            )
        return {
            "ok": True,
            "database_path": str(database_path),
            "restored_from": str(backup_path),
            "schema_version": restored["schema_version"],
            "safety_backup": safety_backup,
        }
    except (OSError, sqlite3.DatabaseError) as exc:
        if isinstance(exc, RuntimeStateBackupError):
            raise
        raise RuntimeStateBackupError(f"runtime database restore failed: {exc}") from exc
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


class RuntimeState(ProcessingRecoveryMixin, EventLedgerMixin):
    def __init__(self, path, backup_dir=None):
        self.path = Path(path)
        self.backup_dir = _backup_directory(self.path, backup_dir)
        self.last_migration_backup = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise RuntimeStateMigrationError(
                f"runtime database must not be a symbolic link: {self.path}"
            )
        self._migrate()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _connect(self):
        return _open_connection(self.path, runtime=True)

    def _migrate(self):
        existing = _database_present(self.path)
        current = 0
        if existing:
            report = inspect_runtime_database(self.path, require_latest=False)
            if report["errors"]:
                raise RuntimeStateMigrationError(
                    "runtime database pre-migration verification failed: "
                    + "; ".join(report["errors"])
                )
            current = int(report["schema_version"])
        elif self.path.exists() and not self.path.is_file():
            raise RuntimeStateMigrationError(
                f"runtime database path is not a regular file: {self.path}"
            )

        if current < RUNTIME_SCHEMA_VERSION:
            if existing:
                self.last_migration_backup = create_runtime_backup(
                    self.path,
                    self.backup_dir,
                    reason="pre-migration",
                    target_version=RUNTIME_SCHEMA_VERSION,
                )
            connection = _open_connection(self.path)
            try:
                for migration in MIGRATIONS:
                    if migration.version > current:
                        try:
                            self._apply_migration(connection, migration)
                        except RuntimeStateError:
                            raise
                        except sqlite3.DatabaseError as exc:
                            raise RuntimeStateMigrationError(
                                f"migration {migration.version} "
                                f"({migration.name}) failed: {exc}"
                            ) from exc
                        current = migration.version
            finally:
                connection.close()

        report = inspect_runtime_database(self.path, require_latest=True)
        if not report["ok"]:
            raise RuntimeStateVerificationError(
                "runtime database post-migration verification failed: "
                + "; ".join(report["errors"])
            )
        self.schema_report = report

    @staticmethod
    def _apply_migration(connection, migration):
        try:
            connection.execute("BEGIN IMMEDIATE")
            current, _history = _schema_version(connection)
            # Another process may complete the same migration after this
            # process inspected the database but before it acquires the write
            # lock. An already-verified migration is a safe no-op.
            if current >= migration.version:
                connection.commit()
                return False
            if migration.version != current + 1:
                raise RuntimeStateMigrationError(
                    f"migration {migration.version} cannot follow schema version {current}"
                )
            for statement in migration.statements:
                connection.execute(statement)
            schema_errors = _required_schema_errors(
                connection, version=migration.version
            )
            if schema_errors:
                raise RuntimeStateMigrationError("; ".join(schema_errors))
            connection.execute(
                "INSERT INTO schema_migrations "
                "(version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
                (
                    migration.version,
                    migration.name,
                    migration.checksum,
                    utc_now(),
                ),
            )
            connection.execute(f"PRAGMA user_version = {migration.version}")
            verified_version, _verified_history = _schema_version(connection)
            if verified_version != migration.version:
                raise RuntimeStateMigrationError(
                    f"migration {migration.version} did not activate its schema version"
                )
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise

    def migration_status(self):
        return inspect_runtime_database(self.path, require_latest=True)

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
        received_at = utc_now()
        claim = self.record_event_receipt(
            event_id=event_id,
            capture_id=make_capture_id(
                camera_id,
                source_sha256,
                source_name,
                source_size,
                source_mtime,
            ),
            camera_id=camera_id,
            log_type=log_type,
            source_sha256=source_sha256,
            source_name=source_name,
            source_mtime=source_mtime,
            source_size=int(source_size),
            received_at=received_at,
            effective_at=received_at,
            policy=log_type,
            source_time_provenance="legacy",
            receipt_state="legacy",
            receipt_verified=False,
            receipt_detail={"compatibility_path": True},
            policy_version="legacy",
        )
        if not claim.accepted:
            return claim
        lease = self.acquire_event_lease(
            event_id,
            owner="compatibility-claim",
            lease_seconds=180,
        )
        return EventClaim(
            lease.accepted,
            event_id,
            reason=lease.reason,
            existing_status=lease.existing_status,
        )

    def finish_event(self, event_id, status="processed", error=""):
        mapping = {
            "checkin_created": ("checkin_created", "checkin_created"),
            "processed": ("processed", "processed_no_checkin"),
            "processed_no_checkin": ("processed", "processed_no_checkin"),
            "rejected": ("rejected", "generic_rejected"),
            "failed": ("failed", "generic_failed"),
            "uncertain": ("uncertain", "generic_failed"),
        }
        lifecycle, reason = mapping.get(
            status,
            ("failed", "generic_failed"),
        )
        return self.transition_event(
            event_id,
            to_state=lifecycle,
            reason_code=reason,
            actor_type="system",
            compatibility_status=status,
            error=error,
        )

    def get_event(self, event_id):
        return self.event_details(event_id, include_history=True)

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

    def consume_rate_limit(
        self,
        bucket_key,
        *,
        limit,
        window_seconds,
        now=None,
    ):
        bucket_key = str(bucket_key or "").strip()
        if not bucket_key:
            raise ValueError("rate-limit bucket key is required")
        if len(bucket_key) > 240:
            raise ValueError("rate-limit bucket key exceeds 240 characters")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("rate-limit limit must be a positive integer")
        if (
            isinstance(window_seconds, bool)
            or not isinstance(window_seconds, int)
            or window_seconds < 1
        ):
            raise ValueError("rate-limit window must be a positive integer")
        now = time.time() if now is None else float(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT window_started, request_count FROM request_rate_limits "
                "WHERE bucket_key = ?",
                (bucket_key,),
            ).fetchone()
            if not row or now - float(row["window_started"]) >= window_seconds:
                window_started = now
                count = 1
            else:
                window_started = float(row["window_started"])
                count = int(row["request_count"])
                if count >= limit:
                    retry_after = max(
                        1,
                        int(math.ceil(window_seconds - (now - window_started))),
                    )
                    connection.rollback()
                    return False, retry_after, 0
                count += 1
            connection.execute(
                """
                INSERT INTO request_rate_limits (
                    bucket_key, window_started, request_count
                ) VALUES (?, ?, ?)
                ON CONFLICT(bucket_key) DO UPDATE SET
                    window_started = excluded.window_started,
                    request_count = excluded.request_count
                """,
                (bucket_key, window_started, count),
            )
            connection.commit()
            return True, 0, max(0, limit - count)
        finally:
            connection.close()

    def clear_rate_limit(self, bucket_key):
        connection = self._connect()
        try:
            connection.execute(
                "DELETE FROM request_rate_limits WHERE bucket_key = ?",
                (str(bucket_key),),
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
