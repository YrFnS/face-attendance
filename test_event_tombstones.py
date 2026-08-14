import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from event_identity import (
    CAPTURE_ID_SCHEME,
    CONTENT_HASH_ALGORITHM,
    EVENT_ID_SCHEME,
    make_capture_id,
    make_event_id,
)
from event_ledger import EventLedgerValidationError
from runtime_state import RuntimeState, RuntimeStateError


class EventTombstoneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "runtime.sqlite3"
        self.state = RuntimeState(self.database, backup_dir=self.root / "backups")
        self.digest = "a" * 64
        self.event_id = make_event_id("camera-in", "IN", self.digest)
        self.capture_id = make_capture_id(
            "camera-in", self.digest, "capture.jpg", 123, 1000.0
        )

    def tearDown(self):
        self.temp.cleanup()

    def record_event(self, *, retention_state="not_retained"):
        claim = self.state.record_event_receipt(
            event_id=self.event_id,
            capture_id=self.capture_id,
            camera_id="camera-in",
            log_type="IN",
            source_sha256=self.digest,
            source_name="capture.jpg",
            source_mtime=1000.0,
            source_size=123,
            received_at="2026-08-14T00:00:00Z",
            effective_at="2026-08-14T00:00:00Z",
            branch="Baghdad",
            source_type="holowits_ftp",
            source_principal="camera_in",
            source_binding_id="b" * 64,
            policy="IN",
            receipt_state="verified",
            receipt_verified=True,
            receipt_detail={"verified": True},
            policy_version="directional-v1",
        )
        self.assertTrue(claim.accepted)
        self.state.record_recognition_decision(
            event_id=self.event_id,
            face_index=1,
            face_count=1,
            bbox=[1, 2, 3, 4],
            face_width=2,
            face_height=2,
            detection_score=0.9,
            accepted=False,
            reason_code="unknown_employee",
            retention_state=retention_state,
        )
        self.state.transition_event(
            self.event_id,
            to_state="processed",
            reason_code="processed_no_checkin",
            event_updates={"retention_state": retention_state},
            compatibility_status="processed_no_checkin",
        )
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "UPDATE camera_events SET created_unix = 0, received_unix = 0, "
                "received_at = '1970-01-01T00:00:00Z' WHERE event_id = ?",
                (self.event_id,),
            )
            connection.commit()
        finally:
            connection.close()

    def replay_claim(self):
        return self.state.record_event_receipt(
            event_id=self.event_id,
            capture_id=self.capture_id,
            camera_id="camera-in",
            log_type="IN",
            source_sha256=self.digest,
            source_name="capture.jpg",
            source_mtime=1000.0,
            source_size=123,
            received_at="2026-08-14T00:00:00Z",
            effective_at="2026-08-14T00:00:00Z",
            branch="Baghdad",
            source_type="holowits_ftp",
            source_principal="camera_in",
            source_binding_id="b" * 64,
            policy="IN",
            receipt_state="verified",
            receipt_verified=True,
            receipt_detail={"verified": True},
            policy_version="directional-v1",
        )

    def test_receipt_registers_minimal_tombstone_atomically(self):
        self.record_event()
        tombstone = self.state.get_event_tombstone(self.event_id)
        self.assertEqual(tombstone["event_id"], self.event_id)
        self.assertEqual(tombstone["event_id_scheme"], EVENT_ID_SCHEME)
        self.assertEqual(tombstone["capture_id"], self.capture_id)
        self.assertEqual(tombstone["capture_id_scheme"], CAPTURE_ID_SCHEME)
        self.assertEqual(tombstone["source_sha256"], self.digest)
        self.assertEqual(
            tombstone["content_hash_algorithm"], CONTENT_HASH_ALGORITHM
        )
        self.assertNotIn("best_employee", tombstone)
        self.assertNotIn("receipt_json", tombstone)
        self.assertNotIn("source_name", tombstone)

        replay = self.replay_claim()
        self.assertFalse(replay.accepted)
        self.assertEqual(replay.reason, "duplicate")
        self.assertEqual(replay.existing_status, "processed_no_checkin")

    def test_receipt_and_tombstone_roll_back_together(self):
        digest = "c" * 64
        event_id = make_event_id("camera-in", "IN", digest)
        capture_id = make_capture_id(
            "camera-in", digest, "failed.jpg", 55, 1001.0
        )
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                """
                CREATE TRIGGER fail_camera_event_insert
                BEFORE INSERT ON camera_events
                BEGIN
                    SELECT RAISE(ABORT, 'synthetic event insert failure');
                END
                """
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(sqlite3.IntegrityError):
            self.state.record_event_receipt(
                event_id=event_id,
                capture_id=capture_id,
                camera_id="camera-in",
                log_type="IN",
                source_sha256=digest,
                source_name="failed.jpg",
                source_mtime=1001.0,
                source_size=55,
                received_at="2026-08-14T00:00:01Z",
                effective_at="2026-08-14T00:00:01Z",
                policy="IN",
                receipt_state="verified",
                receipt_verified=True,
                receipt_detail={},
            )
        connection = sqlite3.connect(self.database)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM event_tombstones WHERE event_id = ?",
                    (event_id,),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM camera_events WHERE event_id = ?",
                    (event_id,),
                ).fetchone()[0],
                0,
            )
        finally:
            connection.close()

    def test_prune_removes_details_but_tombstone_blocks_replay(self):
        self.record_event()
        with mock.patch("runtime_state.time.time", return_value=2000000000.0):
            self.assertEqual(self.state.prune_events(1), 1)
        self.assertIsNone(self.state.get_event(self.event_id))
        self.assertIsNotNone(self.state.get_event_tombstone(self.event_id))

        replay = self.replay_claim()
        self.assertFalse(replay.accepted)
        self.assertEqual(replay.reason, "tombstoned")
        self.assertEqual(replay.existing_status, "tombstoned")
        self.assertEqual(replay.event_id, self.event_id)
        self.assertEqual(replay.capture_id, self.capture_id)

        connection = sqlite3.connect(self.database)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM recognition_decisions"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM event_transitions"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM event_tombstones"
                ).fetchone()[0],
                1,
            )
        finally:
            connection.close()

    def test_same_camera_content_stays_blocked_after_direction_change(self):
        self.record_event()
        with mock.patch("runtime_state.time.time", return_value=2000000000.0):
            self.assertEqual(self.state.prune_events(1), 1)
        changed_event_id = make_event_id("camera-in", "OUT", self.digest)
        claim = self.state.record_event_receipt(
            event_id=changed_event_id,
            capture_id=self.capture_id,
            camera_id="camera-in",
            log_type="OUT",
            source_sha256=self.digest,
            source_name="capture.jpg",
            source_mtime=1000.0,
            source_size=123,
            received_at="2026-08-15T00:00:00Z",
            effective_at="2026-08-15T00:00:00Z",
            policy="OUT",
            receipt_state="verified",
            receipt_verified=True,
            receipt_detail={},
        )
        self.assertFalse(claim.accepted)
        self.assertEqual(claim.reason, "tombstoned")
        self.assertEqual(claim.event_id, self.event_id)
        self.assertNotEqual(changed_event_id, self.event_id)

    def test_pruning_refuses_missing_or_conflicting_tombstone(self):
        self.record_event()
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("DROP TRIGGER event_tombstones_no_delete")
            connection.execute(
                "DELETE FROM event_tombstones WHERE event_id = ?",
                (self.event_id,),
            )
            connection.commit()
        finally:
            connection.close()
        with mock.patch("runtime_state.time.time", return_value=2000000000.0):
            with self.assertRaisesRegex(RuntimeStateError, "tombstone"):
                self.state.prune_events(1)
        self.assertIsNotNone(self.state.get_event(self.event_id))

    def test_normal_pruning_never_removes_existing_tombstones(self):
        self.record_event()
        with mock.patch("runtime_state.time.time", return_value=2000000000.0):
            self.assertEqual(self.state.prune_events(1), 1)
            self.assertEqual(self.state.prune_events(1), 0)
        self.assertIsNotNone(self.state.get_event_tombstone(self.event_id))

    def test_tombstones_are_immutable_and_permanent_after_detail_pruning(self):
        self.record_event()
        connection = sqlite3.connect(self.database)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE event_tombstones SET camera_id = 'changed'"
                )
            connection.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM event_tombstones WHERE event_id = ?",
                    (self.event_id,),
                )
            connection.rollback()
            connection.execute(
                "UPDATE camera_events SET created_unix = 0 WHERE event_id = ?",
                (self.event_id,),
            )
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(self.state.prune_events(1), 1)
        connection = sqlite3.connect(self.database)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM event_tombstones WHERE event_id = ?",
                    (self.event_id,),
                )
            connection.rollback()
        finally:
            connection.close()

    def test_identifier_collision_fails_closed(self):
        expected_event = make_event_id("camera-in", "IN", self.digest)
        expected_capture = make_capture_id(
            "camera-in", self.digest, "capture.jpg", 123, 1000.0
        )
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                """
                INSERT INTO event_tombstones (
                    event_id, event_id_scheme, capture_id, capture_id_scheme,
                    camera_id, log_type, source_sha256, content_hash_algorithm,
                    first_received_at, first_received_unix
                ) VALUES (?, ?, ?, ?, ?, 'IN', ?, ?, ?, ?)
                """,
                (
                    expected_event,
                    EVENT_ID_SCHEME,
                    "f" * 64,
                    CAPTURE_ID_SCHEME,
                    "different-camera",
                    "e" * 64,
                    CONTENT_HASH_ALGORITHM,
                    "2026-08-14T00:00:00Z",
                    1786665600.0,
                ),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(EventLedgerValidationError, "collision"):
            self.state.record_event_receipt(
                event_id=expected_event,
                capture_id=expected_capture,
                camera_id="camera-in",
                log_type="IN",
                source_sha256=self.digest,
                source_name="capture.jpg",
                source_mtime=1000.0,
                source_size=123,
                received_at="2026-08-14T00:00:00Z",
                effective_at="2026-08-14T00:00:00Z",
                policy="IN",
                receipt_state="verified",
                receipt_verified=True,
                receipt_detail={},
            )

    def test_quarantined_and_uncertain_events_are_not_pruned(self):
        self.record_event(retention_state="quarantined")
        with mock.patch("runtime_state.time.time", return_value=2000000000.0):
            self.assertEqual(self.state.prune_events(1), 0)
        self.assertIsNotNone(self.state.get_event(self.event_id))
        self.assertIsNotNone(self.state.get_event_tombstone(self.event_id))

        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "UPDATE camera_events SET lifecycle_state = 'uncertain', "
                "status = 'uncertain', retention_state = 'not_retained' "
                "WHERE event_id = ?",
                (self.event_id,),
            )
            connection.commit()
        finally:
            connection.close()
        with mock.patch("runtime_state.time.time", return_value=2000000000.0):
            self.assertEqual(self.state.prune_events(1), 0)
        self.assertIsNotNone(self.state.get_event(self.event_id))


if __name__ == "__main__":
    unittest.main()
