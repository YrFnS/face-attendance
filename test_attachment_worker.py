import os
import tempfile
import unittest
from pathlib import Path

import requests

from attachment_service import (
    AttachmentWorker,
    AttachmentWorkerSettings,
    attachment_capacity_status,
    configuration_issues,
)
from attachment_spool import (
    AttachmentSpoolError,
    resolve_spool_root,
    spool_private_crop,
)
from erpnext_adapter import PrivateAttachmentResult
from event_ledger import make_capture_id, make_recognition_decision_id
from runtime_state import RuntimeState, make_event_id


class FakeAdapter:
    transport = "rest"

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def create_employee_checkin(self, request, image_path=None):
        raise AssertionError("attachment worker must not create Employee Checkin")

    def attach_private_file(self, docname, image_path):
        self.calls.append((docname, Path(image_path)))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class AttachmentWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = RuntimeState(
            self.root / "runtime.sqlite3",
            backup_dir=self.root / "backups",
        )
        self.cfg = {
            "attach_checkin_crop": True,
            "attachment_worker_enabled": True,
            "attachment_spool_dir": str(self.root / "attachment-spool"),
            "attachment_max_image_bytes": 1024 * 1024,
            "attachment_queue_max_active_jobs": 100,
            "attachment_queue_min_free_bytes": 0,
            "attachment_orphan_grace_seconds": 60,
            "attachment_delete_spool_after_success": True,
        }
        self.settings = AttachmentWorkerSettings(
            enabled=True,
            poll_seconds=0.1,
            batch_size=10,
            lease_seconds=60,
            heartbeat_seconds=10,
            max_attempts=3,
            retry_base_seconds=5.0,
            retry_max_seconds=60.0,
            retry_jitter_fraction=0.0,
            max_image_bytes=1024 * 1024,
            queue_max_active_jobs=100,
            queue_min_free_bytes=0,
            orphan_grace_seconds=60,
        )
        self.digest = "a" * 64
        self.event_id = make_event_id("camera-in", "IN", self.digest)
        self.decision_id = make_recognition_decision_id(self.event_id, 1, 1)

    def tearDown(self):
        self.temp.cleanup()

    def create_delivered_parent_and_attachment(self):
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
            receipt_detail={},
            policy_version="directional-v1",
        )
        self.assertTrue(claim.accepted)
        crop = self.root / "crop.jpg"
        crop.write_bytes(b"\xff\xd8\xffworker-jpeg")
        record = spool_private_crop(
            crop,
            decision_id=self.decision_id,
            root=self.root,
            cfg=self.cfg,
        )
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
            attachment=record.to_job_metadata(),
        )
        self.assertEqual(actual, self.decision_id)
        delivery = self.state.claim_next_delivery_job(
            owner="delivery-a",
            lease_seconds=60,
            transport="rest",
            max_attempts=3,
            now=900.0,
        )
        self.state.mark_delivery_submission_started(
            delivery["delivery_id"], owner="delivery-a", now=901.0
        )
        delivered = self.state.mark_delivery_job_delivered_by_lease(
            delivery["delivery_id"],
            owner="delivery-a",
            remote_docname="CHK-0001",
            transport="rest",
            now=902.0,
        )
        return delivered, record

    def worker(self, outcomes, *, now=1000.0):
        adapter = FakeAdapter(outcomes)
        worker = AttachmentWorker(
            self.state,
            adapter,
            self.settings,
            cfg=self.cfg,
            root=self.root,
            owner="attachment-worker-a",
            clock=lambda: now,
            random_source=lambda: 0.5,
            logger=lambda _message: None,
        )
        return worker, adapter

    def test_success_attaches_and_deletes_only_spool_copy(self):
        delivered, record = self.create_delivered_parent_and_attachment()
        worker, adapter = self.worker(
            [PrivateAttachmentResult("FILE-0001", "/private/files/crop.jpg", "rest")]
        )
        result = worker.run_once(max_jobs=1)
        self.assertEqual(result["outcomes"], ["attached"])
        attachment = self.state.attachment_job_for_decision(self.decision_id)
        self.assertEqual(attachment["state"], "attached")
        self.assertEqual(attachment["remote_file_docname"], "FILE-0001")
        self.assertEqual(attachment["remote_file_url"], "/private/files/crop.jpg")
        self.assertEqual(attachment["source_state"], "deleted")
        self.assertFalse(Path(record.source_path).exists())
        self.assertEqual(adapter.calls[0][0], "CHK-0001")
        parent = self.state.get_delivery_job(delivered["delivery_id"])
        self.assertEqual(parent["state"], "delivered")

    def test_attachment_connect_timeout_retries_without_changing_checkin(self):
        delivered, _record = self.create_delivered_parent_and_attachment()
        worker, _adapter = self.worker([requests.ConnectTimeout("connect")])
        result = worker.run_once(max_jobs=1)
        self.assertEqual(result["outcomes"], ["retry_wait"])
        attachment = self.state.attachment_job_for_decision(self.decision_id)
        self.assertEqual(attachment["next_attempt_unix"], 1005.0)
        self.assertEqual(attachment["last_error_class"], "attachment_connect_timeout")
        self.assertEqual(
            self.state.get_delivery_job(delivered["delivery_id"])["state"],
            "delivered",
        )

    def test_attachment_read_timeout_is_uncertain_only_for_attachment(self):
        delivered, _record = self.create_delivered_parent_and_attachment()
        worker, _adapter = self.worker([requests.ReadTimeout("read")])
        result = worker.run_once(max_jobs=1)
        self.assertEqual(result["outcomes"], ["uncertain"])
        attachment = self.state.attachment_job_for_decision(self.decision_id)
        self.assertEqual(attachment["last_error_class"], "attachment_read_timeout")
        self.assertEqual(
            self.state.get_delivery_job(delivered["delivery_id"])["state"],
            "delivered",
        )

    def test_missing_spool_is_permanent_without_downgrading_checkin(self):
        delivered, record = self.create_delivered_parent_and_attachment()
        Path(record.source_path).unlink()
        worker, adapter = self.worker([])
        result = worker.run_once(max_jobs=1)
        self.assertEqual(result["outcomes"], ["permanent_failure"])
        self.assertEqual(adapter.calls, [])
        attachment = self.state.attachment_job_for_decision(self.decision_id)
        self.assertEqual(attachment["source_state"], "missing")
        self.assertEqual(
            self.state.get_delivery_job(delivered["delivery_id"])["state"],
            "delivered",
        )

    def test_expired_attachment_lease_recovery_observes_submission_boundary(self):
        self.create_delivered_parent_and_attachment()
        first = self.state.claim_next_attachment_job(
            owner="attachment-a",
            lease_seconds=60,
            transport="rest",
            max_attempts=3,
            now=1000.0,
        )
        recovered = self.state.recover_attachment_jobs(
            max_attempts=3, now=1100.0
        )
        self.assertEqual(recovered[0]["state"], "retry_wait")
        second = self.state.claim_next_attachment_job(
            owner="attachment-b",
            lease_seconds=60,
            transport="rest",
            max_attempts=3,
            now=1101.0,
        )
        self.state.mark_attachment_submission_started(
            second["attachment_id"], owner="attachment-b", now=1102.0
        )
        recovered = self.state.recover_attachment_jobs(
            max_attempts=3, now=1200.0
        )
        self.assertEqual(recovered[0]["state"], "uncertain")
        self.assertEqual(
            self.state.get_delivery_job(first["delivery_id"])["state"],
            "delivered",
        )

    def test_parent_failure_cancels_attachment_and_removes_spool(self):
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
            receipt_detail={},
            policy_version="directional-v1",
        )
        self.assertTrue(claim.accepted)
        crop = self.root / "cancelled-crop.jpg"
        crop.write_bytes(b"\xff\xd8\xffcancelled")
        record = spool_private_crop(
            crop,
            decision_id=self.decision_id,
            root=self.root,
            cfg=self.cfg,
        )
        self.state.record_recognition_decision(
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
            attachment=record.to_job_metadata(),
        )
        delivery = self.state.claim_next_delivery_job(
            owner="delivery-a",
            lease_seconds=60,
            transport="rest",
            max_attempts=3,
            now=900.0,
        )
        self.state.mark_delivery_job_permanent_failure_by_lease(
            delivery["delivery_id"],
            owner="delivery-a",
            error_class="validation",
            error="invalid employee",
            now=901.0,
        )
        worker, _adapter = self.worker([], now=1000.0)
        worker.recover()
        attachment = self.state.attachment_job_for_decision(self.decision_id)
        self.assertEqual(attachment["state"], "cancelled")
        self.assertEqual(attachment["source_state"], "deleted")
        self.assertFalse(Path(record.source_path).exists())

    def test_orphan_cleanup_never_deletes_referenced_media(self):
        _delivered, record = self.create_delivered_parent_and_attachment()
        orphan = Path(self.cfg["attachment_spool_dir"]) / ("f" * 64 + ".jpg")
        orphan.write_bytes(b"\xff\xd8\xfforphan")
        os.utime(orphan, (0, 0))
        os.utime(record.source_path, (0, 0))
        worker, _adapter = self.worker([], now=10000.0)
        worker.recover()
        self.assertFalse(orphan.exists())
        self.assertTrue(Path(record.source_path).exists())

    def test_spool_reuse_is_not_reported_as_newly_created(self):
        crop = self.root / "reused.jpg"
        crop.write_bytes(b"\xff\xd8\xffreused")
        first = spool_private_crop(
            crop,
            decision_id=self.decision_id,
            root=self.root,
            cfg=self.cfg,
        )
        second = spool_private_crop(
            crop,
            decision_id=self.decision_id,
            root=self.root,
            cfg=self.cfg,
        )
        self.assertTrue(first.newly_created)
        self.assertFalse(second.newly_created)
        self.assertEqual(first.source_path, second.source_path)

    def test_restart_cleans_attached_spool_leftover(self):
        delivered, record = self.create_delivered_parent_and_attachment()
        job = self.state.claim_next_attachment_job(
            owner="attachment-a",
            lease_seconds=60,
            transport="rest",
            max_attempts=3,
            now=1000.0,
        )
        self.state.mark_attachment_submission_started(
            job["attachment_id"], owner="attachment-a", now=1001.0
        )
        attached = self.state.mark_attachment_job_attached_by_lease(
            job["attachment_id"],
            owner="attachment-a",
            transport="rest",
            remote_file_docname="FILE-LEFTOVER",
            remote_file_url="/private/files/leftover.jpg",
            now=1002.0,
        )
        self.assertEqual(attached["state"], "attached")
        self.assertEqual(attached["source_state"], "available")
        self.assertTrue(Path(record.source_path).is_file())

        worker, _adapter = self.worker([], now=1100.0)
        worker.recover()

        current = self.state.get_attachment_job(job["attachment_id"])
        self.assertEqual(current["state"], "attached")
        self.assertEqual(current["source_state"], "deleted")
        self.assertFalse(Path(record.source_path).exists())
        self.assertEqual(
            self.state.get_delivery_job(delivered["delivery_id"])["state"],
            "delivered",
        )

    @unittest.skipIf(os.name == "nt", "symlink semantics differ on Windows")
    def test_spool_root_rejects_symbolic_link_components(self):
        real = self.root / "real-spool"
        real.mkdir()
        linked = self.root / "linked-spool"
        linked.symlink_to(real, target_is_directory=True)
        cfg = {**self.cfg, "attachment_spool_dir": str(linked)}

        issues = configuration_issues(cfg, root=self.root)
        self.assertTrue(any("symbolic link" in item for item in issues))
        with self.assertRaisesRegex(AttachmentSpoolError, "symbolic links"):
            resolve_spool_root(self.root, cfg)

    @unittest.skipIf(os.name == "nt", "POSIX file modes are not available")
    def test_worker_rejects_group_readable_spool_media(self):
        delivered, record = self.create_delivered_parent_and_attachment()
        os.chmod(record.source_path, 0o644)
        worker, adapter = self.worker([])

        result = worker.run_once(max_jobs=1)

        self.assertEqual(result["outcomes"], ["permanent_failure"])
        self.assertEqual(adapter.calls, [])
        current = self.state.attachment_job_for_decision(self.decision_id)
        self.assertEqual(
            current["last_error_class"], "attachment_source_invalid"
        )
        self.assertEqual(
            self.state.get_delivery_job(delivered["delivery_id"])["state"],
            "delivered",
        )

    def test_worker_refuses_attachment_outside_configured_spool(self):
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
            receipt_detail={},
            policy_version="directional-v1",
        )
        self.assertTrue(claim.accepted)
        from attachment_outbox import make_attachment_id

        attachment_id = make_attachment_id(self.decision_id)
        outside = self.root / "outside" / f"{attachment_id}.jpg"
        outside.parent.mkdir()
        outside.write_bytes(b"\xff\xd8\xffoutside")
        import hashlib

        digest = hashlib.sha256(outside.read_bytes()).hexdigest()
        self.state.record_recognition_decision(
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
            attachment={
                "source_path": str(outside.resolve()),
                "source_sha256": digest,
                "source_size": outside.stat().st_size,
                "filename": "outside.jpg",
                "content_type": "image/jpeg",
                "delete_after_success": True,
            },
        )
        delivery = self.state.claim_next_delivery_job(
            owner="delivery-a",
            lease_seconds=60,
            transport="rest",
            max_attempts=3,
            now=900.0,
        )
        self.state.mark_delivery_submission_started(
            delivery["delivery_id"], owner="delivery-a", now=901.0
        )
        self.state.mark_delivery_job_delivered_by_lease(
            delivery["delivery_id"],
            owner="delivery-a",
            remote_docname="CHK-OUTSIDE",
            transport="rest",
            now=902.0,
        )
        worker, adapter = self.worker([])
        result = worker.run_once(max_jobs=1)
        self.assertEqual(result["outcomes"], ["permanent_failure"])
        self.assertEqual(adapter.calls, [])
        self.assertTrue(outside.is_file())
        attachment = self.state.attachment_job_for_decision(self.decision_id)
        self.assertEqual(
            attachment["last_error_class"], "attachment_source_invalid"
        )

    def test_configuration_and_capacity_are_bounded(self):
        issues = configuration_issues(
            {
                "delivery_mode": "worker",
                "attach_checkin_crop": True,
                "attachment_worker_enabled": False,
            },
            root=self.root,
        )
        self.assertTrue(any("attachment_worker_enabled" in item for item in issues))
        self.create_delivered_parent_and_attachment()
        status = attachment_capacity_status(
            self.state,
            {
                **self.cfg,
                "attachment_queue_max_active_jobs": 1,
            },
            self.root,
        )
        self.assertFalse(status["ok"])
        self.assertEqual(status["active_jobs"], 1)


if __name__ == "__main__":
    unittest.main()
