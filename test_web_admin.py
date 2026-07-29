import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import flask  # noqa: F401
except ModuleNotFoundError:
    flask = None

if flask is not None:
    import web_admin
    from embedding_gallery import write_gallery_atomic



def payload():
    return {
        "schema_version": 1,
        "gallery_version": "web-test",
        "generated_at": "2026-07-29T00:00:00Z",
        "model": "buffalo_l",
        "model_version": "",
        "dimension": 3,
        "normalized": True,
        "branch": "Baghdad",
        "employees": [
            {"employee": "HR-EMP-1", "embeddings": [[1.0, 0.0, 0.0]]}
        ],
    }


@unittest.skipIf(flask is None, "Flask dependency is not installed")
class WebAdminTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config = root / "config.json"
        self.gallery = root / "embedding_gallery.json"
        self.status = root / "embedding_sync_status.json"
        self.faces = root / "faces"
        self.config.write_text(
            json.dumps(
                {
                    "branch_name": "Baghdad",
                    "model": "buffalo_l",
                    "embedding_export_enabled": True,
                    "embedding_export_token": "secret",
                    "local_enrollment_enabled": False,
                }
            ),
            encoding="utf-8",
        )
        write_gallery_atomic(
            self.gallery,
            payload(),
            expected_model="buffalo_l",
            expected_branch="Baghdad",
        )
        self.patches = [
            patch.object(web_admin, "CONFIG", self.config),
            patch.object(web_admin, "GALLERY", self.gallery),
            patch.object(web_admin, "SYNC_STATUS", self.status),
            patch.object(web_admin, "FACES", self.faces),
        ]
        for item in self.patches:
            item.start()
        web_admin.app.config.update(TESTING=True)
        self.client = web_admin.app.test_client()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_export_requires_token(self):
        response = self.client.get("/api/faces/embeddings?branch=Baghdad")
        self.assertEqual(response.status_code, 401)

    def test_export_returns_validated_gallery(self):
        response = self.client.get(
            "/api/faces/embeddings?branch=Baghdad",
            headers={"Authorization": "Bearer secret"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["gallery_version"], "web-test")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_export_rejects_wrong_branch(self):
        response = self.client.get(
            "/api/faces/embeddings?branch=Basra",
            headers={"Authorization": "Bearer secret"},
        )
        self.assertEqual(response.status_code, 404)

    def test_upload_is_disabled_on_attendance_server(self):
        response = self.client.post("/upload", data={"employee": "HR-EMP-1"})
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
