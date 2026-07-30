import json
import tempfile
import unittest
from pathlib import Path

from model_manifest import build_manifest, verify_manifest, write_manifest_atomic


class ModelManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.model_dir = self.root / "models" / "licensed_model"
        self.model_dir.mkdir(parents=True)
        (self.model_dir / "detector.onnx").write_bytes(b"detector")
        (self.model_dir / "recognition.onnx").write_bytes(b"recognition")
        self.path = self.root / "model_manifest.json"

    def tearDown(self):
        self.temp.cleanup()

    def create(self):
        manifest = build_manifest(
            model_directory=self.model_dir,
            model="licensed_model",
            model_version="v1",
            license_reference="contract-123",
        )
        write_manifest_atomic(self.path, manifest)
        return manifest

    def test_manifest_round_trip(self):
        self.create()
        result = verify_manifest(
            self.path,
            expected_model="licensed_model",
            expected_model_version="v1",
            expected_model_directory=self.model_dir,
            expected_license_reference="contract-123",
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["verified_file_count"], 2)

    def test_changed_model_file_is_rejected(self):
        self.create()
        (self.model_dir / "recognition.onnx").write_bytes(b"changed")
        result = verify_manifest(
            self.path,
            expected_model="licensed_model",
            expected_model_version="v1",
            expected_model_directory=self.model_dir,
            expected_license_reference="contract-123",
        )
        self.assertFalse(result["ok"])
        self.assertTrue(any("SHA-256 mismatch" in item for item in result["errors"]))

    def test_unlisted_file_is_rejected(self):
        self.create()
        (self.model_dir / "extra.bin").write_bytes(b"extra")
        result = verify_manifest(
            self.path,
            expected_model="licensed_model",
            expected_model_directory=self.model_dir,
            expected_license_reference="contract-123",
        )
        self.assertFalse(result["ok"])
        self.assertTrue(any("unlisted" in item for item in result["errors"]))

    def test_placeholder_license_reference_is_rejected(self):
        with self.assertRaises(ValueError):
            build_manifest(
                model_directory=self.model_dir,
                model="licensed_model",
                license_reference="CHANGE_ME",
            )


if __name__ == "__main__":
    unittest.main()
