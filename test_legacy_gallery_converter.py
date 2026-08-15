import ast
import hashlib
import pickle
import tempfile
import unittest
from pathlib import Path

from embedding_gallery import GalleryError, load_gallery
from legacy_gallery_converter import convert_legacy_gallery


class LegacyGalleryConverterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "embeddings.pkl"
        self.destination = self.root / "embedding_gallery.json"
        self.backup = self.root / "backups"
        self.quarantine = self.root / "quarantine"
        self.config = {
            "model": "buffalo_l",
            "model_version": "test-build",
            "branch_name": "Baghdad",
            "require_model_version_match": True,
        }
        self.records = [
            {
                "employee": "HR-EMP-1",
                "employee_name": "One",
                "embedding": [1.0, 0.0, 0.0],
            }
        ]
        self.source.write_bytes(pickle.dumps(self.records))
        self.digest = hashlib.sha256(self.source.read_bytes()).hexdigest()

    def tearDown(self):
        self.temp.cleanup()

    def convert(self, **overrides):
        options = {
            "source": self.source,
            "destination": self.destination,
            "config": self.config,
            "expected_sha256": self.digest,
            "backup_dir": self.backup,
            "quarantine_dir": self.quarantine,
            "acknowledge_risk": True,
        }
        options.update(overrides)
        return convert_legacy_gallery(**options)

    def test_conversion_backs_up_validates_and_quarantines_source(self):
        result = self.convert()
        self.assertFalse(self.source.exists())
        self.assertTrue(Path(result["backup_path"]).exists())
        self.assertTrue(Path(result["quarantine_path"]).exists())
        self.assertTrue(self.destination.exists())
        _, metadata, _ = load_gallery(
            self.destination,
            expected_model="buffalo_l",
            expected_model_version="test-build",
            expected_branch="Baghdad",
            require_model_version_match=True,
        )
        self.assertEqual(metadata["employee_count"], 1)
        self.assertEqual(metadata["embedding_count"], 1)
        self.assertIn(self.digest[:12], metadata["gallery_version"])

    def test_hash_mismatch_refuses_before_backup_or_deserialization(self):
        with self.assertRaisesRegex(GalleryError, "SHA-256 mismatch"):
            self.convert(expected_sha256="0" * 64)
        self.assertTrue(self.source.exists())
        self.assertFalse(self.destination.exists())
        self.assertFalse(self.backup.exists())

    def test_explicit_acknowledgement_is_required(self):
        with self.assertRaisesRegex(GalleryError, "explicit acknowledgement"):
            self.convert(acknowledge_risk=False)
        self.assertFalse(self.destination.exists())

    def test_existing_destination_is_never_overwritten(self):
        self.destination.write_text("existing", encoding="utf-8")
        with self.assertRaisesRegex(GalleryError, "refusing to overwrite"):
            self.convert()
        self.assertEqual(self.destination.read_text(encoding="utf-8"), "existing")

    def test_face_attendance_has_no_pickle_import_or_deserialization(self):
        source_path = Path(__file__).with_name("face_attendance.py")
        if not source_path.exists():
            self.skipTest("face_attendance.py is not present in this isolated test tree")
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        self.assertNotIn("pickle", imported)
        self.assertNotIn("pickle.loads", source)


if __name__ == "__main__":
    unittest.main()
