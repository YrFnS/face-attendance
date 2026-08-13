import hashlib
import json
import pickle
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

from data_contract import (
    GalleryError,
    employee_directory,
    employee_id_from_storage_component,
    employee_storage_component,
    safe_log_message,
    strict_json_loads,
    validate_employee_id,
)
from embedding_gallery import load_gallery, validate_gallery, write_gallery_atomic
from gallery_release import release_scope
from legacy_gallery_converter import convert_legacy_gallery
from secure_sync import sync_gallery


def payload():
    return {
        "schema_version": 1,
        "gallery_version": "contract-v1",
        "generated_at": "2026-08-13T00:00:00Z",
        "model": "buffalo_l",
        "model_version": "approved-v1",
        "dimension": 3,
        "normalized": True,
        "branch": "Baghdad Main",
        "employees": [
            {
                "employee": "HR-EMP-1",
                "employee_name": "One",
                "embeddings": [[1.0, 0.0, 0.0]],
            }
        ],
    }


class NoNetworkSession:
    def __init__(self):
        self.calls = []

    def get(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        raise AssertionError("network request must not be reached")


class EmployeeContractTests(unittest.TestCase):
    def test_safe_ascii_identifier_stays_backward_compatible(self):
        self.assertEqual(employee_storage_component("HR-EMP-1"), "HR-EMP-1")

    def test_unicode_identifier_has_reversible_safe_storage(self):
        employee = "موظف-١٢٣"
        component = employee_storage_component(employee)
        self.assertTrue(component.startswith("e~"))
        self.assertNotIn("/", component)
        self.assertEqual(employee_id_from_storage_component(component), employee)

    def test_path_traversal_control_length_and_spaces_are_rejected(self):
        values = (
            "../EMP-1",
            "EMP/1",
            "EMP\\1",
            "EMP\n1",
            " EMP-1",
            "EMP 1",
            "-EMP-1",
            "A" * 129,
        )
        for value in values:
            with self.subTest(value=value):
                with self.assertRaises(GalleryError):
                    validate_employee_id(value)

    def test_storage_path_remains_within_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = employee_directory(root, "موظف-١٢٣")
            self.assertEqual(path.parent, root.resolve())

    def test_storage_rejects_symlinked_employee_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside"
            outside.mkdir()
            (root / "HR-EMP-1").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(GalleryError, "symbolic link"):
                employee_directory(root, "HR-EMP-1")

    def test_log_text_escapes_newlines_and_bidi_controls(self):
        result = safe_log_message("EMP\n123\u202eevil")
        self.assertNotIn("\n", result)
        self.assertIn("\\u000a", result)
        self.assertIn("\\u202e", result)


class GalleryContractTests(unittest.TestCase):
    def test_valid_contract_is_accepted(self):
        sanitized, _, metadata = validate_gallery(payload())
        self.assertEqual(metadata["employee_count"], 1)
        self.assertEqual(sanitized["branch"], "Baghdad Main")

    def test_gallery_strings_must_be_strings_without_controls(self):
        cases = (
            ("model", 123),
            ("gallery_version", "bad\nversion"),
            ("branch", "bad\u202ebranch"),
            ("employee_name", "bad\rname"),
        )
        for key, value in cases:
            with self.subTest(key=key):
                item = payload()
                if key == "employee_name":
                    item["employees"][0][key] = value
                else:
                    item[key] = value
                with self.assertRaises(GalleryError):
                    validate_gallery(item)

    def test_dimension_must_be_strict_and_bounded(self):
        for value in (True, "3", 3.0, 0, 4097):
            with self.subTest(value=value):
                item = payload()
                item["dimension"] = value
                with self.assertRaises(GalleryError):
                    validate_gallery(item)

    def test_vectors_must_be_json_numbers_with_exact_dimension(self):
        bad_vectors = (
            ["1", 0, 0],
            [True, 0, 0],
            [1, 0],
            "1,0,0",
            [1e20, 0, 0],
        )
        for vector in bad_vectors:
            with self.subTest(vector=vector):
                item = payload()
                item["employees"][0]["embeddings"] = [vector]
                with self.assertRaises(GalleryError):
                    validate_gallery(item)

    def test_employee_template_and_total_counts_are_bounded(self):
        item = payload()
        item["employees"][0]["embeddings"] = [
            [1, 0, 0],
            [0, 1, 0],
        ]
        with self.assertRaisesRegex(GalleryError, "max_embeddings_per_employee"):
            validate_gallery(item, max_embeddings_per_employee=1)
        with self.assertRaisesRegex(GalleryError, "max_total_embeddings"):
            validate_gallery(item, max_total_embeddings=1)

        item = payload()
        item["employees"].append(
            {"employee": "HR-EMP-2", "embeddings": [[0, 1, 0]]}
        )
        with self.assertRaisesRegex(GalleryError, "max_employees"):
            validate_gallery(item, max_employees=1)

    def test_normalized_identifier_duplicates_are_rejected(self):
        item = payload()
        item["employees"] = [
            {"employee": "É-1", "embeddings": [[1, 0, 0]]},
            {"employee": "E\u0301-1", "embeddings": [[0, 1, 0]]},
        ]
        with self.assertRaisesRegex(GalleryError, "duplicate employee"):
            validate_gallery(item)

    def test_release_fields_are_strict(self):
        base_release = {
            "sequence": 1,
            "publisher": "central-enrollment",
            "key_id": "key-2026",
            "algorithm": "ed25519",
            "signature": "A" * 86,
        }
        for field, value in (
            ("sequence", "1"),
            ("publisher", "bad\npublisher"),
            ("key_id", "../key"),
            ("signature", "not-base64="),
        ):
            with self.subTest(field=field):
                item = payload()
                item["release"] = dict(base_release, **{field: value})
                with self.assertRaises(GalleryError):
                    validate_gallery(item)

    def test_unknown_fields_and_checksum_mismatch_are_rejected(self):
        item = payload()
        item["employees"][0]["unexpected"] = "value"
        with self.assertRaisesRegex(GalleryError, "unsupported field"):
            validate_gallery(item)

        item = payload()
        item["checksum"] = "0" * 64
        with self.assertRaisesRegex(GalleryError, "checksum"):
            validate_gallery(item)

    def test_duplicate_json_keys_and_nonfinite_numbers_are_rejected(self):
        with self.assertRaisesRegex(GalleryError, "duplicate key"):
            strict_json_loads('{"model":"a","model":"b"}', field="gallery")
        with self.assertRaisesRegex(GalleryError, "non-finite"):
            strict_json_loads('{"value":NaN}', field="gallery")


class SynchronizationBoundaryTests(unittest.TestCase):
    def base_config(self):
        return {
            "central_url": "https://central.example.test",
            "central_api_token": "secret",
            "branch_name": "Baghdad",
            "model": "buffalo_l",
            "model_version": "approved-v1",
            "embedding_sync_retries": 0,
        }

    def test_invalid_branch_is_rejected_before_network(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = dict(self.base_config(), branch_name="bad\nbranch")
            session = NoNetworkSession()
            with self.assertRaises(GalleryError):
                sync_gallery(
                    cfg,
                    Path(directory) / "gallery.json",
                    Path(directory) / "status.json",
                    session=session,
                    sleep=lambda _: None,
                )
            self.assertEqual(session.calls, [])

    def test_unsafe_endpoint_is_rejected_before_network(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = dict(self.base_config(), embedding_gallery_path="../gallery")
            session = NoNetworkSession()
            with self.assertRaises(GalleryError):
                sync_gallery(
                    cfg,
                    Path(directory) / "gallery.json",
                    Path(directory) / "status.json",
                    session=session,
                    sleep=lambda _: None,
                )
            self.assertEqual(session.calls, [])


class LegacyBoundaryTests(unittest.TestCase):
    def test_legacy_converter_rejects_traversal_employee_before_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "embeddings.pkl"
            raw = pickle.dumps(
                [{"employee": "../escape", "embedding": [1.0, 0.0, 0.0]}]
            )
            source.write_bytes(raw)
            destination = root / "embedding_gallery.json"
            with self.assertRaises(GalleryError):
                convert_legacy_gallery(
                    source=source,
                    destination=destination,
                    config={
                        "model": "buffalo_l",
                        "model_version": "approved-v1",
                        "branch_name": "Baghdad",
                    },
                    expected_sha256=hashlib.sha256(raw).hexdigest(),
                    backup_dir=root / "backup",
                    quarantine_dir=root / "quarantine",
                    acknowledge_risk=True,
                    keep_source=True,
                )
            self.assertFalse(destination.exists())


# face_attendance imports InsightFace at module import time. Tests use a stub;
# production still imports the real package.
insightface = types.ModuleType("insightface")
insightface_app = types.ModuleType("insightface.app")
insightface_app.FaceAnalysis = object
insightface.app = insightface_app
sys.modules.setdefault("insightface", insightface)
sys.modules.setdefault("insightface.app", insightface_app)
import face_attendance  # noqa: E402


class RuntimeBoundaryTests(unittest.TestCase):
    def test_runtime_log_is_single_line(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(face_attendance, "LOGS", Path(directory)):
                face_attendance.log("employee=HR-1\nforged=success")
                text = (Path(directory) / "watch.log").read_text(encoding="utf-8")
            self.assertEqual(text.count("\n"), 1)
            self.assertIn("\\u000a", text)

    def test_unicode_employee_crop_filename_is_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            crop = np.zeros((8, 8, 3), dtype=np.uint8)
            with patch.object(face_attendance, "LOGS", Path(directory)):
                path, temporary = face_attendance.save_checkin_image(
                    crop,
                    "موظف-١٢٣",
                    0.91,
                    {"save_checkin_crops": True, "attach_checkin_crop": False},
                )
            self.assertFalse(temporary)
            self.assertTrue(path.is_file())
            self.assertNotIn("موظف", path.name)
            self.assertEqual(path.parent, Path(directory) / "checkins")

    def test_invalid_employee_never_reaches_erp_request(self):
        cfg = {
            "frappe_url": "https://erp.example.test",
            "frappe_api_key": "key",
            "frappe_api_secret": "secret",
        }
        with patch.object(face_attendance, "load_config", return_value=cfg), patch.object(
            face_attendance.requests, "post"
        ) as post:
            with self.assertRaises(GalleryError):
                face_attendance.create_checkin_api("../escape", "IN")
            post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
