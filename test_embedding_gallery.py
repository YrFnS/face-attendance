import tempfile
import unittest
from pathlib import Path

import numpy as np

from embedding_gallery import (
    GalleryError,
    GalleryReloader,
    gallery_status,
    load_gallery,
    validate_gallery,
    write_gallery_atomic,
)


def sample_payload():
    return {
        "schema_version": 1,
        "gallery_version": "test-v1",
        "generated_at": "2026-07-29T00:00:00Z",
        "model": "buffalo_l",
        "model_version": "test",
        "dimension": 3,
        "normalized": False,
        "branch": "Baghdad",
        "employees": [
            {
                "employee": "HR-EMP-1",
                "employee_name": "One",
                "embeddings": [[2.0, 0.0, 0.0], [1.0, 0.0, 0.0]],
            },
            {
                "employee": "HR-EMP-2",
                "employee_name": "Two",
                "embeddings": [[0.0, 3.0, 0.0]],
            },
        ],
    }


class EmbeddingGalleryTests(unittest.TestCase):
    def test_validate_normalizes_vectors(self):
        sanitized, known, metadata = validate_gallery(
            sample_payload(),
            expected_model="buffalo_l",
            expected_branch="Baghdad",
        )
        self.assertTrue(sanitized["normalized"])
        self.assertEqual(metadata["employee_count"], 2)
        self.assertEqual(metadata["embedding_count"], 3)
        self.assertAlmostEqual(float(np.linalg.norm(known[0]["embeddings"][0])), 1.0)

    def test_validate_rejects_wrong_model(self):
        with self.assertRaisesRegex(GalleryError, "model mismatch"):
            validate_gallery(sample_payload(), expected_model="antelopev2")

    def test_validate_rejects_wrong_model_version_when_required(self):
        with self.assertRaisesRegex(GalleryError, "model version mismatch"):
            validate_gallery(
                sample_payload(),
                expected_model_version="different-build",
                require_model_version_match=True,
            )

    def test_validate_rejects_wrong_branch(self):
        with self.assertRaisesRegex(GalleryError, "branch mismatch"):
            validate_gallery(sample_payload(), expected_branch="Basra")

    def test_validate_rejects_bad_dimension(self):
        data = sample_payload()
        data["dimension"] = 512
        with self.assertRaisesRegex(GalleryError, "dimension mismatch"):
            validate_gallery(data)

    def test_validate_rejects_empty_gallery(self):
        data = sample_payload()
        data["employees"] = []
        with self.assertRaisesRegex(GalleryError, "empty"):
            validate_gallery(data)

    def test_atomic_write_load_and_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "embedding_gallery.json"
            write_gallery_atomic(
                path,
                sample_payload(),
                expected_model="buffalo_l",
                expected_branch="Baghdad",
            )
            known, metadata, saved = load_gallery(
                path,
                expected_model="buffalo_l",
                expected_branch="Baghdad",
            )
            self.assertEqual(len(known), 2)
            self.assertEqual(metadata["gallery_version"], "test-v1")
            self.assertIn("checksum", saved)

            reloader = GalleryReloader(
                path,
                expected_model="buffalo_l",
                expected_branch="Baghdad",
            )
            _, _, changed = reloader.reload(force=True)
            self.assertTrue(changed)
            _, _, changed = reloader.reload()
            self.assertFalse(changed)

            status = gallery_status(path, max_age_seconds=86400)
            self.assertTrue(status["available"])
            self.assertFalse(status["stale"])

    def test_invalid_replacement_does_not_destroy_loaded_gallery(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "embedding_gallery.json"
            write_gallery_atomic(path, sample_payload())
            reloader = GalleryReloader(path)
            known, _, _ = reloader.reload(force=True)
            self.assertEqual(known[0]["employee"], "HR-EMP-1")

            path.write_text(
                '{"schema_version": 1, "employees": []}', encoding="utf-8"
            )
            with self.assertRaises(GalleryError):
                reloader.reload()
            self.assertEqual(reloader.known[0]["employee"], "HR-EMP-1")


if __name__ == "__main__":
    unittest.main()
