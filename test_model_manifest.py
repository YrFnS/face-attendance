import json
import tempfile
import unittest
from pathlib import Path

from model_manifest import (
    build_manifest,
    insightface_root_for_model_directory,
    runtime_model_binding,
    verify_manifest,
    write_manifest_atomic,
)


class ModelManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.insightface_root = self.root / "insightface"
        self.model_dir = (
            self.insightface_root / "models" / "licensed_model"
        )
        self.model_dir.mkdir(parents=True)
        (self.model_dir / "detector.onnx").write_bytes(b"detector")
        (self.model_dir / "recognition.onnx").write_bytes(
            b"recognition"
        )
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
        self.assertEqual(
            Path(result["insightface_root"]), self.insightface_root
        )

    def test_runtime_binding_derives_native_root(self):
        binding = runtime_model_binding(
            {
                "model": "licensed_model",
                "model_version": "v1",
                "model_directory": str(self.model_dir),
            },
            self.root,
        )
        self.assertEqual(
            Path(binding["insightface_root"]), self.insightface_root
        )
        self.assertEqual(
            Path(binding["model_directory"]), self.model_dir
        )

    def test_non_native_layout_is_rejected(self):
        other = self.root / "licensed_model"
        other.mkdir()
        with self.assertRaisesRegex(ValueError, "root/models/<model>"):
            insightface_root_for_model_directory(
                other, "licensed_model"
            )

    def test_changed_model_file_is_rejected(self):
        self.create()
        (self.model_dir / "recognition.onnx").write_bytes(
            b"changed"
        )
        result = verify_manifest(
            self.path,
            expected_model="licensed_model",
            expected_model_version="v1",
            expected_model_directory=self.model_dir,
            expected_license_reference="contract-123",
        )
        self.assertFalse(result["ok"])
        self.assertTrue(
            any(
                "SHA-256 mismatch" in item
                for item in result["errors"]
            )
        )

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
        self.assertTrue(
            any("unlisted" in item for item in result["errors"])
        )

    def test_skip_hash_still_rejects_size_mismatch(self):
        manifest = self.create()
        manifest["files"][0]["size"] += 1
        write_manifest_atomic(self.path, manifest)
        result = verify_manifest(
            self.path,
            expected_model="licensed_model",
            expected_model_directory=self.model_dir,
            expected_license_reference="contract-123",
            verify_files=False,
        )
        self.assertFalse(result["ok"])
        self.assertFalse(result["hashes_verified"])
        self.assertTrue(
            any(
                "size mismatch" in item
                for item in result["errors"]
            )
        )

    def test_placeholder_license_reference_is_rejected(self):
        with self.assertRaises(ValueError):
            build_manifest(
                model_directory=self.model_dir,
                model="licensed_model",
                license_reference="CHANGE_ME",
            )


if __name__ == "__main__":
    unittest.main()
