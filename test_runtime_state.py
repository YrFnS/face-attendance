import tempfile
import unittest
from pathlib import Path

from runtime_state import RuntimeState, make_event_id


class RuntimeStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = RuntimeState(Path(self.temp.name) / "state.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def test_duplicate_image_for_same_camera_is_blocked(self):
        event_id = make_event_id("camera-in", "IN", "abc")
        first = self.state.claim_event(
            event_id=event_id,
            camera_id="camera-in",
            log_type="IN",
            source_sha256="abc",
            source_name="one.jpg",
            source_mtime=1,
            source_size=10,
        )
        second = self.state.claim_event(
            event_id=event_id,
            camera_id="camera-in",
            log_type="IN",
            source_sha256="abc",
            source_name="two.jpg",
            source_mtime=2,
            source_size=10,
        )
        self.assertTrue(first.accepted)
        self.assertFalse(second.accepted)
        self.assertEqual(second.reason, "duplicate")
        self.assertEqual(second.existing_status, "processing")

    def test_same_image_from_different_camera_is_distinct(self):
        first_id = make_event_id("camera-in", "IN", "abc")
        second_id = make_event_id("camera-out", "OUT", "abc")
        first = self.state.claim_event(
            event_id=first_id,
            camera_id="camera-in",
            log_type="IN",
            source_sha256="abc",
            source_name="one.jpg",
            source_mtime=1,
            source_size=10,
        )
        second = self.state.claim_event(
            event_id=second_id,
            camera_id="camera-out",
            log_type="OUT",
            source_sha256="abc",
            source_name="two.jpg",
            source_mtime=2,
            source_size=10,
        )
        self.assertTrue(first.accepted)
        self.assertTrue(second.accepted)

    def test_event_status_is_persistent(self):
        event_id = make_event_id("camera-in", "IN", "abc")
        self.state.claim_event(
            event_id=event_id,
            camera_id="camera-in",
            log_type="IN",
            source_sha256="abc",
            source_name="one.jpg",
            source_mtime=1,
            source_size=10,
        )
        self.state.finish_event(event_id, status="checkin_created")
        self.assertEqual(self.state.get_event(event_id)["status"], "checkin_created")

    def test_login_failures_lock_key(self):
        for _ in range(3):
            self.state.record_login_failure(
                "127.0.0.1:admin",
                max_attempts=3,
                window_seconds=300,
                lockout_seconds=60,
            )
        allowed, retry = self.state.login_allowed("127.0.0.1:admin")
        self.assertFalse(allowed)
        self.assertGreater(retry, 0)
        self.state.clear_login_failures("127.0.0.1:admin")
        self.assertTrue(self.state.login_allowed("127.0.0.1:admin")[0])

    def test_audit_is_recorded(self):
        self.state.audit(
            actor="admin",
            action="embedding_sync",
            remote_addr="127.0.0.1",
            detail={"changed": True},
        )
        rows = self.state.recent_audit()
        self.assertEqual(rows[0]["actor"], "admin")
        self.assertEqual(rows[0]["detail"]["changed"], True)


if __name__ == "__main__":
    unittest.main()
