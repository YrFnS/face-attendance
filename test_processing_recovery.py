import sqlite3
import tempfile
import unittest
from pathlib import Path

from event_ledger import (
    TERMINAL_EVENT_STATES,
    make_capture_id,
    make_recognition_decision_id,
)
from processing_recovery import (
    RECOVERY_REQUIRED_INDEXES,
    ProcessingLeaseError,
    attendance_policy_scope_key,
    configuration_issues,
)
from runtime_state import RUNTIME_SCHEMA_VERSION, RuntimeState, make_event_id


class ProcessingRecoveryTests(unittest.TestCase):
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

    def record_event(self, *, event_id=None, direction="IN", digest=None):
        event_id = event_id or self.event_id
        digest = digest or self.digest
        capture_id = make_capture_id(
            "camera-in", digest, f"{event_id[:8]}.jpg", 123, 1000.0
        )
        return self.state.record_event_receipt(
            event_id=event_id,
            capture_id=capture_id,
            camera_id="camera-in",
            log_type=direction,
            source_sha256=digest,
            source_name=f"{event_id[:8]}.jpg",
            source_mtime=1000.0,
            source_size=123,
            received_at="2026-08-14T00:00:00Z",
            effective_at="2026-08-14T00:00:00Z",
            branch="Baghdad",
            source_type="holowits_ftp",
            source_principal="camera_in",
            source_binding_id="b" * 64,
            policy=direction,
            source_at="2026-08-13T23:59:59Z",
            source_time_provenance="test",
            receipt_state="verified",
            receipt_verified=True,
            receipt_detail={"verified": True},
            policy_version="directional-v1",
        )

    def test_schema_v3_has_processing_and_policy_state(self):
        report = self.state.migration_status()
        self.assertGreaterEqual(RUNTIME_SCHEMA_VERSION, 3)
        self.assertEqual(report["schema_version"], RUNTIME_SCHEMA_VERSION)
        connection = sqlite3.connect(self.database)
        try:
            event_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(camera_events)"
                ).fetchall()
            }
            policy_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(attendance_policy_state)"
                ).fetchall()
            }
            indexes = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                ).fetchall()
            }
        finally:
            connection.close()
        self.assertTrue(
            {
                "processing_attempt",
                "lease_owner",
                "lease_expires_unix",
                "processing_phase",
                "delivery_started_at",
                "delivery_decision_id",
            }.issubset(event_columns)
        )
        self.assertIn("reservation_state", policy_columns)
        self.assertTrue(set(RECOVERY_REQUIRED_INDEXES).issubset(indexes))

    def test_active_lease_blocks_second_worker_and_expired_work_recovers(self):
        self.record_event()
        first = self.state.acquire_event_lease(
            self.event_id,
            owner="worker-a",
            lease_seconds=60,
            now=1000.0,
        )
        self.assertTrue(first.accepted)
        self.assertEqual(first.attempt, 1)
        blocked = self.state.acquire_event_lease(
            self.event_id,
            owner="worker-b",
            lease_seconds=60,
            now=1020.0,
        )
        self.assertFalse(blocked.accepted)
        self.assertEqual(blocked.reason, "active_lease")

        outcomes = self.state.recover_expired_event_leases(now=1100.0)
        self.assertEqual([item.outcome for item in outcomes], ["retry"])
        recovered = self.state.acquire_event_lease(
            self.event_id,
            owner="worker-b",
            lease_seconds=60,
            now=1101.0,
        )
        self.assertTrue(recovered.accepted)
        self.assertTrue(recovered.recovered)
        self.assertEqual(recovered.attempt, 2)
        event = self.state.get_event(self.event_id)
        self.assertEqual(event["lease_owner"], "worker-b")
        self.assertGreaterEqual(event["recovery_count"], 1)

    def test_delivery_boundary_becomes_uncertain_after_expired_lease(self):
        self.record_event()
        lease = self.state.acquire_event_lease(
            self.event_id,
            owner="worker-a",
            lease_seconds=60,
            now=1000.0,
        )
        decision_id = make_recognition_decision_id(
            self.event_id, 1, lease.attempt
        )
        reservation = self.state.reserve_attendance_policy(
            employee="HR-1",
            direction="IN",
            branch="Baghdad",
            policy_version="directional-v1",
            event_id=self.event_id,
            decision_id=decision_id,
            effective_at="2026-08-14T00:00:00Z",
            cooldown_seconds=600,
            reservation_seconds=120,
            now=1001.0,
        )
        self.assertTrue(reservation.accepted)
        self.state.begin_delivery_attempt(
            self.event_id,
            owner="worker-a",
            decision_id=decision_id,
            lease_seconds=60,
            now=1002.0,
        )

        outcomes = self.state.recover_expired_event_leases(now=1200.0)
        self.assertEqual([item.outcome for item in outcomes], ["uncertain"])
        event = self.state.get_event(self.event_id)
        self.assertEqual(event["lifecycle_state"], "uncertain")
        self.assertEqual(event["processing_phase"], "terminal")
        policy = self.state.attendance_policy_state(reservation.scope_key)
        self.assertEqual(policy["reservation_state"], "uncertain")

        another_event = "c" * 64
        another_decision = "d" * 64
        blocked = self.state.reserve_attendance_policy(
            employee="HR-1",
            direction="IN",
            branch="Baghdad",
            policy_version="directional-v1",
            event_id=another_event,
            decision_id=another_decision,
            effective_at="2026-08-14T00:20:00Z",
            cooldown_seconds=600,
            reservation_seconds=120,
            now=1201.0,
        )
        self.assertFalse(blocked.accepted)
        self.assertEqual(blocked.reason, "uncertain_reservation")

    def test_cooldown_is_scoped_by_direction_branch_and_policy(self):
        self.record_event()
        decision_id = make_recognition_decision_id(self.event_id, 1, 1)
        first = self.state.reserve_attendance_policy(
            employee="HR-1",
            direction="IN",
            branch="Baghdad",
            policy_version="directional-v1",
            event_id=self.event_id,
            decision_id=decision_id,
            effective_at="2026-08-14T00:00:00Z",
            cooldown_seconds=600,
            reservation_seconds=120,
            now=1000.0,
        )
        self.assertTrue(first.accepted)
        self.state.commit_attendance_policy_reservation(
            scope_key=first.scope_key,
            event_id=self.event_id,
            decision_id=decision_id,
        )

        second_event = "c" * 64
        second_decision = "d" * 64
        same_direction = self.state.reserve_attendance_policy(
            employee="HR-1",
            direction="IN",
            branch="Baghdad",
            policy_version="directional-v1",
            event_id=second_event,
            decision_id=second_decision,
            effective_at="2026-08-14T00:05:00Z",
            cooldown_seconds=600,
            reservation_seconds=120,
            now=1300.0,
        )
        self.assertFalse(same_direction.accepted)
        self.assertEqual(same_direction.reason, "cooldown")

        opposite_event = "e" * 64
        opposite_decision = "f" * 64
        opposite_direction = self.state.reserve_attendance_policy(
            employee="HR-1",
            direction="OUT",
            branch="Baghdad",
            policy_version="directional-v1",
            event_id=opposite_event,
            decision_id=opposite_decision,
            effective_at="2026-08-14T00:05:00Z",
            cooldown_seconds=600,
            reservation_seconds=120,
            now=1300.0,
        )
        self.assertTrue(opposite_direction.accepted)
        self.assertNotEqual(first.scope_key, opposite_direction.scope_key)
        self.assertEqual(
            first.scope_key,
            attendance_policy_scope_key(
                "HR-1", "IN", "Baghdad", "directional-v1"
            ),
        )

    def test_pre_delivery_recovery_releases_pending_policy_reservation(self):
        self.record_event()
        lease = self.state.acquire_event_lease(
            self.event_id,
            owner="worker-a",
            lease_seconds=60,
            now=1000.0,
        )
        decision_id = make_recognition_decision_id(
            self.event_id, 1, lease.attempt
        )
        reservation = self.state.reserve_attendance_policy(
            employee="HR-1",
            direction="IN",
            branch="Baghdad",
            policy_version="directional-v1",
            event_id=self.event_id,
            decision_id=decision_id,
            effective_at="2026-08-14T00:00:00Z",
            cooldown_seconds=600,
            reservation_seconds=120,
            now=1001.0,
        )
        self.assertTrue(reservation.accepted)
        outcomes = self.state.recover_expired_event_leases(now=1100.0)
        self.assertEqual(outcomes[0].outcome, "retry")
        policy = self.state.attendance_policy_state(reservation.scope_key)
        self.assertEqual(policy["reservation_state"], "none")
        self.assertEqual(policy["reservation_event_id"], "")

    def test_compatibility_finish_cannot_finalize_reacquired_event(self):
        event_id = make_event_id("camera-in", "IN", "compatibility-digest")
        claim = self.state.claim_event(
            event_id=event_id,
            camera_id="camera-in",
            log_type="IN",
            source_sha256="compatibility-digest",
            source_name="compatibility.jpg",
            source_mtime=1.0,
            source_size=10,
        )
        self.assertTrue(claim.accepted)
        event = self.state.get_event(event_id)
        takeover = self.state.acquire_event_lease(
            event_id,
            owner="replacement-worker",
            lease_seconds=60,
            now=float(event["lease_expires_unix"]) + 1,
        )
        self.assertTrue(takeover.accepted)
        with self.assertRaisesRegex(
            ProcessingLeaseError, "current unexpired processing lease"
        ):
            self.state.finish_event(event_id, status="processed")
        current = self.state.get_event(event_id)
        self.assertEqual(current["lease_owner"], "replacement-worker")
        self.assertNotIn(current["lifecycle_state"], TERMINAL_EVENT_STATES)

    def test_filesystem_cooldown_lock_is_removed(self):
        source = (Path(__file__).resolve().parent / "face_attendance.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("cooldown_state.lock", source)
        self.assertNotIn("acquire_cooldown_lock", source)
        self.assertNotIn("save_cooldown_state", source)

    def test_configuration_validation_is_fail_closed(self):
        self.assertEqual(configuration_issues({}), [])
        issues = configuration_issues(
            {
                "production_mode": True,
                "event_processing_lease_seconds": 10,
                "attendance_policy_reservation_seconds": 5,
                "event_startup_recovery_enabled": False,
                "cooldown_seconds": "600",
            }
        )
        self.assertTrue(any("event_processing_lease_seconds" in item for item in issues))
        self.assertTrue(any("cooldown_seconds" in item for item in issues))
        self.assertTrue(any("must be true in production" in item for item in issues))

    def test_stale_worker_cannot_finalize_after_lease_reacquisition(self):
        self.record_event()
        first = self.state.acquire_event_lease(
            self.event_id,
            owner="worker-a",
            lease_seconds=30,
            now=1000.0,
        )
        self.assertTrue(first.accepted)
        second = self.state.acquire_event_lease(
            self.event_id,
            owner="worker-b",
            lease_seconds=60,
            now=1100.0,
        )
        self.assertTrue(second.accepted)
        with self.assertRaisesRegex(ProcessingLeaseError, "current unexpired"):
            self.state.finalize_event_with_lease(
                self.event_id,
                owner="worker-a",
                to_state="processed",
                reason_code="processed_no_checkin",
                compatibility_status="processed_no_checkin",
                now=1101.0,
            )
        event = self.state.get_event(self.event_id)
        self.assertEqual(event["lease_owner"], "worker-b")
        self.assertNotIn(event["lifecycle_state"], TERMINAL_EVENT_STATES)
        self.state.finalize_event_with_lease(
            self.event_id,
            owner="worker-b",
            to_state="processed",
            reason_code="processed_no_checkin",
            compatibility_status="processed_no_checkin",
            now=1102.0,
        )
        self.assertEqual(
            self.state.get_event(self.event_id)["lifecycle_state"], "processed"
        )

    def test_expired_delivery_reservation_cannot_be_overwritten(self):
        self.record_event()
        lease = self.state.acquire_event_lease(
            self.event_id,
            owner="worker-a",
            lease_seconds=60,
            now=1000.0,
        )
        decision_id = make_recognition_decision_id(
            self.event_id, 1, lease.attempt
        )
        reservation = self.state.reserve_attendance_policy(
            employee="HR-1",
            direction="IN",
            branch="Baghdad",
            policy_version="directional-v1",
            event_id=self.event_id,
            decision_id=decision_id,
            effective_at="2026-08-14T00:00:00Z",
            cooldown_seconds=600,
            reservation_seconds=30,
            now=1001.0,
        )
        self.assertTrue(reservation.accepted)
        self.state.begin_delivery_attempt(
            self.event_id,
            owner="worker-a",
            decision_id=decision_id,
            lease_seconds=60,
            now=1002.0,
        )
        blocked = self.state.reserve_attendance_policy(
            employee="HR-1",
            direction="IN",
            branch="Baghdad",
            policy_version="directional-v1",
            event_id="c" * 64,
            decision_id="d" * 64,
            effective_at="2026-08-14T00:20:00Z",
            cooldown_seconds=600,
            reservation_seconds=30,
            now=1040.0,
        )
        self.assertFalse(blocked.accepted)
        self.assertEqual(blocked.reason, "uncertain_reservation")
        policy = self.state.attendance_policy_state(reservation.scope_key)
        self.assertEqual(policy["reservation_state"], "uncertain")


if __name__ == "__main__":
    unittest.main()
