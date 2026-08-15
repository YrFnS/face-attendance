import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import requests

from erpnext_adapter import (
    BenchERPNextAdapter,
    EmployeeCheckinRequest,
    EmployeeCheckinResult,
    ERPNextAdapterConflictError,
    ERPNextAdapterContractError,
    RESTERPNextAdapter,
)
from delivery_service import DeliveryWorker, DeliveryWorkerSettings
from erpnext_idempotency import (
    DEFAULT_IDEMPOTENCY_CREATE_METHOD,
    DEFAULT_IDEMPOTENCY_PROBE_METHOD,
    IDEMPOTENCY_APP_NAME,
    IDEMPOTENCY_CONTRACT_VERSION,
    ERPNextIdempotencyCapabilityError,
    capability_fingerprint,
    idempotency_configuration_issues,
    parse_capability,
)
from event_ledger import make_capture_id, make_recognition_decision_id
from runtime_state import (
    MIGRATION_BY_VERSION,
    RUNTIME_SCHEMA_VERSION,
    RuntimeState,
    make_event_id,
    utc_now,
    verify_runtime_backup,
)


APP_ROOT = (
    Path(__file__).resolve().parent
    / "frappe_apps"
    / "face_attendance_idempotency"
)
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from face_attendance_idempotency.contract import (  # noqa: E402
    APP_NAME,
    CONTRACT_VERSION,
    CREATE_METHOD,
    DELIVERY_PAYLOAD_CONTRACT_VERSION,
    PROBE_METHOD,
    CreateRequest,
    DeliveryConflictError,
    DuplicateDeliveryId,
    capability_payload,
    create_or_get,
)
from face_attendance_idempotency.install import (  # noqa: E402
    normalize_employee_checkin_delivery_id,
)
from face_attendance_idempotency.api import _is_unique_violation  # noqa: E402


class FakeResponse:
    def __init__(self, payload=None, *, status=200):
        self.payload = payload if payload is not None else {}
        self.status_code = status
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError(f"HTTP {self.status_code}")
            error.response = self
            raise error

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class AtomicMemoryStore:
    def __init__(self):
        self.rows = {}
        self.lock = threading.Lock()
        self.counter = 0

    def insert(self, request):
        with self.lock:
            if request.delivery_id in self.rows:
                raise DuplicateDeliveryId()
            self.counter += 1
            row = {
                "name": f"CHK-{self.counter:04d}",
                **request.immutable_payload(),
            }
            self.rows[request.delivery_id] = row
            return row

    def find_for_update(self, delivery_id):
        with self.lock:
            row = self.rows.get(delivery_id)
            return dict(row) if row else None


class ERPNextIdempotencyTests(unittest.TestCase):
    def capability(self):
        return capability_payload("erp.example.test", "mariadb")

    def request(self, *, employee="HR-0001"):
        return CreateRequest.build(
            delivery_id="d" * 64,
            employee=employee,
            log_type="IN",
            time="2026-08-14 08:00:00",
            delivery_contract_version="erpnext-employee-checkin-v1",
            event_id="e" * 64,
            decision_id="c" * 64,
            camera_id="camera-in",
            branch="Baghdad",
        )

    def test_server_and_client_contract_constants_match(self):
        self.assertEqual(APP_NAME, IDEMPOTENCY_APP_NAME)
        self.assertEqual(CONTRACT_VERSION, IDEMPOTENCY_CONTRACT_VERSION)
        self.assertEqual(CREATE_METHOD, DEFAULT_IDEMPOTENCY_CREATE_METHOD)
        self.assertEqual(PROBE_METHOD, DEFAULT_IDEMPOTENCY_PROBE_METHOD)
        self.assertEqual(
            DELIVERY_PAYLOAD_CONTRACT_VERSION,
            "erpnext-employee-checkin-v1",
        )

    def test_capability_is_self_verifying_and_destination_pinned(self):
        payload = self.capability()
        self.assertEqual(payload["fingerprint"], capability_fingerprint(payload))
        parsed = parse_capability(
            payload,
            expected_site="erp.example.test",
            expected_fingerprint=payload["fingerprint"],
        )
        self.assertEqual(parsed.site, "erp.example.test")
        tampered = dict(payload, site="other.example.test")
        with self.assertRaisesRegex(
            ERPNextIdempotencyCapabilityError, "fingerprint"
        ):
            parse_capability(tampered)
        with self.assertRaisesRegex(
            ERPNextIdempotencyCapabilityError, "approved value"
        ):
            parse_capability(payload, expected_fingerprint="0" * 64)

    def test_atomic_create_or_get_returns_one_document_under_concurrency(self):
        store = AtomicMemoryStore()
        request = self.request()
        results = []
        errors = []

        def run():
            try:
                results.append(create_or_get(store, request))
            except Exception as exc:  # pragma: no cover - diagnostic
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(store.rows), 1)
        self.assertEqual({result.name for result in results}, {"CHK-0001"})
        self.assertEqual(sum(result.created for result in results), 1)

    def test_same_delivery_id_with_conflicting_payload_is_rejected(self):
        store = AtomicMemoryStore()
        create_or_get(store, self.request())
        with self.assertRaisesRegex(DeliveryConflictError, "employee"):
            create_or_get(store, self.request(employee="HR-0002"))

    def test_rest_adapter_probes_and_calls_atomic_method(self):
        capability = self.capability()
        session = FakeSession(
            [
                FakeResponse({"message": capability}),
                FakeResponse(
                    {
                        "message": {
                            "ok": True,
                            "name": "CHK-0001",
                            "created": True,
                            "delivery_id": "d" * 64,
                            "contract_version": capability["contract_version"],
                            "site": capability["site"],
                            "fingerprint": capability["fingerprint"],
                            "delivery_payload_contract_version": capability[
                                "delivery_payload_contract_version"
                            ],
                        }
                    }
                ),
            ]
        )
        adapter = RESTERPNextAdapter(
            base_url="https://erp.example.test",
            api_key="key",
            api_secret="secret",
            session=session,
            idempotency_required=True,
            expected_site=capability["site"],
            expected_fingerprint=capability["fingerprint"],
        )
        request = EmployeeCheckinRequest.build(
            "HR-0001",
            "IN",
            "2026-08-14T08:00:00Z",
            delivery_id="d" * 64,
            event_id="e" * 64,
            decision_id="c" * 64,
            camera_id="camera-in",
            branch="Baghdad",
            delivery_contract_version="erpnext-employee-checkin-v1",
        )
        result = adapter.create_employee_checkin(request)
        self.assertTrue(result.idempotency_verified)
        self.assertEqual(result.docname, "CHK-0001")
        self.assertTrue(session.calls[0][0].endswith(DEFAULT_IDEMPOTENCY_PROBE_METHOD))
        self.assertTrue(session.calls[1][0].endswith(DEFAULT_IDEMPOTENCY_CREATE_METHOD))
        sent = session.calls[1][1]["json"]
        self.assertEqual(sent["delivery_id"], "d" * 64)
        self.assertEqual(sent["expected_fingerprint"], capability["fingerprint"])

    def test_bench_adapter_uses_same_probe_and_create_methods(self):
        capability = self.capability()
        calls = []

        def execute(method, kwargs):
            calls.append((method, kwargs))
            if method == DEFAULT_IDEMPOTENCY_PROBE_METHOD:
                return capability
            return {
                "ok": True,
                "name": "CHK-BENCH-1",
                "created": False,
                "delivery_id": "d" * 64,
                "contract_version": capability["contract_version"],
                "site": capability["site"],
                "fingerprint": capability["fingerprint"],
                "delivery_payload_contract_version": capability[
                    "delivery_payload_contract_version"
                ],
            }

        adapter = BenchERPNextAdapter(
            execute=execute,
            idempotency_required=True,
            expected_site=capability["site"],
            expected_fingerprint=capability["fingerprint"],
        )
        request = EmployeeCheckinRequest.build(
            "HR-0001",
            "IN",
            "2026-08-14T08:00:00Z",
            delivery_id="d" * 64,
            event_id="e" * 64,
            decision_id="c" * 64,
            camera_id="camera-in",
            branch="Baghdad",
            delivery_contract_version="erpnext-employee-checkin-v1",
        )
        result = adapter.create_employee_checkin(request)
        self.assertFalse(result.created)
        self.assertEqual(
            [method for method, _kwargs in calls],
            [DEFAULT_IDEMPOTENCY_PROBE_METHOD, DEFAULT_IDEMPOTENCY_CREATE_METHOD],
        )

    def test_rest_conflict_is_permanent_adapter_conflict(self):
        capability = self.capability()
        adapter = RESTERPNextAdapter(
            base_url="https://erp.example.test",
            api_key="key",
            api_secret="secret",
            session=FakeSession(
                [
                    FakeResponse({"message": capability}),
                    FakeResponse(
                        {
                            "message": {
                                "ok": False,
                                "error_code": "delivery_id_conflict",
                                "message": "delivery ID conflicts",
                            }
                        },
                        status=409,
                    ),
                ]
            ),
            idempotency_required=True,
            expected_site=capability["site"],
            expected_fingerprint=capability["fingerprint"],
        )
        request = EmployeeCheckinRequest.build(
            "HR-0001",
            "IN",
            "2026-08-14T08:00:00Z",
            delivery_id="d" * 64,
            event_id="e" * 64,
            decision_id="c" * 64,
            camera_id="camera-in",
            branch="Baghdad",
            delivery_contract_version="erpnext-employee-checkin-v1",
        )
        with self.assertRaises(ERPNextAdapterConflictError):
            adapter.create_employee_checkin(request)

    def test_bench_structured_conflict_is_permanent_adapter_conflict(self):
        capability = self.capability()

        def execute(method, kwargs):
            del kwargs
            if method == DEFAULT_IDEMPOTENCY_PROBE_METHOD:
                return capability
            return {
                "ok": False,
                "error_code": "delivery_id_conflict",
                "message": "delivery ID conflicts",
            }

        adapter = BenchERPNextAdapter(
            execute=execute,
            idempotency_required=True,
            expected_site=capability["site"],
            expected_fingerprint=capability["fingerprint"],
        )
        request = EmployeeCheckinRequest.build(
            "HR-0001",
            "IN",
            "2026-08-14T08:00:00Z",
            delivery_id="d" * 64,
            event_id="e" * 64,
            decision_id="c" * 64,
            camera_id="camera-in",
            branch="Baghdad",
            delivery_contract_version="erpnext-employee-checkin-v1",
        )
        with self.assertRaises(ERPNextAdapterConflictError):
            adapter.create_employee_checkin(request)

    def test_server_rejects_unsupported_delivery_payload_contract(self):
        with self.assertRaisesRegex(
            Exception, "supported payload contract"
        ):
            CreateRequest.build(
                delivery_id="d" * 64,
                employee="HR-0001",
                log_type="IN",
                time="2026-08-14 08:00:00",
                delivery_contract_version="unsupported-v2",
                event_id="e" * 64,
                decision_id="c" * 64,
                camera_id="camera-in",
                branch="Baghdad",
            )

    def test_manual_employee_checkin_blank_delivery_id_is_normalized_to_null(self):
        class Document:
            custom_face_attendance_delivery_id = ""

        document = Document()
        normalize_employee_checkin_delivery_id(document)
        self.assertIsNone(document.custom_face_attendance_delivery_id)

    def test_server_recognizes_only_the_delivery_unique_constraint(self):
        class UniqueValidationError(Exception):
            pass

        class Frappe:
            pass

        expected = UniqueValidationError(
            "Employee Checkin",
            "CHK-0001",
            "Duplicate entry for key 'unique_face_attendance_delivery_id'",
        )
        nested = UniqueValidationError(
            "Employee Checkin",
            "CHK-0001",
            RuntimeError(
                "Duplicate entry for key 'unique_face_attendance_delivery_id'"
            ),
        )
        unrelated = UniqueValidationError(
            "Employee Checkin",
            "CHK-0001",
            "Duplicate entry for key 'some_other_constraint'",
        )
        self.assertTrue(_is_unique_violation(Frappe(), expected))
        self.assertTrue(_is_unique_violation(Frappe(), nested))
        self.assertFalse(_is_unique_violation(Frappe(), unrelated))

    def test_production_worker_configuration_requires_pinned_contract(self):
        issues = idempotency_configuration_issues(
            {
                "production_mode": True,
                "delivery_mode": "worker",
                "erpnext_idempotency_required": False,
            }
        )
        text = "\n".join(issues)
        self.assertIn("erpnext_idempotency_required", text)
        self.assertIn("erpnext_expected_site", text)
        self.assertIn("erpnext_expected_idempotency_fingerprint", text)

    def test_worker_refreshes_the_live_capability_after_cache_expiry(self):
        first = parse_capability(
            self.capability(),
            expected_site="erp.example.test",
            expected_fingerprint=self.capability()["fingerprint"],
        )
        second_payload = capability_payload("erp.example.test", "postgres")
        second = parse_capability(
            second_payload,
            expected_site="erp.example.test",
            expected_fingerprint=second_payload["fingerprint"],
        )

        class ProbeAdapter:
            transport = "rest"

            def __init__(self):
                self.calls = []

            def verify_idempotency_contract(self, *, force=False):
                self.calls.append(bool(force))
                return first if len(self.calls) == 1 else second

        class State:
            def recover_expired_delivery_job_leases(self, **_kwargs):
                return []

            def claim_next_delivery_job(self, **_kwargs):
                return None

        clock = [1000.0]
        settings = DeliveryWorkerSettings(
            mode="worker",
            enabled=True,
            poll_seconds=0.1,
            batch_size=1,
            lease_seconds=60,
            heartbeat_seconds=10,
            max_attempts=3,
            retry_base_seconds=5.0,
            retry_max_seconds=60.0,
            retry_jitter_fraction=0.0,
            queue_max_active_jobs=100,
            queue_min_free_bytes=0,
            idempotency_required=True,
            idempotency_probe_cache_seconds=30.0,
        )
        adapter = ProbeAdapter()
        worker = DeliveryWorker(
            State(),
            adapter,
            settings,
            clock=lambda: clock[0],
            logger=lambda _message: None,
        )
        worker.run_once(max_jobs=1)
        clock[0] = 1010.0
        worker.run_once(max_jobs=1)
        clock[0] = 1040.0
        worker.run_once(max_jobs=1)
        self.assertEqual(adapter.calls, [True, True])
        self.assertEqual(worker._idempotency_capability.database_type, "postgres")



class RuntimeIdempotencyBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = RuntimeState(
            self.root / "runtime.sqlite3",
            backup_dir=self.root / "backups",
        )
        self.digest = "a" * 64
        self.event_id = make_event_id("camera-in", "IN", self.digest)
        self.state.record_event_receipt(
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
            source_type="fixture",
            source_principal="fixture",
            source_binding_id="b" * 64,
            policy="IN",
            receipt_state="verified",
            receipt_verified=True,
            receipt_detail={"verified": True},
            policy_version="directional-v1",
        )
        self.decision_id = make_recognition_decision_id(self.event_id, 1, 1)
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
            retention_state="not_retained",
            decision_version=1,
        )

    def tearDown(self):
        self.temp.cleanup()

    def capability(self, *, site="erp.example.test"):
        payload = capability_payload(site, "mariadb")
        return parse_capability(
            payload,
            expected_site=site,
            expected_fingerprint=payload["fingerprint"],
        )

    def claim(self, *, owner="worker-a", now=1000.0):
        return self.state.claim_next_delivery_job(
            owner=owner,
            lease_seconds=60,
            transport="rest",
            max_attempts=3,
            now=now,
        )

    def test_schema_v8_and_job_binding_are_verified_and_immutable(self):
        self.assertEqual(RUNTIME_SCHEMA_VERSION, 8)
        job = self.claim()
        bound = self.state.bind_delivery_job_idempotency_contract_by_lease(
            job["delivery_id"],
            owner="worker-a",
            capability=self.capability(),
            now=1001.0,
        )
        self.assertEqual(bound["erpnext_site"], "erp.example.test")
        self.assertTrue(bound["erpnext_idempotency_verified_at"])
        connection = sqlite3.connect(self.state.path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE delivery_jobs SET erpnext_site = 'other' "
                    "WHERE delivery_id = ?",
                    (job["delivery_id"],),
                )
        finally:
            connection.close()

    def test_schema_v7_database_is_backed_up_before_idempotency_migration(self):
        previous = self.root / "schema-v7.sqlite3"
        connection = sqlite3.connect(previous)
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

        migrated = RuntimeState(
            previous,
            backup_dir=self.root / "schema-v7-backups",
        )
        self.assertEqual(migrated.migration_status()["schema_version"], 8)
        self.assertIsNotNone(migrated.last_migration_backup)
        backup = verify_runtime_backup(migrated.last_migration_backup["path"])
        self.assertTrue(backup["ok"], backup)
        self.assertEqual(backup["schema_version"], 7)

    def test_post_submit_expiry_retries_only_with_verified_contract(self):
        job = self.claim()
        self.state.bind_delivery_job_idempotency_contract_by_lease(
            job["delivery_id"],
            owner="worker-a",
            capability=self.capability(),
            now=1001.0,
        )
        self.state.mark_delivery_submission_started(
            job["delivery_id"], owner="worker-a", now=1002.0
        )
        recovered = self.state.recover_expired_delivery_job_leases(
            max_attempts=3, now=1100.0
        )
        self.assertEqual(recovered[0]["state"], "retry_wait")
        self.assertEqual(
            recovered[0]["last_error_class"],
            "delivery_lease_expired_after_idempotent_submission",
        )

    def test_contract_drift_is_rejected_before_submission(self):
        job = self.claim()
        self.state.bind_delivery_job_idempotency_contract_by_lease(
            job["delivery_id"],
            owner="worker-a",
            capability=self.capability(),
            now=1001.0,
        )
        with self.assertRaisesRegex(Exception, "different ERPNext"):
            self.state.bind_delivery_job_idempotency_contract_by_lease(
                job["delivery_id"],
                owner="worker-a",
                capability=self.capability(site="other.example.test"),
                now=1002.0,
            )

    def test_timeout_after_remote_commit_replays_to_one_checkin(self):
        capability = self.capability()
        remote = AtomicMemoryStore()
        clock = [1000.0]

        class CommitThenTimeoutAdapter:
            transport = "rest"

            def __init__(self):
                self.calls = 0

            def verify_idempotency_contract(self, *, force=False):
                del force
                return capability

            def create_employee_checkin(self, request, image_path=None):
                self.calls += 1
                self.assert_no_image(image_path)
                server_request = CreateRequest.build(
                    delivery_id=request.delivery_id,
                    employee=request.employee,
                    log_type=request.log_type,
                    time=request.event_time,
                    delivery_contract_version=request.delivery_contract_version,
                    event_id=request.event_id,
                    decision_id=request.decision_id,
                    camera_id=request.camera_id,
                    branch=request.branch,
                )
                result = create_or_get(remote, server_request)
                if self.calls == 1:
                    raise requests.ReadTimeout(
                        "response was lost after ERPNext committed"
                    )
                return EmployeeCheckinResult(
                    result.name,
                    self.transport,
                    created=result.created,
                    delivery_id=request.delivery_id,
                    idempotency_verified=True,
                    erpnext_site=capability.site,
                    idempotency_fingerprint=capability.fingerprint,
                    delivery_contract_version=(
                        capability.delivery_payload_contract_version
                    ),
                )

            @staticmethod
            def assert_no_image(image_path):
                if image_path is not None:
                    raise AssertionError("check-in delivery must not attach media")

        reservation = self.state.reserve_attendance_policy(
            employee="HR-0001",
            direction="IN",
            branch="Baghdad",
            policy_version="directional-v1",
            event_id=self.event_id,
            decision_id=self.decision_id,
            effective_at="2026-08-14T00:00:00Z",
            cooldown_seconds=600,
            reservation_seconds=300,
            now=900.0,
        )
        self.assertTrue(reservation.accepted)

        settings = DeliveryWorkerSettings(
            mode="worker",
            enabled=True,
            poll_seconds=0.1,
            batch_size=1,
            lease_seconds=60,
            heartbeat_seconds=10,
            max_attempts=3,
            retry_base_seconds=5.0,
            retry_max_seconds=60.0,
            retry_jitter_fraction=0.0,
            queue_max_active_jobs=100,
            queue_min_free_bytes=0,
            idempotency_required=True,
            idempotency_probe_cache_seconds=300.0,
        )
        adapter = CommitThenTimeoutAdapter()
        worker = DeliveryWorker(
            self.state,
            adapter,
            settings,
            owner="delivery-worker-a",
            clock=lambda: clock[0],
            random_source=lambda: 0.5,
            logger=lambda _message: None,
        )

        first = worker.run_once(max_jobs=1)
        self.assertEqual(first["outcomes"], ["retry_wait"])
        self.assertEqual(len(remote.rows), 1)
        first_job = self.state.delivery_job_for_decision(self.decision_id)
        self.assertEqual(first_job["last_error_class"], "read_timeout_idempotent_replay")
        self.assertEqual(first_job["erpnext_site"], capability.site)
        self.assertEqual(
            first_job["erpnext_idempotency_fingerprint"],
            capability.fingerprint,
        )

        clock[0] = first_job["next_attempt_unix"] + 1
        second = worker.run_once(max_jobs=1)
        self.assertEqual(second["outcomes"], ["delivered"])
        self.assertEqual(len(remote.rows), 1)
        final_job = self.state.delivery_job_for_decision(self.decision_id)
        self.assertEqual(final_job["remote_docname"], "CHK-0001")
        self.assertEqual(final_job["attempt_count"], 2)
        policy = self.state.attendance_policy_state(reservation.scope_key)
        self.assertEqual(policy["reservation_state"], "none")
        self.assertEqual(policy["committed_decision_id"], self.decision_id)


if __name__ == "__main__":
    unittest.main()
