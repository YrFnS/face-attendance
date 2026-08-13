import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path

import cv2
import numpy as np

from pad import PADGate, PADResult
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
attendance.process_image = lambda *args, **kwargs: False
sys.modules["face_attendance"] = attendance
watch_service = importlib.import_module("watch_service")


class FakeFace:
    def __init__(self, left=1):
        self.bbox = np.array([left, 2, left + 20, 30], dtype=np.float32)
        self.det_score = 0.99
        self.embedding = np.array([1.0, 0.0, 0.0], dtype=np.float32)


class FakeApp:
    def __init__(self, faces):
        self.faces = list(faces)
        self.calls = 0

    def get(self, _image):
        self.calls += 1
        return list(self.faces)


class PassingPAD:
    enabled = True
    required = True
    max_faces = 8
    provider = "http"
    expected_provider = "approved-provider"

    def __init__(self):
        self.calls = []

    def evaluate(self, _crop, context):
        self.calls.append(dict(context))
        return PADResult(
            True,
            0.95,
            "approved-provider",
            evidence_id=f"evidence-{context['face_index']}",
            model="liveness-v3",
            binding_id=str(context["face_index"]) * 64,
            crop_sha256="a" * 64,
            face_index=context["face_index"],
            face_count=context["face_count"],
        )


class StaticGallery:
    def __init__(self, value=None, error=None):
        self.value = value if value is not None else []
        self.error = error

    def refresh(self):
        if self.error:
            raise self.error
        return self.value


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
            "pad_require_single_face": True,
        }
        self.pad_gate = PADGate(
            {
                "pad_provider": "disabled",
                "pad_required": False,
                "pad_fail_closed": True,
            }
        )
        self.original_process_image = attendance.process_image

    def tearDown(self):
        attendance.process_image = self.original_process_image
        self.temp.cleanup()

    def event_id(self):
        digest, _ = file_sha256(self.image_path)
        return make_event_id("camera-in", "IN", digest)

    def test_handled_error_after_claim_is_finalized(self):
        result = watch_service.process_path(
            self.image_path,
            FakeApp([FakeFace()]),
            [],
            StaticGallery(error=ValueError("invalid gallery")),
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
                FakeApp([FakeFace()]),
                [],
                StaticGallery(error=RuntimeError("gallery offline")),
                self.cfg,
                self.state,
                self.pad_gate,
            )
        event = self.state.get_event(self.event_id())
        self.assertEqual(event["status"], "failed")
        self.assertIn("gallery offline", event["error"])
        self.assertIsNotNone(event["completed_at"])

    def test_multiple_faces_are_rejected_before_pad_or_recognition(self):
        app = FakeApp([FakeFace(1), FakeFace(40)])
        pad = PassingPAD()
        called = []
        attendance.process_image = lambda *args, **kwargs: called.append(True) or True
        result = watch_service.process_path(
            self.image_path,
            app,
            [],
            StaticGallery([]),
            self.cfg,
            self.state,
            pad,
        )
        self.assertFalse(result)
        self.assertEqual(app.calls, 1)
        self.assertEqual(pad.calls, [])
        self.assertEqual(called, [])
        event = self.state.get_event(self.event_id())
        self.assertEqual(event["status"], "rejected")
        self.assertIn("pad_expected_one_face_found_2", event["error"])

    def test_recognition_consumes_the_exact_pad_evaluated_face_set(self):
        face = FakeFace(5)
        app = FakeApp([face])
        pad = PassingPAD()
        captured = {}

        def process_image(_image, _source, bound_app, _known, _cfg, _dry_run, **kwargs):
            captured["faces"] = bound_app.get(np.zeros((1, 1, 3), dtype=np.uint8))
            captured["attach_source"] = kwargs.get("attach_source")
            return True

        attendance.process_image = process_image
        result = watch_service.process_path(
            self.image_path,
            app,
            [],
            StaticGallery([{"employee": "HR-1"}]),
            self.cfg,
            self.state,
            pad,
        )
        self.assertTrue(result)
        self.assertEqual(app.calls, 1)
        self.assertEqual(len(pad.calls), 1)
        self.assertIs(captured["faces"][0], face)
        self.assertEqual(pad.calls[0]["face_index"], 1)
        self.assertEqual(pad.calls[0]["face_count"], 1)
        self.assertEqual(pad.calls[0]["bbox"], [5, 2, 25, 30])
        event = self.state.get_event(self.event_id())
        self.assertEqual(event["status"], "checkin_created")

    def test_per_face_mode_requires_every_face_to_pass(self):
        self.cfg["pad_require_single_face"] = False
        faces = [FakeFace(1), FakeFace(40)]

        class MixedPAD(PassingPAD):
            def evaluate(self, crop, context):
                result = super().evaluate(crop, context)
                if context["face_index"] == 2:
                    return PADResult(
                        False,
                        0.2,
                        result.provider,
                        reason="presentation_attack",
                        evidence_id=result.evidence_id,
                        model=result.model,
                        binding_id=result.binding_id,
                        crop_sha256=result.crop_sha256,
                        face_index=result.face_index,
                        face_count=result.face_count,
                    )
                return result

        pad = MixedPAD()
        called = []
        attendance.process_image = lambda *args, **kwargs: called.append(True) or True
        result = watch_service.process_path(
            self.image_path,
            FakeApp(faces),
            [],
            StaticGallery([]),
            self.cfg,
            self.state,
            pad,
        )
        self.assertFalse(result)
        self.assertEqual(len(pad.calls), 2)
        self.assertEqual(called, [])
        event = self.state.get_event(self.event_id())
        self.assertEqual(event["status"], "rejected")
        self.assertIn("2:presentation_attack", event["error"])


if __name__ == "__main__":
    unittest.main()
