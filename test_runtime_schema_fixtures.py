import base64
import gzip
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from event_identity import (
    LEGACY_CAPTURE_ID_SCHEME,
    LEGACY_CONTENT_HASH_ALGORITHM,
    LEGACY_DECISION_ID_SCHEME,
    LEGACY_EVENT_ID_SCHEME,
    make_capture_id,
    make_event_id,
)
from runtime_state import (
    RUNTIME_SCHEMA_VERSION,
    RuntimeState,
    verify_runtime_backup,
)


FIXTURE_ROOT = Path(__file__).resolve().parent / "tests" / "fixtures"


class ReleasedSchemaFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(
            (FIXTURE_ROOT / "manifest.json").read_text(encoding="utf-8")
        )

    def materialize(self, info, destination):
        encoded = (FIXTURE_ROOT / info["name"]).read_bytes()
        compressed = base64.b64decode(b"".join(encoded.split()), validate=True)
        self.assertEqual(
            hashlib.sha256(compressed).hexdigest(),
            info["gzip_sha256"],
        )
        raw = gzip.decompress(compressed)
        self.assertEqual(len(raw), info["raw_size"])
        self.assertEqual(hashlib.sha256(raw).hexdigest(), info["raw_sha256"])
        destination.write_bytes(raw)

    def test_every_frozen_released_schema_migrates_and_remains_readable(self):
        self.assertEqual(self.manifest["fixture_format"], 1)
        self.assertEqual(
            [item["schema_version"] for item in self.manifest["fixtures"]],
            [1, 2, 3, 4],
        )
        for info in self.manifest["fixtures"]:
            with self.subTest(schema_version=info["schema_version"]):
                with tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    database = root / "runtime.sqlite3"
                    self.materialize(info, database)
                    connection = sqlite3.connect(database)
                    try:
                        self.assertEqual(
                            connection.execute("PRAGMA user_version").fetchone()[0],
                            info["schema_version"],
                        )
                        migration = connection.execute(
                            "SELECT checksum FROM schema_migrations "
                            "WHERE version = ?",
                            (info["schema_version"],),
                        ).fetchone()
                        self.assertEqual(
                            migration[0], info["migration_checksum"]
                        )
                    finally:
                        connection.close()

                    state = RuntimeState(database, backup_dir=root / "backups")
                    report = state.migration_status()
                    self.assertTrue(report["ok"], report)
                    self.assertEqual(report["schema_version"], 8)
                    self.assertEqual(RUNTIME_SCHEMA_VERSION, 8)
                    self.assertIsNotNone(state.last_migration_backup)
                    backup_report = verify_runtime_backup(
                        state.last_migration_backup["path"]
                    )
                    self.assertTrue(backup_report["ok"], backup_report)
                    self.assertEqual(
                        backup_report["schema_version"], info["schema_version"]
                    )

                    event = state.get_event(self.manifest["event_id"])
                    self.assertIsNotNone(event)
                    self.assertEqual(event["camera_id"], "camera-in")
                    self.assertEqual(event["source_name"], "released.jpg")
                    self.assertEqual(event["source_sha256"], "a" * 64)
                    self.assertEqual(
                        event["event_id_scheme"], LEGACY_EVENT_ID_SCHEME
                    )
                    self.assertEqual(
                        event["capture_id_scheme"], LEGACY_CAPTURE_ID_SCHEME
                    )
                    self.assertEqual(
                        event["content_hash_algorithm"],
                        LEGACY_CONTENT_HASH_ALGORITHM,
                    )

                    tombstone = state.get_event_tombstone(
                        self.manifest["event_id"]
                    )
                    self.assertIsNotNone(tombstone)
                    self.assertEqual(
                        tombstone["event_id_scheme"], LEGACY_EVENT_ID_SCHEME
                    )
                    self.assertEqual(
                        tombstone["capture_id_scheme"],
                        LEGACY_CAPTURE_ID_SCHEME,
                    )
                    self.assertEqual(
                        tombstone["content_hash_algorithm"],
                        LEGACY_CONTENT_HASH_ALGORITHM,
                    )
                    self.assertNotIn("best_employee", tombstone)
                    self.assertNotIn("receipt_json", tombstone)

                    connection = sqlite3.connect(database)
                    connection.row_factory = sqlite3.Row
                    try:
                        self.assertEqual(
                            connection.execute(
                                "SELECT COUNT(*) FROM event_tombstones"
                            ).fetchone()[0],
                            1,
                        )
                        if info["schema_version"] >= 2:
                            decision = dict(
                                connection.execute(
                                    "SELECT * FROM recognition_decisions"
                                ).fetchone()
                            )
                            self.assertEqual(
                                decision["best_employee"], "HR-0001"
                            )
                            self.assertEqual(
                                decision["decision_id_scheme"],
                                LEGACY_DECISION_ID_SCHEME,
                            )
                            self.assertEqual(decision["delivery_id"], "")
                            self.assertEqual(
                                decision["delivery_contract_version"], ""
                            )
                            self.assertEqual(
                                connection.execute(
                                    "SELECT COUNT(*) FROM operator_actions"
                                ).fetchone()[0],
                                1,
                            )
                        if info["schema_version"] >= 3:
                            policy = dict(
                                connection.execute(
                                    "SELECT * FROM attendance_policy_state"
                                ).fetchone()
                            )
                            self.assertEqual(policy["employee"], "HR-0001")
                            self.assertEqual(policy["direction"], "IN")
                            self.assertEqual(event["processing_attempt"], 2)
                        if info["schema_version"] >= 4:
                            self.assertEqual(
                                event["source_path"],
                                "/var/lib/face-attendance/released.jpg",
                            )
                            self.assertEqual(
                                event["retention_path"],
                                "/var/lib/face-attendance/archive/released.jpg",
                            )
                            self.assertEqual(event["operator_revision"], 2)
                    finally:
                        connection.close()

    def test_legacy_tombstone_blocks_current_scheme_replay_after_prune(self):
        info = self.manifest["fixtures"][-1]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = root / "runtime.sqlite3"
            self.materialize(info, database)
            state = RuntimeState(database, backup_dir=root / "backups")

            with mock.patch("runtime_state.time.time", return_value=2000000000.0):
                self.assertEqual(state.prune_events(1), 1)
            self.assertIsNone(state.get_event(self.manifest["event_id"]))

            digest = "a" * 64
            current_event_id = make_event_id("camera-in", "IN", digest)
            current_capture_id = make_capture_id(
                "camera-in", digest, "released.jpg", 321, 1700000000.0
            )
            claim = state.record_event_receipt(
                event_id=current_event_id,
                capture_id=current_capture_id,
                camera_id="camera-in",
                log_type="IN",
                source_sha256=digest,
                source_name="released.jpg",
                source_mtime=1700000000.0,
                source_size=321,
                received_at="2026-08-14T00:00:00Z",
                effective_at="2026-08-14T00:00:00Z",
                policy="IN",
                receipt_state="verified",
                receipt_verified=True,
                receipt_detail={},
            )
            self.assertFalse(claim.accepted)
            self.assertEqual(claim.reason, "tombstoned")
            self.assertEqual(claim.event_id, self.manifest["event_id"])
            self.assertNotEqual(claim.event_id, current_event_id)


if __name__ == "__main__":
    unittest.main()
