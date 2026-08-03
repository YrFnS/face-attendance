import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from embedding_gallery import (
    GalleryError,
    GalleryReloader,
    gallery_status,
    load_gallery,
    sync_gallery,
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


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, headers, params, timeout):
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "params": params,
                "timeout": timeout,
            }
        )
        return FakeResponse(self.payload)


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
        payload = sample_payload()
        payload["dimension"] = 512
        with self.assertRaisesRegex(GalleryError, "dimension mismatch"):
            validate_gallery(payload)

    def test_validate_rejects_empty_gallery(self):
        payload = sample_payload()
        payload["employees"] = []
        with self.assertRaisesRegex(GalleryError, "empty"):
            validate_gallery(payload)

    def test_atomic_write_load_and_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "embedding_gallery.json"
            write_gallery_atomic(
                path,
                sample_payload(),
                expected_model="buffalo_l",
                expected_branch="Baghdad",
            )
            known, metadata, payload = load_gallery(
                path,
                expected_model="buffalo_l",
                expected_branch="Baghdad",
            )
            self.assertEqual(len(known), 2)
            self.assertEqual(metadata["gallery_version"], "test-v1")
            self.assertIn("checksum", payload)

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

            path.write_text('{"schema_version": 1, "employees": []}', encoding="utf-8")
            with self.assertRaises(GalleryError):
                reloader.reload()
            self.assertEqual(reloader.known[0]["employee"], "HR-EMP-1")

    def test_failed_sync_preserves_previous_gallery(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            gallery_path = directory / "embedding_gallery.json"
            status_path = directory / "embedding_sync_status.json"
            cfg = {
                "central_url": "http://127.0.0.1:8088",
                "central_api_token": "secret-token",
                "branch_name": "Baghdad",
                "model": "buffalo_l",
            }
            sync_gallery(
                cfg,
                gallery_path,
                status_path,
                session=FakeSession(sample_payload()),
            )
            invalid = sample_payload()
            invalid["dimension"] = 512
            with self.assertRaises(GalleryError):
                sync_gallery(
                    cfg,
                    gallery_path,
                    status_path,
                    session=FakeSession(invalid),
                )
            _, metadata, _ = load_gallery(gallery_path)
            self.assertEqual(metadata["gallery_version"], "test-v1")

    def test_sync_uses_bearer_token_and_does_not_rewrite_unchanged_gallery(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            gallery_path = directory / "embedding_gallery.json"
            status_path = directory / "embedding_sync_status.json"
            session = FakeSession({"data": sample_payload()})
            cfg = {
                "central_url": "http://127.0.0.1:8088",
                "central_api_token": "secret-token",
                "branch_name": "Baghdad",
                "embedding_gallery_path": "/api/faces/embeddings",
                "model": "buffalo_l",
                "require_model_match": True,
            }

            first = sync_gallery(cfg, gallery_path, status_path, session=session)
            second = sync_gallery(cfg, gallery_path, status_path, session=session)

            self.assertTrue(first["changed"])
            self.assertFalse(second["changed"])
            self.assertEqual(
                session.calls[0]["headers"]["Authorization"],
                "Bearer secret-token",
            )
            self.assertEqual(session.calls[0]["params"], {"branch": "Baghdad"})
            saved_status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertTrue(saved_status["ok"])


    def test_sync_rejects_placeholder_token(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = {
                "central_url": "http://127.0.0.1:8088",
                "central_api_token": "CHANGE_ME",
                "branch_name": "Baghdad",
                "model": "buffalo_l",
            }
            with self.assertRaisesRegex(GalleryError, "non-placeholder"):
                sync_gallery(
                    cfg,
                    Path(directory) / "gallery.json",
                    Path(directory) / "status.json",
                    session=FakeSession(sample_payload()),
                )

    def test_sync_rejects_remote_plain_http(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = {
                "central_url": "http://192.0.2.10:8088",
                "central_api_token": "secret-token",
                "branch_name": "Baghdad",
                "model": "buffalo_l",
            }
            with self.assertRaisesRegex(GalleryError, "HTTPS"):
                sync_gallery(
                    cfg,
                    Path(directory) / "gallery.json",
                    Path(directory) / "status.json",
                    session=FakeSession(sample_payload()),
                )


if __name__ == "__main__":
    unittest.main()
