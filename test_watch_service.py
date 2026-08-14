import importlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

import cv2
import numpy as np

from camera_sources import (
    load_camera_sources,
    receipt_path,
    source_by_username,
    write_source_receipt,
)
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
            "production_mode": False,
            "branch_name": "Baghdad",
            "camera_uploads_dir": str(self.root / "camera_uploads"),
            "camera_source_receipt_required": True,
            "camera_source_receipt_secret": "receipt-secret-" + "x" * 32,
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
                    "upload_dir": str(self.image_path.parent),
                    "allowed_networks": ["192.0.2.10/32"],
                }
            },
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
        self.sources = load_camera_sources(self.cfg, self.root)
        self.write_receipt()
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

    def write_receipt(self):
        digest, size = file_sha256(self.image_path)
        source = source_by_username(self.sources, "camera_in")
        write_source_receipt(
            self.image_path,
            source,
            self.cfg,
            remote_ip="192.0.2.10",
            source_sha256=digest,
            source_size=size,
        )

    def event_id(self):
        digest, _ = file_sha256(self.image_path)
        return make_event_id("camera-in", "IN", digest)

    def test_missing_receipt_is_claimed_and_rejected_before_recognition(self):
        receipt_path(self.image_path).unlink()
        app = FakeApp([FakeFace()])
        result = watch_service.process_path(
            self.image_path,
            app,
            [],
            StaticGallery([]),
            self.cfg,
            self.state,
            self.pad_gate,
            sources=self.sources,
        )
        self.assertFalse(result)
        self.assertEqual(app.calls, 0)
        event = self.state.get_event(self.event_id())
        self.assertEqual(event["status"], "rejected")
        self.assertIn("source_binding", event["error"])
        self.assertIn("receipt is missing", event["error"])

    def test_tampered_receipt_is_rejected_before_pad_or_recognition(self):
        data = json.loads(receipt_path(self.image_path).read_text(encoding="utf-8"))
        data["ftp_username"] = "other-camera"
        receipt_path(self.image_path).write_text(json.dumps(data), encoding="utf-8")
        app = FakeApp([FakeFace()])
        pad = PassingPAD()
        result = watch_service.process_path(
            self.image_path,
            app,
            [],
            StaticGallery([]),
            self.cfg,
            self.state,
            pad,
            sources=self.sources,
        )
        self.assertFalse(result)
        self.assertEqual(app.calls, 0)
        self.assertEqual(pad.calls, [])
        event = self.state.get_event(self.event_id())
        self.assertEqual(event["status"], "rejected")
        self.assertIn("ftp_username", event["error"])

    def test_handled_error_after_claim_is_finalized(self):
        result = watch_service.process_path(
            self.image_path,
            FakeApp([FakeFace()]),
            [],
            StaticGallery(error=ValueError("invalid gallery")),
            self.cfg,
            self.state,
            self.pad_gate,
            sources=self.sources,
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
                sources=self.sources,
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
            sources=self.sources,
        )
        self.assertFalse(result)
        self.assertEqual(app.calls, 1)
        self.assertEqual(pad.calls, [])
        self.assertEqual(called, [])
        event = self.state.get_event(self.event_id())
        self.assertEqual(event["status"], "rejected")
        self.assertIn("pad_expected_one_face_found_2", event["error"])

    def test_recognition_consumes_exact_pad_faces_and_bound_source_context(self):
        face = FakeFace(5)
        app = FakeApp([face])
        pad = PassingPAD()
        captured = {}

        def process_image(_image, source_text, bound_app, _known, event_cfg, _dry_run, **kwargs):
            captured["faces"] = bound_app.get(np.zeros((1, 1, 3), dtype=np.uint8))
            captured["attach_source"] = kwargs.get("attach_source")
            captured["source_text"] = source_text
            captured["log_type"] = event_cfg["log_type"]
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
            sources=self.sources,
        )
        self.assertTrue(result)
        self.assertEqual(app.calls, 1)
        self.assertEqual(len(pad.calls), 1)
        self.assertIs(captured["faces"][0], face)
        self.assertEqual(pad.calls[0]["face_index"], 1)
        self.assertEqual(pad.calls[0]["face_count"], 1)
        self.assertEqual(pad.calls[0]["bbox"], [5, 2, 25, 30])
        self.assertEqual(pad.calls[0]["branch"], "Baghdad")
        self.assertEqual(pad.calls[0]["source_principal"], "camera_in")
        self.assertEqual(pad.calls[0]["source_remote_ip"], "192.0.2.10")
        self.assertIn("principal=camera_in", captured["source_text"])
        self.assertIn("ip=192.0.2.10", captured["source_text"])
        self.assertEqual(captured["log_type"], "IN")
        self.assertIsNone(captured["attach_source"])
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
            sources=self.sources,
        )
        self.assertFalse(result)
        self.assertEqual(len(pad.calls), 2)
        self.assertEqual(called, [])
        event = self.state.get_event(self.event_id())
        self.assertEqual(event["status"], "rejected")
        self.assertIn("2:presentation_attack", event["error"])


if __name__ == "__main__":
    unittest.main()
