import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

from attachment_outbox import (
    ATTACHMENT_OUTBOX_REQUIRED_INDEXES,
    ATTACHMENT_OUTBOX_REQUIRED_TRIGGERS,
    make_attachment_id,
)
from attachment_service import AttachmentWorker, AttachmentWorkerSettings
from attachment_spool import spool_private_crop
from erpnext_adapter import (
    ERPNextAdapterConfigurationError,
    EmployeeCheckinResult,
    PrivateAttachmentResult,
)
from delivery_outbox import DeliveryOutboxMixin
from event_ledger import EventLedgerMixin, EventLedgerValidationError, make_capture_id
from runtime_state import (
    MIGRATION_BY_VERSION,
    RUNTIME_SCHEMA_VERSION,
    RuntimeState,
    make_event_id,
    utc_now,
    verify_runtime_backup,
)


class SchemaSevenState(DeliveryOutboxMixin, EventLedgerMixin):
    def __init__(self, path):
        self.path = Path(path)

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection


class FakeAttachmentAdapter:
    transport = "rest"

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.attachment_calls = []

    def create_employee_checkin(self, request, image_path=None):
        return EmployeeCheckinResult("CHK-0001", self.transport)

    def attach_private_file(self, docname, image_path):
        self.attachment_calls.append((docname, Path(image_path)))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class AttachmentOutboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = RuntimeState(
            self.root / "runtime.sqlite3",
            backup_dir=self.root / "backups",
        )
        self.digest = "a" * 64
        self.event_id = make_event_id("camera-in", "IN", self.digest)
        claim = self.state.record_event_receipt(
            event_id=self.event_id,
            capture_id=make_capture_id(
                "camera-in", self.digest, "capture.jpg", 123, 1000.0
            ),
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
        self.crop = self.root / "crop.jpg"
        self.crop.write_bytes(b"\xff\xd8crop-bytes\xff\xd9")
        self.cfg = {
            "delivery_mode": "worker",
            "attach_checkin_crop": True,
            "attachment_worker_enabled": True,
            "attachment_spool_dir": str(self.root / "attachment-spool"),
            "attachment_max_image_bytes": 1024 * 1024,
            "attachment_delete_spool_after_success": True,
            "attachment_orphan_grace_seconds": 3600,
        }
        self.settings = AttachmentWorkerSettings(
            enabled=True,
            batch_size=10,
            lease_seconds=60,
            heartbeat_seconds=10,
            max_attempts=3,
            retry_base_seconds=5,
            retry_max_seconds=60,
            retry_jitter_fraction=0,
            max_image_bytes=1024 * 1024,
            queue_max_active_jobs=100,
            queue_min_free_bytes=0,
            orphan_grace_seconds=3600,
        )

    def tearDown(self):
        self.temp.cleanup()

    def record_decision(self, *, attachment=True):
        # decision version 1 produces a deterministic decision ID.
        from event_ledger import make_recognition_decision_id

        decision_id = make_recognition_decision_id(self.event_id, 1, 1)
        metadata = None
        spool = None
        if attachment is True:
            spool = spool_private_crop(
                self.crop,
                decision_id=decision_id,
                root=self.root,
                cfg=self.cfg,
            )
            metadata = spool.to_job_metadata()
        elif isinstance(attachment, dict):
            metadata = attachment
        actual = self.state.record_recognition_decision(
            event_id=self.event_id,
            face_index=1,
            face_count=1,
            bbox=[1, 2, 30, 40],
            face_width=29,
            face_height=38,
            detection_score=0.99,
            best_employee="HR-0001",
            best_score=0.91,
            runner_up_score=0.20,
            score_margin=0.71,
            pad_passed=True,
            pad_skipped=False,
            accepted=True,
            reason_code="accepted_candidate",
            candidate_log_type="IN",
            retention_state="temporary",
            decision_version=1,
            attachment=metadata,
        )
        self.assertEqual(actual, decision_id)
        return decision_id, spool

    def deliver_parent(self, decision_id, *, docname="CHK-0001"):
        job = self.state.claim_next_delivery_job(
            owner="delivery-worker",
            lease_seconds=60,
            transport="rest",
            max_attempts=3,
            now=1000.0,
        )
        self.assertEqual(job["decision_id"], decision_id)
        self.state.mark_delivery_submission_started(
            job["delivery_id"], owner="delivery-worker", now=1001.0
        )
        return self.state.mark_delivery_job_delivered_by_lease(
            job["delivery_id"],
            owner="delivery-worker",
            remote_docname=docname,
            transport="rest",
            now=1002.0,
        )

    def worker(self, outcomes):
        return AttachmentWorker(
            self.state,
            FakeAttachmentAdapter(outcomes),
            self.settings,
            cfg=self.cfg,
            root=self.root,
            owner="attachment-worker",
            clock=lambda: 1100.0,
            random_source=lambda: 0.5,
            logger=lambda _message: None,
        )

    def test_populated_schema_v7_is_backed_up_migrated_and_preserved(self):
        previous = self.root / "schema-v7.sqlite3"
        connection = sqlite3.connect(previous)
        connection.row_factory = sqlite3.Row
        try:
            for version in range(1, 8):
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
                    (version, migration.name, migration.checksum, utc_now()),
                )
                connection.execute(f"PRAGMA user_version = {version}")
                connection.commit()
        finally:
            connection.close()

        legacy = SchemaSevenState(previous)
        digest = "c" * 64
        event_id = make_event_id("camera-in", "IN", digest)
        claim = legacy.record_event_receipt(
            event_id=event_id,
            capture_id=make_capture_id(
                "camera-in", digest, "legacy.jpg", 123, 1000.0
            ),
            camera_id="camera-in",
            log_type="IN",
            source_sha256=digest,
            source_name="legacy.jpg",
            source_mtime=1000.0,
            source_size=123,
            received_at="2026-08-14T00:00:00Z",
            effective_at="2026-08-14T00:00:00Z",
            branch="Baghdad",
            source_type="holowits_ftp",
            source_principal="camera_in",
            source_binding_id="d" * 64,
            policy="IN",
            receipt_state="verified",
            receipt_verified=True,
            receipt_detail={},
            policy_version="directional-v1",
        )
        self.assertTrue(claim.accepted)
        decision_id = legacy.record_recognition_decision(
            event_id=event_id,
            face_index=1,
            face_count=1,
            bbox=[1, 2, 30, 40],
            face_width=29,
            face_height=38,
            detection_score=0.99,
            best_employee="HR-0001",
            best_score=0.91,
            runner_up_score=0.20,
            score_margin=0.71,
            pad_passed=True,
            pad_skipped=False,
            accepted=True,
            reason_code="accepted_candidate",
            candidate_log_type="IN",
            retention_state="not_retained",
            decision_version=1,
        )

        migrated = RuntimeState(
            previous, backup_dir=self.root / "schema-v7-backups"
        )
        self.assertEqual(migrated.migration_status()["schema_version"], 8)
        self.assertIsNotNone(migrated.last_migration_backup)
        backup = verify_runtime_backup(migrated.last_migration_backup["path"])
        self.assertTrue(backup["ok"], backup)
        self.assertEqual(backup["schema_version"], 7)
        self.assertIsNotNone(migrated.delivery_job_for_decision(decision_id))
        self.assertIsNone(migrated.attachment_job_for_decision(decision_id))

    def test_schema_v8_has_attachment_jobs_and_guards(self):
        self.assertEqual(RUNTIME_SCHEMA_VERSION, 8)
        self.assertEqual(self.state.migration_status()["schema_version"], 8)
        connection = sqlite3.connect(self.state.path)
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            indexes = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            }
            triggers = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                )
            }
        finally:
            connection.close()
        self.assertIn("attachment_jobs", tables)
        self.assertTrue(set(ATTACHMENT_OUTBOX_REQUIRED_INDEXES).issubset(indexes))
        self.assertTrue(ATTACHMENT_OUTBOX_REQUIRED_TRIGGERS.issubset(triggers))

        decision_id, _spool = self.record_decision()
        attachment = self.state.attachment_job_for_decision(decision_id)
        connection = sqlite3.connect(self.state.path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    UPDATE attachment_jobs
                    SET state = 'pending', submission_started_at = '2026-08-14T00:00:00Z'
                    WHERE attachment_id = ?
                    """,
                    (attachment["attachment_id"],),
                )
        finally:
            connection.close()

    def test_decision_creates_waiting_attachment_atomically(self):
        decision_id, spool = self.record_decision()
        delivery = self.state.delivery_job_for_decision(decision_id)
        attachment = self.state.attachment_job_for_decision(decision_id)
        self.assertEqual(
            attachment["attachment_id"], make_attachment_id(decision_id)
        )
        self.assertEqual(attachment["delivery_id"], delivery["delivery_id"])
        self.assertEqual(attachment["state"], "waiting_for_checkin")
        self.assertEqual(attachment["source_sha256"], spool.source_sha256)
        self.assertIsNone(
            self.state.claim_next_attachment_job(
                owner="attachment-worker",
                lease_seconds=60,
                transport="rest",
                max_attempts=3,
                now=1000.0,
            )
        )

    def test_attachment_insert_failure_rolls_back_decision_and_delivery(self):
        connection = sqlite3.connect(self.state.path)
        try:
            connection.execute(
                """
                CREATE TRIGGER synthetic_attachment_failure
                BEFORE INSERT ON attachment_jobs
                BEGIN
                    SELECT RAISE(ABORT, 'synthetic attachment failure');
                END
                """
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(EventLedgerValidationError):
            self.record_decision()
        connection = sqlite3.connect(self.state.path)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM recognition_decisions"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM delivery_jobs").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM attachment_jobs").fetchone()[0],
                0,
            )
        finally:
            connection.close()

    def test_successful_attachment_never_changes_delivered_checkin(self):
        decision_id, spool = self.record_decision()
        delivered = self.deliver_parent(decision_id)
        worker = self.worker(
            [PrivateAttachmentResult("FILE-0001", "/private/files/crop.jpg", "rest")]
        )
        result = worker.run_once(max_jobs=1)
        self.assertEqual(result["outcomes"], ["attached"])
        attachment = self.state.attachment_job_for_decision(decision_id)
        self.assertEqual(attachment["state"], "attached")
        self.assertEqual(attachment["parent_docname"], "CHK-0001")
        self.assertEqual(attachment["remote_file_docname"], "FILE-0001")
        self.assertEqual(attachment["source_state"], "deleted")
        self.assertFalse(Path(spool.source_path).exists())
        self.assertEqual(
            self.state.get_delivery_job(delivered["delivery_id"])["state"],
            "delivered",
        )

    def test_attachment_retry_does_not_downgrade_delivered_checkin(self):
        decision_id, spool = self.record_decision()
        delivered = self.deliver_parent(decision_id)
        worker = self.worker([requests.ConnectTimeout("attachment connect timeout")])
        result = worker.run_once(max_jobs=1)
        self.assertEqual(result["outcomes"], ["retry_wait"])
        attachment = self.state.attachment_job_for_decision(decision_id)
        self.assertEqual(attachment["state"], "retry_wait")
        self.assertEqual(
            attachment["last_error_class"], "attachment_connect_timeout"
        )
        self.assertTrue(Path(spool.source_path).is_file())
        self.assertEqual(
            self.state.get_delivery_job(delivered["delivery_id"])["state"],
            "delivered",
        )

    def test_permanent_attachment_failure_preserves_checkin_and_crop(self):
        decision_id, spool = self.record_decision()
        delivered = self.deliver_parent(decision_id)
        worker = self.worker(
            [ERPNextAdapterConfigurationError("attachment endpoint invalid")]
        )
        result = worker.run_once(max_jobs=1)
        self.assertEqual(result["outcomes"], ["permanent_failure"])
        attachment = self.state.attachment_job_for_decision(decision_id)
        self.assertEqual(attachment["state"], "permanent_failure")
        self.assertTrue(Path(spool.source_path).is_file())
        self.assertEqual(
            self.state.get_delivery_job(delivered["delivery_id"])["state"],
            "delivered",
        )

    def test_spool_failure_is_durable_but_checkin_remains_deliverable(self):
        decision_id, _ = self.record_decision(
            attachment={
                "error_class": "attachment_spool_failed",
                "error": "disk full",
            }
        )
        attachment = self.state.attachment_job_for_decision(decision_id)
        self.assertEqual(attachment["state"], "permanent_failure")
        delivered = self.deliver_parent(decision_id)
        self.assertEqual(delivered["state"], "delivered")
        self.assertEqual(
            self.state.attachment_job_for_decision(decision_id)["state"],
            "permanent_failure",
        )

    def test_unresolved_attachment_protects_event_from_retention(self):
        decision_id, _ = self.record_decision()
        self.deliver_parent(decision_id)
        self.state.transition_event(
            self.event_id,
            to_state="processed",
            reason_code="processed_no_checkin",
            compatibility_status="processed_no_checkin",
        )
        connection = sqlite3.connect(self.state.path)
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


    def test_terminal_attachment_allows_normal_event_retention(self):
        decision_id, _ = self.record_decision()
        self.deliver_parent(decision_id)
        worker = self.worker(
            [ERPNextAdapterConfigurationError("attachment endpoint invalid")]
        )
        self.assertEqual(
            worker.run_once(max_jobs=1)["outcomes"],
            ["permanent_failure"],
        )
        self.state.transition_event(
            self.event_id,
            to_state="processed",
            reason_code="processed_no_checkin",
            compatibility_status="processed_no_checkin",
        )
        connection = sqlite3.connect(self.state.path)
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


if __name__ == "__main__":
    unittest.main()
