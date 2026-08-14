import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from camera_sources import (
    load_camera_sources,
    receipt_path,
    source_by_username,
    write_source_receipt,
)
from event_admin import main as event_admin_main
from event_ledger import make_capture_id
from event_operations import (
    EventInspector,
    EventOperationError,
    EventSourceResolver,
)
from runtime_state import RuntimeState, file_sha256, make_event_id


class EventAdminTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "runtime_state.sqlite3"
        self.config_path = self.root / "config.json"
        self.source_path = self.root / "camera_uploads" / "in" / "event.jpg"
        self.source_path.parent.mkdir(parents=True)
        self.source_path.write_bytes(b"event-source-evidence")
        self.cfg = {
            "production_mode": False,
            "branch_name": "Baghdad",
            "camera_uploads_dir": str(self.root / "camera_uploads"),
            "camera_source_receipt_required": True,
            "camera_source_receipt_secret": "receipt-secret-" + "x" * 32,
            "camera_source_receipt_future_tolerance_seconds": 300,
            "ftp_permissions": "elw",
            "ftp_users": {
                "camera_in": {
                    "password": "camera-in-password-unique",
                    "permissions": "elw",
                }
            },
            "camera_sources": {
                "camera-in": {
                    "source_type": "holowits_ftp",
                    "branch": "Baghdad",
                    "policy": "IN",
                    "ftp_username": "camera_in",
                    "upload_dir": str(self.source_path.parent),
                    "allowed_networks": ["192.0.2.10/32"],
                }
            },
        }
        self.config_path.write_text(
            json.dumps(self.cfg, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.sources = load_camera_sources(self.cfg, self.root)
        self.digest, self.size = file_sha256(self.source_path)
        self.event_id = make_event_id("camera-in", "IN", self.digest)
        self.capture_id = make_capture_id(
            "camera-in",
            self.digest,
            self.source_path.name,
            self.size,
            self.source_path.stat().st_mtime,
        )
        source = source_by_username(self.sources, "camera_in")
        write_source_receipt(
            self.source_path,
            source,
            self.cfg,
            remote_ip="192.0.2.10",
            source_sha256=self.digest,
            source_size=self.size,
        )
        self.state = RuntimeState(
            self.database,
            backup_dir=self.root / "backups",
        )
        self.record_event()

    def tearDown(self):
        self.temp.cleanup()

    def record_event(self):
        claim = self.state.record_event_receipt(
            event_id=self.event_id,
            capture_id=self.capture_id,
            camera_id="camera-in",
            log_type="IN",
            source_sha256=self.digest,
            source_name=self.source_path.name,
            source_mtime=self.source_path.stat().st_mtime,
            source_size=self.size,
            received_at="2026-08-14T00:00:00Z",
            effective_at="2026-08-14T00:00:00Z",
            branch="Baghdad",
            source_type="holowits_ftp",
            source_principal="camera_in",
            source_remote_ip="192.0.2.10",
            source_binding_id="b" * 64,
            policy="IN",
            source_at="2026-08-13T23:59:59Z",
            source_time_provenance="filesystem_mtime_untrusted",
            transport_received_at="2026-08-14T00:00:00Z",
            receipt_state="verified",
            receipt_verified=True,
            receipt_detail={
                "verified": True,
                "remote_ip": "192.0.2.10",
                "signature": "must-not-be-exposed",
            },
            policy_version="directional-v1",
        )
        self.assertTrue(claim.accepted)

    def record_decision(self, *, accepted=False, decision_version=1):
        return self.state.record_recognition_decision(
            event_id=self.event_id,
            face_index=1,
            face_count=1,
            bbox=[1, 2, 30, 40],
            face_width=29,
            face_height=38,
            detection_score=0.99,
            best_employee="HR-001" if accepted else "",
            best_score=0.91 if accepted else 0.41,
            runner_up_score=0.55 if accepted else 0.39,
            score_margin=0.36 if accepted else 0.02,
            pad_passed=True,
            pad_skipped=False,
            pad_score=0.95,
            pad_provider="approved-provider",
            pad_model="liveness-v3",
            pad_evidence_id="evidence-1",
            pad_binding_id="c" * 64,
            accepted=accepted,
            reason_code="accepted_candidate" if accepted else "unknown_employee",
            candidate_log_type="IN",
            policy_version="directional-v1",
            gallery_version="gallery-42",
            gallery_generated_at="2026-08-14T00:00:00Z",
            gallery_model="buffalo_l",
            gallery_model_version="approved-v1",
            recognition_model="buffalo_l",
            recognition_model_version="approved-v1",
            preprocessing_version="preprocess-v1",
            retention_state="retained",
            decision_version=decision_version,
        )

    def reject_event(self, *, reason_code="no_face", retention="retained"):
        lease = self.state.acquire_event_lease(
            self.event_id,
            owner="test-worker",
            lease_seconds=60,
            now=1000,
        )
        self.assertTrue(lease.accepted)
        self.state.finalize_event_with_lease(
            self.event_id,
            owner="test-worker",
            to_state="rejected",
            reason_code=reason_code,
            compatibility_status="rejected",
            event_updates={"retention_state": retention},
            now=1001,
        )

    def test_read_only_list_filter_and_redaction(self):
        self.record_decision(accepted=True)
        self.state.record_operator_action(
            event_id=self.event_id,
            actor="auditor@example.com",
            action="reviewed",
            detail={
                "note": "visible",
                "token": "super-secret-token",
                "erp_password_hash": "must-also-be-redacted",
                "raw_embedding_values": [0.1, 0.2, 0.3],
            },
        )
        before = self.database.stat().st_mtime_ns
        inspector = EventInspector(self.database)
        listing = inspector.list_events(
            camera_id="camera-in",
            branch="Baghdad",
            direction="IN",
            employee="HR-001",
        )
        inspected = inspector.inspect_event(self.event_id)
        after = self.database.stat().st_mtime_ns

        self.assertEqual(before, after)
        self.assertTrue(listing["read_only"])
        self.assertEqual(listing["total"], 1)
        self.assertEqual(listing["items"][0]["employee"], "HR-001")
        serialized = json.dumps(inspected, ensure_ascii=False)
        self.assertNotIn("super-secret-token", serialized)
        self.assertNotIn("[0.1, 0.2, 0.3]", serialized)
        self.assertNotIn("must-also-be-redacted", serialized)
        detail = inspected["operator_actions"][0]["detail"]
        self.assertEqual(detail["token"], "<redacted>")
        self.assertEqual(detail["erp_password_hash"], "<redacted>")
        self.assertEqual(
            detail["raw_embedding_values"],
            "<omitted-biometric-vector>",
        )
        self.assertEqual(
            inspected["event"]["receipt"]["signature"],
            "<redacted>",
        )
        self.assertFalse(inspected["biometric_vectors_exposed"])

    def test_explain_uncertain_delivery_refuses_retry(self):
        decision_id = self.record_decision(accepted=True)
        lease = self.state.acquire_event_lease(
            self.event_id,
            owner="test-worker",
            lease_seconds=30,
            now=1000,
        )
        self.assertTrue(lease.accepted)
        self.state.begin_delivery_attempt(
            self.event_id,
            owner="test-worker",
            decision_id=decision_id,
            lease_seconds=30,
            now=1001,
        )
        outcomes = self.state.recover_expired_event_leases(now=2000)
        self.assertEqual(outcomes[0].outcome, "uncertain")

        explanation = EventInspector(self.database).explain_event(
            self.event_id,
            now=2000,
        )
        self.assertEqual(explanation["current_state"], "uncertain")
        self.assertTrue(explanation["delivery_safety"]["delivery_started"])
        self.assertFalse(explanation["operator_eligibility"]["reprocess"])
        self.assertFalse(explanation["operator_eligibility"]["delivery_retry"])
        self.assertIn("Reconcile", explanation["recommended_action"])

    def test_dismiss_is_atomic_audited_and_requires_reason(self):
        self.reject_event()
        with self.assertRaises(EventOperationError):
            self.state.operator_dismiss_event(
                self.event_id,
                actor="operator@example.com",
                reason="no",
                now=2000,
            )
        result = self.state.operator_dismiss_event(
            self.event_id,
            actor="operator@example.com",
            reason="Reviewed and no attendance action is required.",
            now=2000,
        )
        event = self.state.get_event(self.event_id)
        self.assertEqual(result["state"], "dismissed")
        self.assertEqual(event["lifecycle_state"], "dismissed")
        self.assertEqual(event["final_disposition"], "dismissed")
        self.assertEqual(event["operator_actions"][-1]["action"], "dismissed")
        self.assertEqual(event["transitions"][-1]["actor_type"], "operator")
        self.assertIn(
            "no attendance action",
            event["operator_actions"][-1]["detail"]["operator_reason"],
        )

    def test_active_lease_and_delivery_boundary_block_operator_mutations(self):
        lease = self.state.acquire_event_lease(
            self.event_id,
            owner="live-worker",
            lease_seconds=60,
            now=1000,
        )
        self.assertTrue(lease.accepted)
        with self.assertRaisesRegex(EventOperationError, "active processing lease"):
            self.state.operator_dismiss_event(
                self.event_id,
                actor="operator@example.com",
                reason="Stop this event after investigation.",
                now=1001,
            )

        decision_id = self.record_decision(accepted=True)
        self.state.begin_delivery_attempt(
            self.event_id,
            owner="live-worker",
            decision_id=decision_id,
            lease_seconds=60,
            now=1002,
        )
        with self.assertRaisesRegex(EventOperationError, "delivery boundary"):
            self.state.operator_reprocess_event(
                self.event_id,
                actor="operator@example.com",
                reason="Try the recognition pipeline again.",
                source_path=self.source_path,
                now=2000,
            )

    def test_safe_reprocess_preserves_decisions_and_creates_new_attempt(self):
        self.record_decision(accepted=False, decision_version=1)
        self.reject_event()
        result = self.state.operator_reprocess_event(
            self.event_id,
            actor="operator@example.com",
            reason="Camera obstruction was corrected and evidence was verified.",
            source_path=self.source_path,
            now=2000,
        )
        event = self.state.get_event(self.event_id)
        self.assertEqual(result["state"], "received")
        self.assertEqual(event["lifecycle_state"], "received")
        self.assertIsNone(event["completed_at"])
        self.assertEqual(event["final_disposition"], "")
        self.assertEqual(len(event["decisions"]), 1)
        self.assertEqual(
            event["operator_actions"][-1]["action"],
            "reprocess_requested",
        )
        next_lease = self.state.acquire_event_lease(
            self.event_id,
            owner="replacement-worker",
            lease_seconds=60,
            now=2001,
        )
        self.assertTrue(next_lease.accepted)
        self.assertEqual(next_lease.attempt, 2)

    def move_to_quarantine(self):
        quarantine = self.root / "logs" / "quarantine" / "no_face"
        quarantine.mkdir(parents=True)
        target = quarantine / f"20260814_000000_{self.source_path.name}"
        target_receipt = receipt_path(target)
        os.replace(self.source_path, target)
        os.replace(receipt_path(self.source_path), target_receipt)
        return target

    def test_quarantine_retain_and_requeue_are_audited(self):
        self.reject_event(retention="quarantined")
        quarantine_source = self.move_to_quarantine()
        resolver = EventSourceResolver(self.config_path)
        event = self.state.get_event(self.event_id)
        found = resolver.find_quarantine_source(event)
        self.assertEqual(found, quarantine_source.resolve())
        retained = self.state.operator_record_quarantine_resolution(
            self.event_id,
            actor="operator@example.com",
            reason="Keep this evidence for the approved review window.",
            source_path=found,
            resolution="retain",
            now=2000,
        )
        self.assertEqual(retained["action"], "quarantine_retained")

        move = resolver.requeue_quarantine_source(event)
        self.assertTrue(move.destination_image.is_file())
        self.assertTrue(move.destination_receipt.is_file())
        requeued = self.state.operator_reprocess_event(
            self.event_id,
            actor="operator@example.com",
            reason=(
                "Source and receipt were verified after correcting the rejection cause."
            ),
            source_path=move.destination_image,
            action="quarantine_requeued",
            now=2001,
        )
        self.assertEqual(requeued["state"], "received")
        updated = self.state.get_event(self.event_id)
        self.assertEqual(
            updated["operator_actions"][-1]["action"],
            "quarantine_requeued",
        )
        self.assertEqual(updated["lifecycle_state"], "received")

    def test_read_only_database_symlink_is_rejected(self):
        alias = self.root / "runtime-alias.sqlite3"
        try:
            alias.symlink_to(self.database)
        except (OSError, NotImplementedError):
            self.skipTest("symbolic links are unavailable on this platform")
        with self.assertRaisesRegex(Exception, "symbolic-link"):
            EventInspector(alias).list_events()

    def test_reprocess_requires_exact_source_content_and_companion_receipt(self):
        self.reject_event()
        self.source_path.write_bytes(b"changed-source-evidence")
        with self.assertRaisesRegex(EventOperationError, "SHA-256"):
            self.state.operator_reprocess_event(
                self.event_id,
                actor="operator@example.com",
                reason="The retained source must be verified before reprocessing.",
                source_path=self.source_path,
                now=2000,
            )

        self.source_path.write_bytes(b"event-source-evidence")
        receipt_path(self.source_path).unlink()
        with self.assertRaisesRegex(EventOperationError, "companion receipt"):
            self.state.operator_reprocess_event(
                self.event_id,
                actor="operator@example.com",
                reason="The retained source must include its signed receipt.",
                source_path=self.source_path,
                now=2001,
            )

    def test_processed_without_delivery_can_be_reprocessed(self):
        lease = self.state.acquire_event_lease(
            self.event_id,
            owner="test-worker",
            lease_seconds=60,
            now=1000,
        )
        self.assertTrue(lease.accepted)
        self.state.finalize_event_with_lease(
            self.event_id,
            owner="test-worker",
            to_state="processed",
            reason_code="processed_no_checkin",
            compatibility_status="processed_no_checkin",
            now=1001,
        )
        result = self.state.operator_reprocess_event(
            self.event_id,
            actor="operator@example.com",
            reason="A corrected gallery is available and no delivery was attempted.",
            source_path=self.source_path,
            now=2000,
        )
        self.assertEqual(result["state"], "received")

    def test_quarantine_resolution_requires_quarantined_retention(self):
        self.reject_event(retention="retained")
        with self.assertRaisesRegex(EventOperationError, "retention_state"):
            self.state.operator_record_quarantine_resolution(
                self.event_id,
                actor="operator@example.com",
                reason="Attempted resolution must match retained evidence state.",
                source_path=self.source_path,
                resolution="retain",
                now=2000,
            )

    def test_cli_exposes_no_delivery_retry_or_cancel_command(self):
        parser = __import__("event_admin").build_parser()
        help_text = parser.format_help()
        self.assertNotIn("delivery-retry", help_text)
        self.assertNotIn("delivery-cancel", help_text)

    def test_event_admin_cli_read_only_and_confirmation(self):
        self.record_decision(accepted=True)
        output = io.StringIO()
        with redirect_stdout(output):
            result = event_admin_main(
                [
                    "list",
                    "--database",
                    str(self.database),
                    "--employee",
                    "HR-001",
                    "--limit",
                    "10",
                ]
            )
        self.assertEqual(result, 0)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["read_only"])
        self.assertEqual(payload["total"], 1)

        self.reject_event()
        with self.assertRaises(SystemExit) as raised:
            event_admin_main(
                [
                    "dismiss",
                    self.event_id,
                    "--database",
                    str(self.database),
                    "--actor",
                    "operator@example.com",
                    "--reason",
                    "Reviewed and dismissed safely.",
                ]
            )
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
