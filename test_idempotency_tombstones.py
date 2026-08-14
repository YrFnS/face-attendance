import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from event_identity import (
    IDENTITY_CONTRACT_VERSION,
    make_capture_id,
    make_event_id,
)
from idempotency_tombstones import TOMBSTONE_REQUIRED_TRIGGERS
from runtime_state import (
    MIGRATIONS,
    RUNTIME_SCHEMA_VERSION,
    RuntimeState,
    verify_runtime_backup,
)


class IdempotencyTombstoneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "runtime.sqlite3"
        self.backups = self.root / "backups"
        self.state = RuntimeState(
            self.database,
            backup_dir=self.backups,
        )
        self.digest = "a" * 64
        self.event_id = make_event_id(
            "camera-in", "IN", self.digest
        )
        self.capture_id = make_capture_id(
            "camera-in",
            self.digest,
            "capture.jpg",
            123,
            1000.0,
        )

    def tearDown(self):
        self.temp.cleanup()

    def record(self, *, camera="camera-in", direction="IN", digest=None):
        digest = digest or self.digest
        event_id = make_event_id(camera, direction, digest)
        capture_id = make_capture_id(
            camera,
            digest,
            f"{camera}.jpg",
            123,
            1000.0,
        )
        claim = self.state.record_event_receipt(
            event_id=event_id,
            capture_id=capture_id,
            camera_id=camera,
            log_type=direction,
            source_sha256=digest,
            source_name=f"{camera}.jpg",
            source_mtime=1000.0,
            source_size=123,
            received_at="2026-08-10T00:00:00Z",
            effective_at="2026-08-10T00:00:00Z",
            branch="Baghdad",
            source_type="holowits_ftp",
            source_principal=camera,
            source_binding_id="b" * 64,
            policy=direction,
            receipt_state="verified",
            receipt_verified=True,
            receipt_detail={
                "verified": True,
                "signature": "not-retained-in-tombstone",
            },
            policy_version="directional-v1",
        )
        return event_id, capture_id, claim

    def finish_rejected(self, event_id):
        self.state.transition_event(
            event_id,
            to_state="rejected",
            reason_code="unknown_employee",
            event_updates={"retention_state": "not_retained"},
            compatibility_status="rejected",
        )

    def test_prune_creates_minimal_tombstone_and_blocks_replay(self):
        event_id, capture_id, claim = self.record()
        self.assertTrue(claim.accepted)
        self.state.record_recognition_decision(
            event_id=event_id,
            face_index=1,
            face_count=1,
            bbox=[1, 2, 3, 4],
            face_width=2,
            face_height=2,
            detection_score=0.9,
            best_employee="HR-0001",
            accepted=False,
            reason_code="unknown_employee",
            retention_state="not_retained",
        )
        self.finish_rejected(event_id)

        pruned = self.state.prune_events(
            1,
            now=2_000_000_000,
        )
        self.assertEqual(pruned, 1)
        self.assertIsNone(self.state.get_event(event_id))
        tombstone = self.state.idempotency_tombstone(event_id=event_id)
        self.assertEqual(tombstone["event_id"], event_id)
        self.assertEqual(tombstone["capture_id"], capture_id)
        self.assertEqual(tombstone["source_sha256"], self.digest)
        self.assertEqual(
            tombstone["identity_contract_version"],
            IDENTITY_CONTRACT_VERSION,
        )
        self.assertEqual(tombstone["final_state"], "rejected")
        serialized = json.dumps(tombstone, sort_keys=True)
        for forbidden in (
            "HR-0001",
            "source_path",
            "retention_path",
            "receipt_json",
            "signature",
            "best_score",
            "embedding",
        ):
            self.assertNotIn(forbidden, serialized)

        replay_capture = make_capture_id(
            "camera-in",
            self.digest,
            "replayed.jpg",
            123,
            2000.0,
        )
        replay = self.state.record_event_receipt(
            event_id=event_id,
            capture_id=replay_capture,
            camera_id="camera-in",
            log_type="IN",
            source_sha256=self.digest,
            source_name="replayed.jpg",
            source_mtime=2000.0,
            source_size=123,
            received_at="2033-05-18T00:00:00Z",
            effective_at="2033-05-18T00:00:00Z",
            branch="Baghdad",
            source_type="holowits_ftp",
            source_principal="camera-in",
            source_binding_id="b" * 64,
            policy="IN",
            receipt_state="verified",
            receipt_verified=True,
            receipt_detail={"verified": True},
            policy_version="directional-v1",
        )
        self.assertFalse(replay.accepted)
        self.assertEqual(replay.reason, "tombstone")
        self.assertEqual(replay.existing_status, "pruned")
        self.assertEqual(replay.capture_id, capture_id)

    def test_tombstone_scope_allows_other_camera_or_new_content(self):
        first_event, _capture, claim = self.record()
        self.assertTrue(claim.accepted)
        self.finish_rejected(first_event)
        self.assertEqual(
            self.state.prune_events(1, now=2_000_000_000),
            1,
        )

        other_event, _other_capture, other = self.record(
            camera="camera-out",
            direction="OUT",
        )
        self.assertTrue(other.accepted)
        self.assertNotEqual(other_event, first_event)

        new_event, _new_capture, new_content = self.record(
            digest="c" * 64,
        )
        self.assertTrue(new_content.accepted)
        self.assertNotEqual(new_event, first_event)

    def test_normal_prune_preserves_uncertain_quarantined_and_active_work(self):
        uncertain_event, _capture, uncertain = self.record(
            digest="d" * 64,
        )
        self.assertTrue(uncertain.accepted)
        self.state.transition_event(
            uncertain_event,
            to_state="uncertain",
            reason_code="generic_failed",
            event_updates={"retention_state": "retained"},
            compatibility_status="uncertain",
        )

        quarantined_event, _capture, quarantined = self.record(
            digest="e" * 64,
        )
        self.assertTrue(quarantined.accepted)
        self.state.transition_event(
            quarantined_event,
            to_state="rejected",
            reason_code="unknown_employee",
            event_updates={"retention_state": "quarantined"},
            compatibility_status="rejected",
        )

        active_event, _capture, active = self.record(
            digest="f" * 64,
        )
        self.assertTrue(active.accepted)
        lease = self.state.acquire_event_lease(
            active_event,
            owner="worker-a",
            lease_seconds=180,
            now=2_000_000_000,
        )
        self.assertTrue(lease.accepted)

        self.assertEqual(
            self.state.prune_events(1, now=2_000_000_001),
            0,
        )
        for event_id in (
            uncertain_event,
            quarantined_event,
            active_event,
        ):
            self.assertIsNotNone(self.state.get_event(event_id))
            self.assertIsNone(
                self.state.idempotency_tombstone(event_id=event_id)
            )

    def test_schema_guard_blocks_direct_reinsert_and_tombstone_update(self):
        event_id, _capture, claim = self.record()
        self.assertTrue(claim.accepted)
        self.finish_rejected(event_id)
        self.assertEqual(
            self.state.prune_events(1, now=2_000_000_000),
            1,
        )
        connection = sqlite3.connect(self.database)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO camera_events (
                        event_id, camera_id, log_type, source_sha256,
                        source_name, source_mtime, source_size, status,
                        created_unix, updated_unix, error
                    ) VALUES (?, 'camera-in', 'IN', ?, 'replay.jpg',
                              1, 10, 'received', 1, 1, '')
                    """,
                    (event_id, self.digest),
                )
            connection.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    UPDATE event_idempotency_tombstones
                    SET final_state = 'processed'
                    WHERE event_id = ?
                    """,
                    (event_id,),
                )
            connection.rollback()
            triggers = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                ).fetchall()
            }
            self.assertTrue(
                set(TOMBSTONE_REQUIRED_TRIGGERS).issubset(triggers)
            )
        finally:
            connection.close()

    def test_schema_v4_is_backed_up_and_migrated_to_v5(self):
        old_database = self.root / "schema-v4.sqlite3"
        connection = sqlite3.connect(old_database)
        try:
            for migration in MIGRATIONS[:4]:
                connection.execute("BEGIN IMMEDIATE")
                for statement in migration.statements:
                    connection.execute(statement)
                connection.execute(
                    """
                    INSERT INTO schema_migrations (
                        version, name, checksum, applied_at
                    ) VALUES (?, ?, ?, '2026-08-14T00:00:00Z')
                    """,
                    (
                        migration.version,
                        migration.name,
                        migration.checksum,
                    ),
                )
                connection.execute(
                    f"PRAGMA user_version = {migration.version}"
                )
                connection.commit()
            legacy_event = make_event_id(
                "camera-in", "IN", "9" * 64
            )
            connection.execute(
                """
                INSERT INTO camera_events (
                    event_id, camera_id, log_type, source_sha256,
                    source_name, source_mtime, source_size, status,
                    created_unix, updated_unix, error, capture_id,
                    received_at, received_unix, effective_at, policy,
                    lifecycle_state, state_version, reason_code,
                    policy_version, retention_state, processing_phase
                ) VALUES (
                    ?, 'camera-in', 'IN', ?, 'old.jpg', 1, 10,
                    'rejected', 1, 1, '', ?, '2026-08-10T00:00:00Z',
                    1, '2026-08-10T00:00:00Z', 'IN', 'rejected',
                    1, 'unknown_employee', 'directional-v1',
                    'not_retained', 'terminal'
                )
                """,
                (
                    legacy_event,
                    "9" * 64,
                    make_capture_id(
                        "camera-in",
                        "9" * 64,
                        "old.jpg",
                        10,
                        1.0,
                    ),
                ),
            )
            connection.commit()
        finally:
            connection.close()

        migrated = RuntimeState(
            old_database,
            backup_dir=self.backups,
        )
        self.assertEqual(RUNTIME_SCHEMA_VERSION, 5)
        self.assertEqual(
            migrated.migration_status()["schema_version"],
            5,
        )
        self.assertIsNotNone(migrated.last_migration_backup)
        backup = verify_runtime_backup(
            migrated.last_migration_backup["path"]
        )
        self.assertTrue(backup["ok"], backup)
        self.assertEqual(backup["schema_version"], 4)
        self.assertIsNotNone(migrated.get_event(legacy_event))


if __name__ == "__main__":
    unittest.main()
