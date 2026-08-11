import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path

import cv2
import numpy as np

from pad import PADGate
from runtime_state import RuntimeState, file_sha256, make_event_id


attendance = types.ModuleType("face_attendance")
attendance.LOGS = Path(tempfile.gettempdir()) / "face-attendance-test-logs"
attendance.wait_until_stable = lambda path: True
attendance.log_messages = []
attendance.log = attendance.log_messages.append
attendance.scaled_frame = lambda image, cfg: image
attendance.face_size = lambda face: (100, 100)
attendance.face_crop = lambda image, face, margin=0.25: image
attendance.save_rejected = lambda *args, **kwargs: None
attendance.image_files = lambda folder: []
attendance.load_config = lambda: {}
attendance.cleanup_old_audit_files = lambda cfg: None
sys.modules["face_attendance"] = attendance
watch_service = importlib.import_module("watch_service")


class BrokenGallery:
    def __init__(self, error):
        self.error = error

    def refresh(self):
        raise self.error


class WatchServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        attendance.LOGS = self.root / "logs"
        attendance.log_messages.clear()
        self.image_path = self.root / "camera_uploads" / "in" / "event.jpg"
        self.image_path.parent.mkdir(parents=True)
        self.assertTrue(
            cv2.imwrite(str(self.image_path), np.zeros((32, 32, 3), dtype=np.uint8))
        )
        self.state = RuntimeState(self.root / "runtime.sqlite3")
        self.cfg = {
            "folder_log_types": {"in": "IN", "out": "OUT"},
            "camera_ids": {"in": "camera-in", "out": "camera-out"},
            "max_camera_event_age_seconds": 0,
            "camera_event_future_tolerance_seconds": 60,
            "max_camera_upload_bytes": 1024 * 1024,
            "max_camera_image_pixels": 1_000_000,
            "quarantine_invalid_uploads": False,
            "delete_rejected_camera_uploads": False,
            "delete_camera_uploads_after_processing": False,
            "delete_duplicate_camera_uploads": False,
        }
        self.pad_gate = PADGate(
            {
                "pad_provider": "disabled",
                "pad_required": False,
                "pad_fail_closed": True,
            }
        )

    def tearDown(self):
        self.temp.cleanup()

    def event_id(self):
        digest, _ = file_sha256(self.image_path)
        return make_event_id("camera-in", "IN", digest)

    def test_handled_error_after_claim_is_finalized(self):
        result = watch_service.process_path(
            self.image_path,
            object(),
            [],
            BrokenGallery(ValueError("invalid gallery")),
            self.cfg,
            self.state,
            self.pad_gate,
        )
        self.assertFalse(result)
        event = self.state.get_event(self.event_id())
        self.assertEqual(event["status"], "failed")
        self.assertIn("invalid gallery", event["error"])
        self.assertIsNotNone(event["completed_at"])

    def test_unexpected_error_after_claim_is_finalized_before_reraise(self):
        with self.assertRaisesRegex(RuntimeError, "gallery offline"):
            watch_service.process_path(
                self.image_path,
                object(),
                [],
                BrokenGallery(RuntimeError("gallery offline")),
                self.cfg,
                self.state,
                self.pad_gate,
            )
        event = self.state.get_event(self.event_id())
        self.assertEqual(event["status"], "failed")
        self.assertIn("gallery offline", event["error"])
        self.assertIsNotNone(event["completed_at"])


if __name__ == "__main__":
    unittest.main()
