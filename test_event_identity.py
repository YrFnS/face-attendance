import hashlib
import tempfile
import unittest
from pathlib import Path

from event_identity import (
    DELIVERY_ID_DOMAIN,
    IDENTITY_CONTRACT_VERSION,
    identity_contract,
    make_capture_id,
    make_delivery_id,
    make_event_id,
    make_recognition_decision_id,
)
from runtime_state import RuntimeState


class EventIdentityTests(unittest.TestCase):
    def test_identifier_algorithms_preserve_existing_event_contract(self):
        content = "a" * 64
        expected_event = hashlib.sha256(
            "\0".join(("camera-in", "IN", content)).encode("utf-8")
        ).hexdigest()
        expected_capture = hashlib.sha256(
            "\0".join(
                ("camera-in", content, "one.jpg", "123", "1000.000000")
            ).encode("utf-8")
        ).hexdigest()
        event_id = make_event_id("camera-in", "IN", content)
        capture_id = make_capture_id(
            "camera-in", content, "one.jpg", 123, 1000.0
        )
        self.assertEqual(event_id, expected_event)
        self.assertEqual(capture_id, expected_capture)

    def test_content_capture_decision_and_delivery_scopes_are_distinct(self):
        content = "b" * 64
        event_id = make_event_id("camera-in", "IN", content)
        first_capture = make_capture_id(
            "camera-in", content, "one.jpg", 123, 1000.0
        )
        second_capture = make_capture_id(
            "camera-in", content, "two.jpg", 123, 1001.0
        )
        other_camera_event = make_event_id("camera-out", "OUT", content)
        decision_v1 = make_recognition_decision_id(event_id, 1, 1)
        decision_v2 = make_recognition_decision_id(event_id, 1, 2)
        second_face = make_recognition_decision_id(event_id, 2, 1)
        delivery_v1 = make_delivery_id(decision_v1)
        delivery_v2 = make_delivery_id(decision_v2)

        self.assertNotEqual(first_capture, second_capture)
        self.assertNotEqual(event_id, other_camera_event)
        self.assertNotEqual(decision_v1, decision_v2)
        self.assertNotEqual(decision_v1, second_face)
        self.assertEqual(delivery_v1, make_delivery_id(decision_v1))
        self.assertNotEqual(delivery_v1, delivery_v2)
        self.assertNotIn(delivery_v1, {content, event_id, decision_v1})

    def test_delivery_id_is_domain_separated_per_decision(self):
        decision_id = "c" * 64
        expected = hashlib.sha256(
            "\0".join((DELIVERY_ID_DOMAIN, decision_id)).encode("utf-8")
        ).hexdigest()
        self.assertEqual(make_delivery_id(decision_id), expected)

    def test_contract_marks_only_delivery_id_as_erpnext_idempotency_key(self):
        contract = identity_contract()
        self.assertEqual(contract["version"], IDENTITY_CONTRACT_VERSION)
        self.assertFalse(
            contract["content_hash"]["erpnext_idempotency_key"]
        )
        self.assertFalse(contract["capture_id"]["erpnext_idempotency_key"])
        self.assertFalse(
            contract["recognition_decision_id"][
                "erpnext_idempotency_key"
            ]
        )
        self.assertTrue(contract["delivery_id"]["erpnext_idempotency_key"])

    def test_event_details_exposes_delivery_id_only_for_accepted_decisions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = RuntimeState(
                root / "runtime.sqlite3",
                backup_dir=root / "backups",
            )
            digest = "d" * 64
            event_id = make_event_id("camera-in", "IN", digest)
            capture_id = make_capture_id(
                "camera-in", digest, "capture.jpg", 123, 1000.0
            )
            claim = state.record_event_receipt(
                event_id=event_id,
                capture_id=capture_id,
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
                source_principal="camera-in",
                source_binding_id="e" * 64,
                policy="IN",
                receipt_state="verified",
                receipt_verified=True,
                receipt_detail={"verified": True},
                policy_version="directional-v1",
            )
            self.assertTrue(claim.accepted)
            accepted_id = state.record_recognition_decision(
                event_id=event_id,
                face_index=1,
                face_count=2,
                bbox=[1, 2, 20, 30],
                face_width=19,
                face_height=28,
                detection_score=0.99,
                best_employee="HR-0001",
                best_score=0.91,
                runner_up_score=0.5,
                score_margin=0.41,
                pad_passed=True,
                accepted=True,
                reason_code="accepted_candidate",
                candidate_log_type="IN",
                retention_state="not_retained",
            )
            state.record_recognition_decision(
                event_id=event_id,
                face_index=2,
                face_count=2,
                bbox=[30, 2, 50, 30],
                face_width=20,
                face_height=28,
                detection_score=0.95,
                pad_passed=True,
                accepted=False,
                reason_code="unknown_employee",
                candidate_log_type="IN",
                retention_state="not_retained",
            )
            event = state.get_event(event_id)
            self.assertEqual(
                event["decisions"][0]["delivery_id"],
                make_delivery_id(accepted_id),
            )
            self.assertEqual(event["decisions"][1]["delivery_id"], "")
            explained = state.explain_event(event_id)
            self.assertEqual(
                explained["identifier_contract"]["version"],
                IDENTITY_CONTRACT_VERSION,
            )
            self.assertEqual(
                explained["decisions"][0]["delivery_id"],
                make_delivery_id(accepted_id),
            )


if __name__ == "__main__":
    unittest.main()
