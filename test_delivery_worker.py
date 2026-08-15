import sqlite3
import tempfile
import unittest
from pathlib import Path

import requests

from delivery_service import (
    DeliveryWorker,
    DeliveryWorkerSettings,
    SafeRetryableDeliveryError,
    configuration_issues,
    delivery_capacity_status,
    retry_delay_seconds,
)
from erpnext_adapter import (
    ERPNextAdapterConfigurationError,
    EmployeeCheckinResult,
)
from event_ledger import make_capture_id, make_recognition_decision_id
from processing_recovery import attendance_policy_scope_key
from runtime_state import RUNTIME_SCHEMA_VERSION, RuntimeState, make_event_id


class FakeAdapter:
    transport = "rest"

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def create_employee_checkin(self, request, image_path=None):
        self.requests.append((request, image_path))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class DeliveryWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = RuntimeState(
            self.root / "runtime.sqlite3",
            backup_dir=self.root / "backups",
        )
        self.digest = "a" * 64
        self.event_id = make_event_id("camera-in", "IN", self.digest)
        self.capture_id = make_capture_id(
            "camera-in", self.digest, "capture.jpg", 123, 1000.0
        )
        self.settings = DeliveryWorkerSettings(
            mode="worker",
            enabled=True,
            poll_seconds=0.1,
            batch_size=10,
            lease_seconds=60,
            heartbeat_seconds=10,
            max_attempts=3,
            retry_base_seconds=5.0,
            retry_max_seconds=60.0,
            retry_jitter_fraction=0.0,
            queue_max_active_jobs=100,
            queue_min_free_bytes=0,
            idempotency_required=False,
            idempotency_probe_cache_seconds=300.0,
        )

    def tearDown(self):
        self.temp.cleanup()

    def record_event(self, *, digest=None, event_id=None):
        digest = digest or self.digest
        event_id = event_id or self.event_id
        claim = self.state.record_event_receipt(
            event_id=event_id,
            capture_id=make_capture_id(
                "camera-in", digest, "capture.jpg", 123, 1000.0
            ),
            camera_id="camera-in",
            log_type="IN",
            source_sha256=digest,
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
        return event_id

    def record_accepted_decision(self, *, reserve_policy=False, version=1):
        decision_id = make_recognition_decision_id(self.event_id, 1, version)
        reservation = None
        if reserve_policy:
            reservation = self.state.reserve_attendance_policy(
                employee="HR-0001",
                direction="IN",
                branch="Baghdad",
                policy_version="directional-v1",
                event_id=self.event_id,
                decision_id=decision_id,
                effective_at="2026-08-14T00:00:00Z",
                cooldown_seconds=600,
                reservation_seconds=300,
                now=900.0,
            )
            self.assertTrue(reservation.accepted)
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
            retention_state="not_retained",
            decision_version=version,
        )
        self.assertEqual(actual, decision_id)
        return decision_id, reservation

    def prepare_job(self, *, reserve_policy=False):
        self.record_event()
        return self.record_accepted_decision(reserve_policy=reserve_policy)

    def worker(self, outcomes, *, random_value=0.5):
        return DeliveryWorker(
            self.state,
            FakeAdapter(outcomes),
            self.settings,
            owner="delivery-worker-a",
            clock=lambda: 1000.0,
            random_source=lambda: random_value,
            logger=lambda _message: None,
        )

    def test_schema_v8_has_worker_submission_boundary(self):
        self.assertEqual(RUNTIME_SCHEMA_VERSION, 8)
        self.assertEqual(self.state.migration_status()["schema_version"], 8)
        connection = sqlite3.connect(self.state.path)
        try:
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(delivery_jobs)"
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
        self.assertIn("submission_started_at", columns)
        self.assertIn("retry_delay_seconds", columns)
        self.assertIn("delivery_jobs_submission_lease", indexes)

    def test_retry_delay_is_bounded_and_jittered(self):
        self.assertEqual(
            retry_delay_seconds(
                1,
                base_seconds=5,
                maximum_seconds=60,
                jitter_fraction=0,
            ),
            5,
        )
        self.assertEqual(
            retry_delay_seconds(
                10,
                base_seconds=5,
                maximum_seconds=60,
                jitter_fraction=0,
            ),
            60,
        )
        self.assertAlmostEqual(
            retry_delay_seconds(
                2,
                base_seconds=10,
                maximum_seconds=60,
                jitter_fraction=0.2,
                random_value=0.0,
            ),
            16.0,
        )

    def test_configuration_blocks_unsafe_worker_activation(self):
        issues = configuration_issues(
            {
                "delivery_mode": "worker",
                "delivery_worker_enabled": False,
                "attach_checkin_crop": True,
                "production_mode": True,
            }
        )
        text = "\n".join(issues)
        self.assertIn("delivery_worker_enabled", text)
        self.assertIn("attach_checkin_crop", text)
        self.assertIn("erpnext_idempotency_required", text)
        self.assertIn("erpnext_expected_site", text)
        self.assertIn("erpnext_expected_idempotency_fingerprint", text)

    def test_claim_is_exclusive_and_pre_submission_expiry_requeues(self):
        self.prepare_job()
        claimed = self.state.claim_next_delivery_job(
            owner="worker-a",
            lease_seconds=60,
            transport="rest",
            max_attempts=3,
            now=1000.0,
        )
        self.assertEqual(claimed["state"], "leased")
        self.assertEqual(claimed["attempt_count"], 1)
        self.assertIsNone(
            self.state.claim_next_delivery_job(
                owner="worker-b",
                lease_seconds=60,
                transport="rest",
                max_attempts=3,
                now=1010.0,
            )
        )
        recovered = self.state.recover_expired_delivery_job_leases(
            max_attempts=3,
            now=1100.0,
        )
        self.assertEqual(recovered[0]["state"], "retry_wait")
        again = self.state.claim_next_delivery_job(
            owner="worker-b",
            lease_seconds=60,
            transport="rest",
            max_attempts=3,
            now=1101.0,
        )
        self.assertEqual(again["attempt_count"], 2)
        self.assertEqual(again["lease_owner"], "worker-b")

    def test_expiry_after_submission_is_uncertain_and_blocks_policy(self):
        decision_id, reservation = self.prepare_job(reserve_policy=True)
        job = self.state.claim_next_delivery_job(
            owner="worker-a",
            lease_seconds=60,
            transport="rest",
            max_attempts=3,
            now=1000.0,
        )
        self.state.mark_delivery_submission_started(
            job["delivery_id"], owner="worker-a", now=1001.0
        )
        recovered = self.state.recover_expired_delivery_job_leases(
            max_attempts=3,
            now=1100.0,
        )
        self.assertEqual(recovered[0]["state"], "uncertain")
        policy = self.state.attendance_policy_state(reservation.scope_key)
        self.assertEqual(policy["reservation_state"], "uncertain")
        self.assertEqual(policy["reservation_decision_id"], decision_id)

    def test_success_marks_delivered_and_commits_policy(self):
        _decision_id, reservation = self.prepare_job(reserve_policy=True)
        worker = self.worker([EmployeeCheckinResult("CHK-0001", "rest")])
        result = worker.run_once(max_jobs=1)
        self.assertEqual(result["outcomes"], ["delivered"])
        job = self.state.list_delivery_jobs(limit=1)[0]
        self.assertEqual(job["state"], "delivered")
        self.assertEqual(job["remote_docname"], "CHK-0001")
        policy = self.state.attendance_policy_state(reservation.scope_key)
        self.assertEqual(policy["reservation_state"], "none")
        self.assertEqual(policy["committed_decision_id"], job["decision_id"])

    def test_connect_timeout_enters_bounded_retry(self):
        self.prepare_job()
        worker = self.worker([requests.ConnectTimeout("connect timed out")])
        result = worker.run_once(max_jobs=1)
        self.assertEqual(result["outcomes"], ["retry_wait"])
        job = self.state.list_delivery_jobs(limit=1)[0]
        self.assertEqual(job["attempt_count"], 1)
        self.assertEqual(job["next_attempt_unix"], 1005.0)
        self.assertEqual(job["last_error_class"], "connect_timeout")
        self.assertEqual(job["submission_started_at"], "")

    def test_rate_limit_honors_retry_after(self):
        self.prepare_job()
        response = requests.Response()
        response.status_code = 429
        response.headers["Retry-After"] = "30"
        error = requests.HTTPError("rate limited", response=response)
        worker = self.worker([error])
        worker.run_once(max_jobs=1)
        job = self.state.list_delivery_jobs(limit=1)[0]
        self.assertEqual(job["state"], "retry_wait")
        self.assertEqual(job["next_attempt_unix"], 1030.0)
        self.assertEqual(job["last_error_class"], "http_rate_limit")

    def test_validation_failure_is_permanent(self):
        self.prepare_job()
        worker = self.worker(
            [ERPNextAdapterConfigurationError("invalid ERPNext configuration")]
        )
        result = worker.run_once(max_jobs=1)
        self.assertEqual(result["outcomes"], ["permanent_failure"])
        job = self.state.list_delivery_jobs(limit=1)[0]
        self.assertEqual(job["last_error_class"], "adapter_configuration")

    def test_read_timeout_is_uncertain_not_retried(self):
        _decision_id, reservation = self.prepare_job(reserve_policy=True)
        worker = self.worker([requests.ReadTimeout("response timed out")])
        result = worker.run_once(max_jobs=1)
        self.assertEqual(result["outcomes"], ["uncertain"])
        job = self.state.list_delivery_jobs(limit=1)[0]
        self.assertEqual(job["last_error_class"], "read_timeout")
        policy = self.state.attendance_policy_state(reservation.scope_key)
        self.assertEqual(policy["reservation_state"], "uncertain")

    def test_retry_budget_exhaustion_is_permanent(self):
        self.prepare_job()
        for attempt in range(1, 4):
            claimed = self.state.claim_next_delivery_job(
                owner=f"worker-{attempt}",
                lease_seconds=60,
                transport="rest",
                max_attempts=3,
                now=1000.0 + attempt * 100,
            )
            self.assertIsNotNone(claimed)
            result = self.state.mark_delivery_job_retry_by_lease(
                claimed["delivery_id"],
                owner=f"worker-{attempt}",
                error_class="safe_pre_submission_failure",
                error="temporary",
                delay_seconds=1,
                max_attempts=3,
                safe_after_submission=True,
                now=1000.0 + attempt * 100,
            )
        self.assertEqual(result["state"], "permanent_failure")
        self.assertEqual(result["last_error_class"], "retry_budget_exhausted")

    def test_capacity_status_blocks_at_configured_limit(self):
        self.prepare_job()
        status = delivery_capacity_status(
            self.state,
            {
                "delivery_queue_max_active_jobs": 1,
                "delivery_queue_min_free_bytes": 0,
            },
        )
        self.assertFalse(status["ok"])
        self.assertEqual(status["active_jobs"], 1)

    def test_lease_can_be_renewed_only_by_owner(self):
        self.prepare_job()
        job = self.state.claim_next_delivery_job(
            owner="worker-a",
            lease_seconds=60,
            transport="rest",
            max_attempts=3,
            now=1000.0,
        )
        renewed = self.state.renew_delivery_job_lease(
            job["delivery_id"],
            owner="worker-a",
            lease_seconds=60,
            now=1020.0,
        )
        self.assertEqual(renewed["lease_expires_unix"], 1080.0)
        with self.assertRaisesRegex(Exception, "current delivery lease"):
            self.state.renew_delivery_job_lease(
                job["delivery_id"],
                owner="worker-b",
                lease_seconds=60,
                now=1021.0,
            )


    def test_systemd_and_installer_wire_delivery_worker(self):
        root = Path(__file__).resolve().parent
        service = (
            root / "deploy" / "systemd" / "face-attendance-delivery.service"
        ).read_text(encoding="utf-8")
        installer = (root / "install_linux.sh").read_text(encoding="utf-8")
        self.assertIn("delivery_service.py", service)
        self.assertIn("face-attendance-delivery", installer)
        self.assertIn("delivery_worker_enabled", installer)



if __name__ == "__main__":
    unittest.main()
