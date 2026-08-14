import sqlite3
import tempfile
import unittest
from pathlib import Path

from event_identity import (
    CAPTURE_ID_SCHEME,
    CONTENT_HASH_ALGORITHM,
    DECISION_ID_SCHEME,
    DEFAULT_DELIVERY_CONTRACT_VERSION,
    DELIVERY_ID_SCHEME,
    EVENT_ID_SCHEME,
    EventIdentityError,
    content_sha256,
    identifier_semantics,
    make_capture_id,
    make_delivery_id,
    make_event_id,
    make_recognition_decision_id,
)
from runtime_state import RuntimeState


class EventIdentityTests(unittest.TestCase):
    def test_identifier_domains_are_stable_and_distinct(self):
        digest = "a" * 64
        capture = make_capture_id("camera-in", digest, "event.jpg", 321, 1000.5)
        event = make_event_id("camera-in", "IN", digest)
        decision = make_recognition_decision_id(event, 1, 1)
        delivery = make_delivery_id(decision)

        for value in (capture, event, decision, delivery):
            self.assertEqual(len(value), 64)
            int(value, 16)
        self.assertEqual(len({capture, event, decision, delivery}), 4)
        self.assertEqual(
            capture,
            "cef5ce6862b89e3ee9e710abbcbed3b46400aaa3602c43905dac39d10cb9e874",
        )
        self.assertEqual(
            event,
            "72f944a4b807b5b6c6fc1a51c3b6235a829c2f8a7c70e20b4a4033e34c2bd082",
        )
        self.assertEqual(
            decision,
            "eb3771073b57ec8e488ea0bd51a20eddeb7b78b404d4f435b3642942c1844187",
        )
        self.assertEqual(
            delivery,
            "72625c709c066f0c723b219cde5062578092a93bf62d0909c8c9522bcd35c4e8",
        )
        self.assertEqual(event, make_event_id("camera-in", "IN", digest))
        self.assertNotEqual(event, make_event_id("camera-in", "OUT", digest))
        self.assertNotEqual(
            capture,
            make_capture_id("camera-in", digest, "event-2.jpg", 321, 1000.5),
        )

    def test_content_hash_is_exact_uploaded_bytes(self):
        self.assertEqual(
            content_sha256(b"face-attendance-fixture"),
            "5fe651936151819787a1f0bfcb4803b7a5dc4138e7b56b5050e9f2fd7f8cf17d",
        )
        self.assertNotEqual(
            content_sha256(b"face-attendance-fixture"),
            content_sha256(b"face-attendance-fixture\n"),
        )
        with self.assertRaises(EventIdentityError):
            content_sha256("not-bytes")

    def test_decision_and_delivery_ids_are_per_face_and_attempt(self):
        event = make_event_id("camera-in", "IN", "b" * 64)
        first_face = make_recognition_decision_id(event, 1, 1)
        second_face = make_recognition_decision_id(event, 2, 1)
        retried_first_face = make_recognition_decision_id(event, 1, 2)
        self.assertEqual(len({first_face, second_face, retried_first_face}), 3)

        first_delivery = make_delivery_id(first_face)
        second_delivery = make_delivery_id(second_face)
        other_contract = make_delivery_id(first_face, "erpnext-employee-checkin-v2")
        self.assertNotEqual(first_delivery, second_delivery)
        self.assertNotEqual(first_delivery, other_contract)

    def test_invalid_identity_inputs_fail_closed(self):
        with self.assertRaises(EventIdentityError):
            make_event_id("camera-in", "PRESENCE", "a" * 64)
        with self.assertRaises(EventIdentityError):
            make_recognition_decision_id("x", 1, 1)
        with self.assertRaises(EventIdentityError):
            make_recognition_decision_id("a" * 64, 0, 1)
        with self.assertRaises(EventIdentityError):
            make_delivery_id("a" * 64, "")

    def test_identifier_semantics_are_explicit(self):
        semantics = identifier_semantics()
        self.assertEqual(semantics["content_hash"]["algorithm"], CONTENT_HASH_ALGORITHM)
        self.assertEqual(semantics["capture_id"]["scheme"], CAPTURE_ID_SCHEME)
        self.assertEqual(semantics["event_id"]["scheme"], EVENT_ID_SCHEME)
        self.assertEqual(semantics["decision_id"]["scheme"], DECISION_ID_SCHEME)
        self.assertEqual(semantics["delivery_id"]["scheme"], DELIVERY_ID_SCHEME)
        self.assertEqual(
            semantics["delivery_id"]["contract_version"],
            DEFAULT_DELIVERY_CONTRACT_VERSION,
        )

    def test_accepted_decision_receives_future_delivery_id(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = RuntimeState(root / "state.sqlite3", backup_dir=root / "backups")
            digest = "c" * 64
            event_id = make_event_id("camera-in", "IN", digest)
            capture_id = make_capture_id(
                "camera-in", digest, "capture.jpg", 100, 1000.0
            )
            claim = state.record_event_receipt(
                event_id=event_id,
                capture_id=capture_id,
                camera_id="camera-in",
                log_type="IN",
                source_sha256=digest,
                source_name="capture.jpg",
                source_mtime=1000.0,
                source_size=100,
                received_at="2026-08-14T00:00:00Z",
                effective_at="2026-08-14T00:00:00Z",
                branch="Baghdad",
                source_type="holowits_ftp",
                source_principal="camera_in",
                source_binding_id="d" * 64,
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
                bbox=[1, 2, 30, 40],
                face_width=29,
                face_height=38,
                detection_score=0.99,
                best_employee="HR-0001",
                best_score=0.9,
                runner_up_score=0.3,
                score_margin=0.6,
                pad_passed=True,
                pad_skipped=False,
                accepted=True,
                reason_code="accepted_candidate",
                candidate_log_type="IN",
                retention_state="not_retained",
            )
            rejected_id = state.record_recognition_decision(
                event_id=event_id,
                face_index=2,
                face_count=2,
                bbox=[40, 2, 70, 40],
                face_width=30,
                face_height=38,
                detection_score=0.98,
                accepted=False,
                reason_code="unknown_employee",
                candidate_log_type="IN",
                retention_state="not_retained",
            )
            connection = sqlite3.connect(root / "state.sqlite3")
            connection.row_factory = sqlite3.Row
            try:
                accepted = dict(
                    connection.execute(
                        "SELECT decision_id_scheme, delivery_id, "
                        "delivery_id_scheme, delivery_contract_version "
                        "FROM recognition_decisions WHERE decision_id = ?",
                        (accepted_id,),
                    ).fetchone()
                )
                rejected = dict(
                    connection.execute(
                        "SELECT decision_id_scheme, delivery_id, "
                        "delivery_id_scheme, delivery_contract_version "
                        "FROM recognition_decisions WHERE decision_id = ?",
                        (rejected_id,),
                    ).fetchone()
                )
            finally:
                connection.close()
            self.assertEqual(accepted["decision_id_scheme"], DECISION_ID_SCHEME)
            self.assertEqual(accepted["delivery_id"], make_delivery_id(accepted_id))
            self.assertEqual(accepted["delivery_id_scheme"], DELIVERY_ID_SCHEME)
            self.assertEqual(
                accepted["delivery_contract_version"],
                DEFAULT_DELIVERY_CONTRACT_VERSION,
            )
            self.assertEqual(rejected["decision_id_scheme"], DECISION_ID_SCHEME)
            self.assertEqual(rejected["delivery_id"], "")
            self.assertEqual(rejected["delivery_id_scheme"], "")
            self.assertEqual(rejected["delivery_contract_version"], "")


if __name__ == "__main__":
    unittest.main()
