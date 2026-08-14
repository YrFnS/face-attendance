import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from event_ledger import (
    LEDGER_REQUIRED_TRIGGERS,
    make_capture_id,
    make_recognition_decision_id,
)
from runtime_state import RUNTIME_SCHEMA_VERSION, RuntimeState, make_event_id


class EventLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "runtime.sqlite3"
        self.state = RuntimeState(self.database, backup_dir=self.root / "backups")
        self.digest = "a" * 64
        self.event_id = make_event_id("camera-in", "IN", self.digest)
        self.capture_id = make_capture_id(
            "camera-in", self.digest, "capture.jpg", 1234, 1000.0
        )

    def tearDown(self):
        self.temp.cleanup()

    def record_receipt(self):
        return self.state.record_event_receipt(
            event_id=self.event_id,
            capture_id=self.capture_id,
            camera_id="camera-in",
            log_type="IN",
            source_sha256=self.digest,
            source_name="capture.jpg",
            source_mtime=1000.0,
            source_size=1234,
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
            receipt_detail={"verified": True, "remote_ip": "192.0.2.10"},
            policy_version="directional-v1",
        )

    def test_schema_v2_tables_indexes_and_append_only_triggers_exist(self):
        report = self.state.migration_status()
        self.assertTrue(report["ok"], report)
        self.assertEqual(RUNTIME_SCHEMA_VERSION, 2)
        self.assertEqual(report["schema_version"], 2)
        connection = sqlite3.connect(self.database)
        try:
            objects = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'trigger')"
                ).fetchall()
            }
        finally:
            connection.close()
        self.assertIn("recognition_decisions", objects)
        self.assertIn("event_transitions", objects)
        self.assertIn("operator_actions", objects)
        self.assertTrue(set(LEDGER_REQUIRED_TRIGGERS).issubset(objects))

    def test_receipt_transition_decision_and_operator_action_are_explainable(self):
        claim = self.record_receipt()
        self.assertTrue(claim.accepted)
        self.state.transition_event(
            self.event_id,
            to_state="source_verified",
            reason_code="source_verified",
            event_updates={
                "receipt_state": "verified",
                "receipt_verified": True,
                "receipt_json": {"verified": True},
            },
            compatibility_status="processing",
        )
        self.state.transition_event(
            self.event_id,
            to_state="recognizing",
            reason_code="recognition_started",
            event_updates={
                "gallery_version": "gallery-42",
                "gallery_generated_at": "2026-08-14T00:00:00Z",
                "gallery_model": "buffalo_l",
                "gallery_model_version": "approved-v1",
                "recognition_model": "buffalo_l",
                "recognition_model_version": "approved-v1",
                "preprocessing_version": "preprocess-v1",
                "pad_provider": "approved-provider",
                "pad_model": "liveness-v3",
                "policy_version": "directional-v1",
            },
            compatibility_status="processing",
        )
        decision_id = self.state.record_recognition_decision(
            event_id=self.event_id,
            face_index=1,
            face_count=1,
            bbox=[10, 20, 110, 140],
            face_width=100,
            face_height=120,
            detection_score=0.99,
            best_employee="HR-0001",
            best_score=0.91,
            runner_up_score=0.55,
            score_margin=0.36,
            pad_passed=True,
            pad_skipped=False,
            pad_score=0.96,
            pad_provider="approved-provider",
            pad_model="liveness-v3",
            pad_evidence_id="evidence-1",
            pad_binding_id="c" * 64,
            accepted=True,
            reason_code="accepted_candidate",
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
        )
        self.assertEqual(
            decision_id,
            make_recognition_decision_id(self.event_id, 1),
        )
        action_id = self.state.record_operator_action(
            event_id=self.event_id,
            actor="auditor@example.com",
            action="reviewed",
            detail={"note": "verified against source receipt"},
        )
        self.assertEqual(len(action_id), 64)
        self.state.transition_event(
            self.event_id,
            to_state="processed",
            reason_code="processed_no_checkin",
            event_updates={"retention_state": "retained"},
            compatibility_status="processed_no_checkin",
        )

        event = self.state.get_event(self.event_id)
        self.assertEqual(event["capture_id"], self.capture_id)
        self.assertEqual(event["received_at"], "2026-08-14T00:00:00Z")
        self.assertEqual(event["effective_at"], "2026-08-14T00:00:00Z")
        self.assertEqual(event["gallery_version"], "gallery-42")
        self.assertEqual(event["lifecycle_state"], "processed")
        self.assertEqual(event["final_disposition"], "processed_no_checkin")
        self.assertEqual(
            [item["to_state"] for item in event["transitions"]],
            ["received", "source_verified", "recognizing", "processed"],
        )
        self.assertEqual(event["decisions"][0]["decision_id"], decision_id)
        self.assertEqual(event["decisions"][0]["best_employee"], "HR-0001")
        self.assertAlmostEqual(event["decisions"][0]["pad_score"], 0.96)
        self.assertEqual(event["operator_actions"][0]["action_id"], action_id)
        self.assertEqual(
            event["operator_actions"][0]["detail"]["note"],
            "verified against source receipt",
        )

    def test_decisions_transitions_and_actions_cannot_be_changed_directly(self):
        self.record_receipt()
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
            retention_state="not_retained",
        )
        self.state.record_operator_action(
            event_id=self.event_id,
            actor="operator",
            action="reviewed",
        )
        connection = sqlite3.connect(self.database)
        try:
            for statement in (
                "UPDATE recognition_decisions SET best_score = 1",
                "DELETE FROM recognition_decisions",
                "UPDATE event_transitions SET reason_code = 'generic_failed'",
                "DELETE FROM event_transitions",
                "UPDATE operator_actions SET action = 'changed'",
                "DELETE FROM operator_actions",
            ):
                with self.assertRaises(sqlite3.IntegrityError, msg=statement):
                    connection.execute(statement)
                connection.rollback()
        finally:
            connection.close()

    def test_retention_prune_deletes_parent_and_cascades_history(self):
        self.record_receipt()
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
            retention_state="not_retained",
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
        self.assertEqual(self.state.prune_events(1), 1)
        self.assertIsNone(self.state.get_event(self.event_id))
        connection = sqlite3.connect(self.database)
        try:
            decision_count = connection.execute(
                "SELECT COUNT(*) FROM recognition_decisions"
            ).fetchone()[0]
            transition_count = connection.execute(
                "SELECT COUNT(*) FROM event_transitions"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(decision_count, 0)
        self.assertEqual(transition_count, 0)

    def test_receipt_json_is_normalized_and_does_not_store_vectors(self):
        self.record_receipt()
        event = self.state.get_event(self.event_id)
        self.assertEqual(
            json.loads(event["receipt_json"]),
            {"remote_ip": "192.0.2.10", "verified": True},
        )
        serialized = json.dumps(event, ensure_ascii=False)
        self.assertNotIn("embedding", serialized.lower())


if __name__ == "__main__":
    unittest.main()
