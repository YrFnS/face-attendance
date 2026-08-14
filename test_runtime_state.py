import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from test_event_ledger import EventLedgerTests
from test_event_operations import EventOperationsTests
from test_processing_recovery import ProcessingRecoveryTests
from runtime_state import (
    MIGRATION_BY_VERSION,
    RUNTIME_SCHEMA_VERSION,
    Migration,
    RuntimeState,
    RuntimeStateBackupError,
    RuntimeStateMigrationError,
    create_runtime_backup,
    inspect_runtime_database,
    make_event_id,
    restore_runtime_backup,
    verify_runtime_backup,
)


LEGACY_SCHEMA = """
CREATE TABLE camera_events (
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
CREATE UNIQUE INDEX camera_events_camera_hash
    ON camera_events(camera_id, source_sha256);
CREATE INDEX camera_events_created
    ON camera_events(created_unix);
CREATE TABLE login_limits (
    limiter_key TEXT PRIMARY KEY,
    window_started REAL NOT NULL,
    failures INTEGER NOT NULL,
    locked_until REAL NOT NULL
);
CREATE TABLE request_rate_limits (
    bucket_key TEXT PRIMARY KEY,
    window_started REAL NOT NULL,
    request_count INTEGER NOT NULL
);
CREATE TABLE admin_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    remote_addr TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
CREATE INDEX admin_audit_created ON admin_audit(id DESC);
"""


class RuntimeStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "state.sqlite3"
        self.backups = self.root / "backups"
        self.state = RuntimeState(self.database, backup_dir=self.backups)

    def tearDown(self):
        self.temp.cleanup()

    def claim(self, *, camera="camera-in", log_type="IN", digest="abc"):
        event_id = make_event_id(camera, log_type, digest)
        return event_id, self.state.claim_event(
            event_id=event_id,
            camera_id=camera,
            log_type=log_type,
            source_sha256=digest,
            source_name="one.jpg",
            source_mtime=1,
            source_size=10,
        )

    def create_legacy_database(self, path):
        path.unlink(missing_ok=True)
        connection = sqlite3.connect(path)
        try:
            connection.executescript(LEGACY_SCHEMA)
            event_id = make_event_id("legacy-camera", "IN", "legacy-hash")
            connection.execute(
                """
                INSERT INTO camera_events (
                    event_id, camera_id, log_type, source_sha256, source_name,
                    source_mtime, source_size, status, created_unix, updated_unix,
                    completed_at, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    "legacy-camera",
                    "IN",
                    "legacy-hash",
                    "legacy.jpg",
                    1.0,
                    123,
                    "processed",
                    1.0,
                    2.0,
                    "2026-08-13T00:00:00Z",
                    "",
                ),
            )
            connection.commit()
            return event_id
        finally:
            connection.close()

    def test_schema_version_and_history_are_initialized(self):
        report = self.state.migration_status()
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["schema_version"], RUNTIME_SCHEMA_VERSION)
        self.assertEqual(report["pending_migrations"], [])
        self.assertEqual(
            report["migration_history"][0]["checksum"],
            MIGRATION_BY_VERSION[1].checksum,
        )
        connection = sqlite3.connect(self.database)
        try:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0],
                RUNTIME_SCHEMA_VERSION,
            )
        finally:
            connection.close()
        self.assertIsNone(self.state.last_migration_backup)

    def test_legacy_database_is_backed_up_migrated_and_preserves_rows(self):
        legacy = self.root / "legacy.sqlite3"
        legacy_event = self.create_legacy_database(legacy)
        state = RuntimeState(legacy, backup_dir=self.backups)
        self.assertIsNotNone(state.last_migration_backup)
        backup_path = Path(state.last_migration_backup["path"])
        self.assertTrue(backup_path.is_file())
        self.assertTrue(verify_runtime_backup(backup_path)["ok"])
        self.assertEqual(
            verify_runtime_backup(backup_path)["schema_version"],
            0,
        )
        self.assertEqual(state.get_event(legacy_event)["status"], "processed")
        self.assertEqual(
            state.migration_status()["schema_version"], RUNTIME_SCHEMA_VERSION
        )

    def test_incompatible_legacy_schema_rolls_back_after_verified_backup(self):
        incompatible = self.root / "incompatible.sqlite3"
        connection = sqlite3.connect(incompatible)
        try:
            connection.execute(
                "CREATE TABLE camera_events (event_id TEXT PRIMARY KEY)"
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(
            RuntimeStateMigrationError, "migration 1 .* failed"
        ):
            RuntimeState(incompatible, backup_dir=self.backups)

        backups = list(self.backups.glob("*.sqlite3"))
        self.assertEqual(len(backups), 1)
        self.assertTrue(verify_runtime_backup(backups[0])["ok"])
        connection = sqlite3.connect(incompatible)
        try:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='schema_migrations'"
                ).fetchone()
            )
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(camera_events)"
                ).fetchall()
            }
            self.assertEqual(columns, {"event_id"})
        finally:
            connection.close()

    def test_concurrent_legacy_initialization_is_safe(self):
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
        self.assertEqual(
            states[0].migration_status()["schema_version"],
            RUNTIME_SCHEMA_VERSION,
        )
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

    def test_current_database_does_not_create_redundant_migration_backup(self):
        before = list(self.backups.glob("*.sqlite3"))
        RuntimeState(self.database, backup_dir=self.backups)
        after = list(self.backups.glob("*.sqlite3"))
        self.assertEqual(before, after)

    def test_tampered_migration_checksum_blocks_startup(self):
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "UPDATE schema_migrations SET checksum = 'tampered' WHERE version = 1"
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(RuntimeStateMigrationError, "checksum mismatch"):
            RuntimeState(self.database, backup_dir=self.backups)

    def test_future_or_inconsistent_schema_version_blocks_startup(self):
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("PRAGMA user_version = 99")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(RuntimeStateMigrationError, "does not match"):
            RuntimeState(self.database, backup_dir=self.backups)

    def test_failed_migration_is_transactional(self):
        bad = Migration(
            RUNTIME_SCHEMA_VERSION + 1,
            "intentional_failure",
            (
                "CREATE TABLE migration_should_rollback (id INTEGER PRIMARY KEY)",
                "THIS IS NOT VALID SQL",
            ),
        )
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                RuntimeState._apply_migration(connection, bad)
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='migration_should_rollback'"
            ).fetchone()
            self.assertIsNone(table)
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0],
                RUNTIME_SCHEMA_VERSION,
            )
        finally:
            connection.close()

    def test_manual_backup_and_atomic_restore_round_trip(self):
        event_id, claim = self.claim()
        self.assertTrue(claim.accepted)
        backup = create_runtime_backup(
            self.database,
            self.backups,
            reason="before-change",
        )
        self.state.finish_event(event_id, status="checkin_created")
        self.assertEqual(self.state.get_event(event_id)["status"], "checkin_created")

        result = restore_runtime_backup(
            self.database,
            backup["path"],
            self.backups,
            confirm=True,
        )
        self.assertTrue(result["ok"])
        self.assertIsNotNone(result["safety_backup"])
        restored = RuntimeState(self.database, backup_dir=self.backups)
        self.assertEqual(restored.get_event(event_id)["status"], "processing")

    def test_restore_requires_explicit_confirmation(self):
        backup = create_runtime_backup(self.database, self.backups)
        with self.assertRaisesRegex(RuntimeStateBackupError, "explicit confirmation"):
            restore_runtime_backup(
                self.database,
                backup["path"],
                self.backups,
            )

    def test_backup_metadata_detects_tampering(self):
        backup = create_runtime_backup(self.database, self.backups)
        backup_path = Path(backup["path"])
        metadata_path = backup_path.with_suffix(backup_path.suffix + ".json")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["sha256"] = "0" * 64
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        report = verify_runtime_backup(backup_path)
        self.assertFalse(report["ok"])
        self.assertTrue(any("SHA-256" in error for error in report["errors"]))

    def test_declared_backup_source_version_must_match_copy(self):
        with self.assertRaisesRegex(
            RuntimeStateBackupError,
            "declared source schema version 0",
        ):
            create_runtime_backup(
                self.database,
                self.backups,
                source_version=0,
            )

    def test_backup_metadata_schema_is_verified_and_backup_is_relocatable(self):
        backup = create_runtime_backup(self.database, self.backups)
        backup_path = Path(backup["path"])
        metadata_path = backup_path.with_suffix(backup_path.suffix + ".json")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["schema"] = "untrusted-format"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        report = verify_runtime_backup(backup_path)
        self.assertFalse(report["ok"])
        self.assertTrue(
            any("metadata schema" in error for error in report["errors"])
        )

        metadata["schema"] = "face-attendance-runtime-backup/v1"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        relocated = self.root / "offsite" / backup_path.name
        relocated.parent.mkdir()
        relocated.write_bytes(backup_path.read_bytes())
        relocated.with_suffix(relocated.suffix + ".json").write_text(
            metadata_path.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        relocated_report = verify_runtime_backup(relocated)
        self.assertTrue(relocated_report["ok"], relocated_report)

    def test_schema_verification_detects_missing_index(self):
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("DROP INDEX camera_events_created")
            connection.commit()
        finally:
            connection.close()
        report = inspect_runtime_database(self.database, require_latest=True)
        self.assertFalse(report["ok"])
        self.assertTrue(any("required indexes" in error for error in report["errors"]))

    def test_duplicate_image_for_same_camera_is_blocked(self):
        event_id = make_event_id("camera-in", "IN", "abc")
        first = self.state.claim_event(
            event_id=event_id,
            camera_id="camera-in",
            log_type="IN",
            source_sha256="abc",
            source_name="one.jpg",
            source_mtime=1,
            source_size=10,
        )
        second = self.state.claim_event(
            event_id=event_id,
            camera_id="camera-in",
            log_type="IN",
            source_sha256="abc",
            source_name="two.jpg",
            source_mtime=2,
            source_size=10,
        )
        self.assertTrue(first.accepted)
        self.assertFalse(second.accepted)
        self.assertEqual(second.reason, "duplicate")
        self.assertEqual(second.existing_status, "processing")

    def test_same_image_from_different_camera_is_distinct(self):
        first_id = make_event_id("camera-in", "IN", "abc")
        second_id = make_event_id("camera-out", "OUT", "abc")
        first = self.state.claim_event(
            event_id=first_id,
            camera_id="camera-in",
            log_type="IN",
            source_sha256="abc",
            source_name="one.jpg",
            source_mtime=1,
            source_size=10,
        )
        second = self.state.claim_event(
            event_id=second_id,
            camera_id="camera-out",
            log_type="OUT",
            source_sha256="abc",
            source_name="two.jpg",
            source_mtime=2,
            source_size=10,
        )
        self.assertTrue(first.accepted)
        self.assertTrue(second.accepted)

    def test_event_status_is_persistent(self):
        event_id, claim = self.claim()
        self.assertTrue(claim.accepted)
        self.state.finish_event(event_id, status="checkin_created")
        self.assertEqual(self.state.get_event(event_id)["status"], "checkin_created")

    def test_login_failures_lock_key(self):
        for _ in range(3):
            self.state.record_login_failure(
                "127.0.0.1:admin",
                max_attempts=3,
                window_seconds=300,
                lockout_seconds=60,
            )
        allowed, retry = self.state.login_allowed("127.0.0.1:admin")
        self.assertFalse(allowed)
        self.assertGreater(retry, 0)
        self.state.clear_login_failures("127.0.0.1:admin")
        self.assertTrue(self.state.login_allowed("127.0.0.1:admin")[0])

    def test_audit_is_recorded(self):
        self.state.audit(
            actor="admin",
            action="embedding_sync",
            remote_addr="127.0.0.1",
            detail={"changed": True},
        )
        rows = self.state.recent_audit()
        self.assertEqual(rows[0]["actor"], "admin")
        self.assertEqual(rows[0]["detail"]["changed"], True)


if __name__ == "__main__":
    unittest.main()
