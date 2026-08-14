from pathlib import Path


ROOT = Path(__file__).resolve().parent


def read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def write(path, content):
    (ROOT / path).write_text(content, encoding="utf-8")


def replace_once(path, old, new):
    source = read(path)
    count = source.count(old)
    if count != 1:
        raise SystemExit(
            f"expected one match in {path}, found {count}: {old[:120]!r}"
        )
    write(path, source.replace(old, new, 1))


def patch_runtime_state():
    replace_once(
        "runtime_state.py",
        '''REQUIRED_TABLE_COLUMNS = {
    "schema_migrations": {"version", "name", "checksum", "applied_at"},
    "camera_events": {
        "event_id",
        "camera_id",
        "log_type",
        "source_sha256",
        "source_name",
        "source_mtime",
        "source_size",
        "status",
        "created_unix",
        "updated_unix",
        "completed_at",
        "error",
    },
    "login_limits": {
        "limiter_key",
        "window_started",
        "failures",
        "locked_until",
    },
    "request_rate_limits": {
        "bucket_key",
        "window_started",
        "request_count",
    },
    "admin_audit": {
        "id",
        "created_at",
        "actor",
        "action",
        "remote_addr",
        "detail_json",
    },
}
REQUIRED_INDEXES = {
    "camera_events_camera_hash",
    "camera_events_created",
    "admin_audit_created",
}
''',
        '''# Column specifications are (declared_type, not_null, primary_key_position).
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
''',
    )

    replace_once(
        "runtime_state.py",
        '''def _required_schema_errors(connection):
    errors = []
    for table, required in REQUIRED_TABLE_COLUMNS.items():
        if not _table_exists(connection, table):
            errors.append(f"required table is missing: {table}")
            continue
        columns = {
            row["name"]
            for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        }
        missing = sorted(required - columns)
        if missing:
            errors.append(
                f"table {table} is missing required columns: {', '.join(missing)}"
            )
    indexes = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }
    missing_indexes = sorted(REQUIRED_INDEXES - indexes)
    if missing_indexes:
        errors.append(
            "required indexes are missing: " + ", ".join(missing_indexes)
        )
    return errors
''',
        '''def _required_schema_errors(connection):
    errors = []
    for table, required in REQUIRED_TABLE_COLUMNS.items():
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
    for index_name, (expected_unique, expected_columns) in REQUIRED_INDEXES.items():
        row = indexes.get(index_name)
        if row is None:
            errors.append(f"required index is missing: {index_name}")
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
    return errors
''',
    )

    replace_once(
        "runtime_state.py",
        '''    current = (
        int(source_version)
        if source_version is not None
        else int(source_report["schema_version"])
    )
    target = current if target_version is None else int(target_version)
    directory = _backup_directory(database_path, backup_dir)
''',
        '''    declared_source = None if source_version is None else int(source_version)
    declared_target = None if target_version is None else int(target_version)
    if declared_target is not None and declared_target < 0:
        raise RuntimeStateBackupError("target schema version must not be negative")
    directory = _backup_directory(database_path, backup_dir)
''',
    )

    replace_once(
        "runtime_state.py",
        '''        copied_report = inspect_runtime_database(temp_path, require_latest=False)
        if not copied_report["ok"]:
            raise RuntimeStateBackupError(
                "copied backup verification failed: "
                + "; ".join(copied_report["errors"])
            )
        try:
            os.chmod(temp_path, 0o600)
''',
        '''        copied_report = inspect_runtime_database(temp_path, require_latest=False)
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
''',
    )

    replace_once(
        "runtime_state.py",
        '''    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            report["metadata"] = metadata
            digest, size = file_sha256(backup_path)
            if metadata.get("sha256") != digest:
                report["errors"].append("backup SHA-256 does not match metadata")
            if int(metadata.get("size", -1)) != size:
                report["errors"].append("backup size does not match metadata")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            report["errors"].append(f"backup metadata is invalid: {exc}")
    else:
        report["errors"].append("backup metadata sidecar is missing")
''',
        '''    if metadata_path.is_symlink():
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
            if metadata.get("backup_path") != str(backup_path.resolve()):
                report["errors"].append("backup path does not match metadata")
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
''',
    )

    replace_once(
        "runtime_state.py",
        '''        current_report = inspect_runtime_database(database_path, require_latest=False)
        safety_backup = create_runtime_backup(
            database_path,
            backup_dir,
            reason="pre-restore",
            source_version=current_report["schema_version"],
            target_version=verification["schema_version"],
        )
''',
        '''        safety_backup = create_runtime_backup(
            database_path,
            backup_dir,
            reason="pre-restore",
            target_version=verification["schema_version"],
        )
''',
    )

    replace_once(
        "runtime_state.py",
        '''                self.last_migration_backup = create_runtime_backup(
                    self.path,
                    self.backup_dir,
                    reason="pre-migration",
                    source_version=current,
                    target_version=RUNTIME_SCHEMA_VERSION,
                )
''',
        '''                self.last_migration_backup = create_runtime_backup(
                    self.path,
                    self.backup_dir,
                    reason="pre-migration",
                    target_version=RUNTIME_SCHEMA_VERSION,
                )
''',
    )

    replace_once(
        "runtime_state.py",
        '''            current, _history = _schema_version(connection)
            if migration.version != current + 1:
                raise RuntimeStateMigrationError(
                    f"migration {migration.version} cannot follow schema version {current}"
                )
            for statement in migration.statements:
''',
        '''            current, _history = _schema_version(connection)
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
''',
    )

    source = read("runtime_state.py")
    start = source.index("    @staticmethod\n    def _apply_migration")
    end = source.index("    def migration_status", start)
    block = source[start:end]
    old = '''            connection.commit()
        except Exception:
            connection.rollback()
            raise
'''
    if block.count(old) != 1:
        raise SystemExit("could not locate _apply_migration commit block")
    block = block.replace(
        old,
        '''            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
''',
        1,
    )
    write("runtime_state.py", source[:start] + block + source[end:])


def patch_tests():
    replace_once(
        "test_runtime_state.py",
        '''import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
''',
        '''import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
''',
    )

    marker = '''    def test_current_database_does_not_create_redundant_migration_backup(self):
'''
    addition = '''    def test_concurrent_legacy_initialization_is_safe(self):
        legacy = self.root / "concurrent.sqlite3"
        self.create_legacy_database(legacy)
        barrier = threading.Barrier(2)
        original = RuntimeState._apply_migration

        def synchronized(connection, migration):
            barrier.wait(timeout=10)
            return original(connection, migration)

        def initialize():
            return RuntimeState(legacy, backup_dir=self.backups)

        with patch.object(
            RuntimeState,
            "_apply_migration",
            staticmethod(synchronized),
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(initialize) for _ in range(2)]
                states = [future.result(timeout=20) for future in futures]

        self.assertTrue(all(state.migration_status()["ok"] for state in states))
        self.assertEqual(states[0].migration_status()["schema_version"], 1)
        self.assertGreaterEqual(len(list(self.backups.glob("*.sqlite3"))), 1)

    def test_legacy_schema_type_and_index_shape_are_verified(self):
        incompatible = self.root / "wrong-shape.sqlite3"
        schema = LEGACY_SCHEMA.replace(
            "source_size INTEGER NOT NULL",
            "source_size TEXT NOT NULL",
        ).replace(
            "CREATE UNIQUE INDEX camera_events_camera_hash\n"
            "    ON camera_events(camera_id, source_sha256);",
            "CREATE INDEX camera_events_camera_hash\n"
            "    ON camera_events(source_sha256, camera_id);",
        )
        connection = sqlite3.connect(incompatible)
        try:
            connection.executescript(schema)
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(
            RuntimeStateMigrationError,
            "source_size has type TEXT|uniqueness mismatch|columns",
        ):
            RuntimeState(incompatible, backup_dir=self.backups)
        self.assertEqual(len(list(self.backups.glob("*.sqlite3"))), 1)

'''
    replace_once("test_runtime_state.py", marker, addition + marker)

    marker = '''    def test_schema_verification_detects_missing_index(self):
'''
    addition = '''    def test_declared_backup_source_version_must_match_copy(self):
        with self.assertRaisesRegex(
            RuntimeStateBackupError,
            "declared source schema version 0",
        ):
            create_runtime_backup(
                self.database,
                self.backups,
                source_version=0,
            )

    def test_backup_metadata_is_bound_to_schema_and_path(self):
        backup = create_runtime_backup(self.database, self.backups)
        backup_path = Path(backup["path"])
        metadata_path = backup_path.with_suffix(backup_path.suffix + ".json")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["schema"] = "untrusted-format"
        metadata["backup_path"] = str(self.root / "other.sqlite3")
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        report = verify_runtime_backup(backup_path)
        self.assertFalse(report["ok"])
        self.assertTrue(
            any("metadata schema" in error for error in report["errors"])
        )
        self.assertTrue(
            any("path does not match" in error for error in report["errors"])
        )

'''
    replace_once("test_runtime_state.py", marker, addition + marker)


def main():
    patch_runtime_state()
    patch_tests()


if __name__ == "__main__":
    main()
