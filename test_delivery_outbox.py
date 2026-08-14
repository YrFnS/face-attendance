import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from delivery_outbox import (
    DELIVERY_OUTBOX_REQUIRED_INDEXES,
    DELIVERY_OUTBOX_REQUIRED_TRIGGERS,
)
from event_ledger import (
    EventLedgerMixin,
    EventLedgerValidationError,
    make_capture_id,
)
from runtime_state import (
    MIGRATION_BY_VERSION,
    RUNTIME_SCHEMA_VERSION,
    RuntimeState,
    make_event_id,
    utc_now,
    verify_runtime_backup,
)


class SchemaFiveState(EventLedgerMixin):
    def __init__(self, path):
        self.path = Path(path)

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection


class DeliveryOutboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "runtime.sqlite3"
        self.state = RuntimeState(
            self.database,
            backup_dir=self.root / "backups",
        )
        self.digest = "a" * 64
        self.event_id = make_event_id("camera-in", "IN", self.digest)

    def tearDown(self):
        self.temp.cleanup()

    def record_event(
        self,
        *,
        state=None,
        digest=None,
        event_id=None,
        name="capture.jpg",
    ):
        state = state or self.state
        digest = digest or self.digest
        event_id = event_id or make_event_id("camera-in", "IN", digest)
        capture_id = make_capture_id(
            "camera-in",
            digest,
            name,
            123,
            1000.0,
        )
        claim = state.record_event_receipt(
            event_id=event_id,
            capture_id=capture_id,
            camera_id="camera-in",
            log_type="IN",
            source_sha256=digest,
            source_name=name,
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
        return event_id

    def record_decision(
        self,
        *,
        state=None,
        event_id=None,
        accepted=True,
        decision_version=1,
    ):
        state = state or self.state
        event_id = event_id or self.event_id
        return state.record_recognition_decision(
            event_id=event_id,
            face_index=1,
            face_count=1,
            bbox=[1, 2, 30, 40],
            face_width=29,
            face_height=38,
            detection_score=0.99,
            best_employee="HR-0001" if accepted else "",
            best_score=0.91 if accepted else 0.0,
            runner_up_score=0.20,
            score_margin=0.71 if accepted else 0.0,
            pad_passed=accepted,
            pad_skipped=False,
            accepted=accepted,
            reason_code=(
                "accepted_candidate" if accepted else "unknown_employee"
            ),
            candidate_log_type="IN",
            retention_state="not_retained",
            decision_version=decision_version,
        )

    def test_schema_v8_has_durable_delivery_jobs(self):
        self.assertEqual(RUNTIME_SCHEMA_VERSION, 8)
        report = self.state.migration_status()
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["schema_version"], 8)
        connection = sqlite3.connect(self.database)
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            indexes = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                ).fetchall()
            }
            triggers = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                ).fetchall()
            }
        finally:
            connection.close()
        self.assertIn("delivery_jobs", tables)
        self.assertTrue(set(DELIVERY_OUTBOX_REQUIRED_INDEXES).issubset(indexes))
        self.assertTrue(DELIVERY_OUTBOX_REQUIRED_TRIGGERS.issubset(triggers))

    def test_accepted_decision_atomically_creates_pending_job(self):
        self.record_event()
        decision_id = self.record_decision()
        event = self.state.get_event(self.event_id)
        decision = event["decisions"][0]
        job = self.state.delivery_job_for_decision(decision_id)
        self.assertIsNotNone(job)
        self.assertEqual(job["delivery_id"], decision["delivery_id"])
        self.assertEqual(job["event_id"], self.event_id)
        self.assertEqual(job["employee"], "HR-0001")
        self.assertEqual(job["log_type"], "IN")
        self.assertEqual(job["effective_at"], event["effective_at"])
        self.assertEqual(job["camera_id"], "camera-in")
        self.assertEqual(job["branch"], "Baghdad")
        self.assertEqual(job["state"], "pending")
        self.assertEqual(job["attempt_count"], 0)
        self.assertEqual(job["remote_docname"], "")

    def test_rejected_decision_does_not_create_job(self):
        self.record_event()
        decision_id = self.record_decision(accepted=False)
        self.assertIsNone(self.state.delivery_job_for_decision(decision_id))

    def test_delivery_job_insert_failure_rolls_back_decision(self):
        self.record_event()
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                """
                CREATE TRIGGER synthetic_delivery_job_failure
                BEFORE INSERT ON delivery_jobs
                BEGIN
                    SELECT RAISE(ABORT, 'synthetic delivery job failure');
                END
                """
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(
            EventLedgerValidationError,
            "already exists or is invalid",
        ):
            self.record_decision()

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
                    "SELECT COUNT(*) FROM delivery_jobs"
                ).fetchone()[0],
                0,
            )
        finally:
            connection.close()

    def test_delivery_boundary_leases_job_and_records_remote_document(self):
        self.record_event()
        lease = self.state.acquire_event_lease(
            self.event_id,
            owner="watcher-a",
            lease_seconds=60,
            now=1000.0,
        )
        decision_id = self.record_decision(decision_version=lease.attempt)
        self.state.begin_delivery_attempt(
            self.event_id,
            owner="watcher-a",
            decision_id=decision_id,
            lease_seconds=60,
            transport="rest",
            now=1001.0,
        )
        leased = self.state.delivery_job_for_decision(decision_id)
        self.assertEqual(leased["state"], "leased")
        self.assertEqual(leased["transport"], "rest")
        self.assertEqual(leased["attempt_count"], 1)
        self.assertEqual(leased["lease_owner"], "watcher-a")

        delivered = self.state.mark_delivery_job_delivered(
            decision_id=decision_id,
            remote_docname="CHK-0001",
            transport="rest",
            now=1002.0,
        )
        self.assertEqual(delivered["state"], "delivered")
        self.assertEqual(delivered["remote_docname"], "CHK-0001")
        self.assertEqual(delivered["lease_owner"], "")
        self.assertTrue(delivered["delivered_at"])

    def test_expired_delivery_marks_event_and_job_uncertain(self):
        self.record_event()
        lease = self.state.acquire_event_lease(
            self.event_id,
            owner="watcher-a",
            lease_seconds=60,
            now=1000.0,
        )
        decision_id = self.record_decision(decision_version=lease.attempt)
        self.state.begin_delivery_attempt(
            self.event_id,
            owner="watcher-a",
            decision_id=decision_id,
            lease_seconds=60,
            transport="bench",
            now=1001.0,
        )
        outcomes = self.state.recover_expired_event_leases(now=1200.0)
        self.assertEqual([item.outcome for item in outcomes], ["uncertain"])
        self.assertEqual(
            self.state.get_event(self.event_id)["lifecycle_state"],
            "uncertain",
        )
        job = self.state.delivery_job_for_decision(decision_id)
        self.assertEqual(job["state"], "uncertain")
        self.assertEqual(
            job["last_error_class"],
            "delivery_lease_expired",
        )
        self.assertEqual(job["lease_owner"], "")

    def test_delivered_job_survives_detailed_event_pruning(self):
        self.record_event()
        lease = self.state.acquire_event_lease(
            self.event_id,
            owner="watcher-a",
            lease_seconds=60,
            now=1000.0,
        )
        decision_id = self.record_decision(decision_version=lease.attempt)
        self.state.begin_delivery_attempt(
            self.event_id,
            owner="watcher-a",
            decision_id=decision_id,
            lease_seconds=60,
            transport="rest",
            now=1001.0,
        )
        delivered = self.state.mark_delivery_job_delivered(
            decision_id=decision_id,
            remote_docname="CHK-0002",
            transport="rest",
            now=1002.0,
        )
        self.state.finalize_event_with_lease(
            self.event_id,
            owner="watcher-a",
            to_state="checkin_created",
            reason_code="checkin_created",
            compatibility_status="checkin_created",
            now=1003.0,
        )
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "UPDATE camera_events SET created_unix = 0 WHERE event_id = ?",
                (self.event_id,),
            )
            connection.commit()
        finally:
            connection.close()
        with mock.patch("runtime_state.time.time", return_value=2_000_000_000.0):
            self.assertEqual(self.state.prune_events(1), 1)
        self.assertIsNone(self.state.get_event(self.event_id))
        retained = self.state.get_delivery_job(delivered["delivery_id"])
        self.assertEqual(retained["state"], "delivered")
        self.assertEqual(retained["remote_docname"], "CHK-0002")

    def test_pending_job_prevents_detailed_event_pruning(self):
        self.record_event()
        self.record_decision()
        self.state.transition_event(
            self.event_id,
            to_state="processed",
            reason_code="processed_no_checkin",
            compatibility_status="processed_no_checkin",
        )
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "UPDATE camera_events SET created_unix = 0 WHERE event_id = ?",
                (self.event_id,),
            )
            connection.commit()
        finally:
            connection.close()
        with mock.patch("runtime_state.time.time", return_value=2_000_000_000.0):
            self.assertEqual(self.state.prune_events(1), 0)
        self.assertIsNotNone(self.state.get_event(self.event_id))

    def test_queue_summary_and_bounded_listing(self):
        self.record_event()
        self.record_decision()
        summary = self.state.delivery_queue_summary(now=1000.0)
        self.assertEqual(summary["counts"]["pending"], 1)
        self.assertEqual(summary["due"], 1)
        self.assertEqual(summary["total"], 1)
        jobs = self.state.list_delivery_jobs(state="pending", limit=10)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["event_id"], self.event_id)

    def test_schema_v5_delivery_identity_is_backfilled(self):
        previous = self.root / "schema-v5.sqlite3"
        connection = sqlite3.connect(previous)
        connection.row_factory = sqlite3.Row
        try:
            for version in range(1, 6):
                migration = MIGRATION_BY_VERSION[version]
                connection.execute("BEGIN IMMEDIATE")
                for statement in migration.statements:
                    connection.execute(statement)
                connection.execute(
                    """
                    INSERT INTO schema_migrations (
                        version, name, checksum, applied_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        version,
                        migration.name,
                        migration.checksum,
                        utc_now(),
                    ),
                )
                connection.execute(f"PRAGMA user_version = {version}")
                connection.commit()
        finally:
            connection.close()

        v5 = SchemaFiveState(previous)
        event_id = self.record_event(
            state=v5,
            event_id=make_event_id("camera-in", "IN", "c" * 64),
            digest="c" * 64,
            name="schema-five.jpg",
        )
        decision_id = self.record_decision(
            state=v5,
            event_id=event_id,
            accepted=True,
        )
        v5.transition_event(
            event_id,
            to_state="checkin_created",
            reason_code="checkin_created",
            compatibility_status="checkin_created",
        )

        migrated = RuntimeState(
            previous,
            backup_dir=self.root / "v5-backups",
        )
        self.assertEqual(migrated.migration_status()["schema_version"], 8)
        self.assertIsNotNone(migrated.last_migration_backup)
        backup = verify_runtime_backup(migrated.last_migration_backup["path"])
        self.assertTrue(backup["ok"], backup)
        self.assertEqual(backup["schema_version"], 5)
        job = migrated.delivery_job_for_decision(decision_id)
        self.assertEqual(job["state"], "delivered")
        self.assertEqual(job["transport"], "legacy-synchronous")
        self.assertEqual(job["remote_docname"], "")


if __name__ == "__main__":
    unittest.main()
