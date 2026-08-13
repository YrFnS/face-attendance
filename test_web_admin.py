import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

try:
    import flask  # noqa: F401
except ModuleNotFoundError:
    flask = None

if flask is not None:
    import web_admin
    from embedding_gallery import write_gallery_atomic
    from web_security import hash_password


def payload():
    return {
        "schema_version": 1,
        "gallery_version": "web-test",
        "generated_at": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "model": "buffalo_l",
        "model_version": "v1",
        "dimension": 3,
        "normalized": True,
        "branch": "Baghdad",
        "employees": [{"employee": "HR-EMP-1", "embeddings": [[1.0, 0.0, 0.0]]}],
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
        self.runtime = root / "runtime_state.sqlite3"
        self.config.write_text(json.dumps({
            "branch_name": "Baghdad",
            "model": "buffalo_l",
            "model_version": "v1",
            "require_model_match": True,
            "require_model_version_match": True,
            "reject_stale_embedding_gallery": True,
            "embedding_max_age_seconds": 3600,
            "embedding_export_enabled": True,
            "embedding_export_token": "secret",
            "local_enrollment_enabled": False,
            "web_admin_username": "admin",
            "web_admin_password_hash": hash_password("correct horse battery staple"),
            "web_session_secret": "s" * 48,
            "web_cookie_secure": False,
            "web_hsts_enabled": False,
            "runtime_state_db": str(self.runtime),
            "model_license_acknowledged": True,
        }), encoding="utf-8")
        write_gallery_atomic(self.gallery, payload(), expected_model="buffalo_l", expected_branch="Baghdad")
        self.patches = [
            patch.object(web_admin, "CONFIG", self.config),
            patch.object(web_admin, "GALLERY", self.gallery),
            patch.object(web_admin, "SYNC_STATUS", self.status),
            patch.object(web_admin, "FACES", self.faces),
        ]
        for item in self.patches:
            item.start()
        web_admin.apply_security_config()
        web_admin.app.config.update(TESTING=True)
        self.client = web_admin.app.test_client()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def csrf(self):
        with self.client.session_transaction() as session:
            return session["csrf_token"]

    def login(self):
        self.client.get("/login")
        response = self.client.post("/login", data={
            "username": "admin",
            "password": "correct horse battery staple",
            "csrf_token": self.csrf(),
            "next": "/",
        })
        self.assertEqual(response.status_code, 302)

    def test_dashboard_requires_login(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_login_and_security_headers(self):
        self.login()
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])

    def test_export_requires_token(self):
        self.assertEqual(self.client.get("/api/faces/embeddings?branch=Baghdad").status_code, 401)

    def test_export_returns_etag_and_304(self):
        response = self.client.get("/api/faces/embeddings?branch=Baghdad", headers={"Authorization": "Bearer secret"})
        self.assertEqual(response.status_code, 200)
        cached = self.client.get("/api/faces/embeddings?branch=Baghdad", headers={"Authorization": "Bearer secret", "If-None-Match": response.headers["ETag"]})
        self.assertEqual(cached.status_code, 304)

    def test_upload_is_disabled_on_attendance_server(self):
        self.login()
        response = self.client.post("/upload", data={"employee": "HR-EMP-1", "csrf_token": self.csrf()})
        self.assertEqual(response.status_code, 403)

    def test_state_change_rejects_missing_csrf(self):
        self.login()
        self.assertEqual(self.client.post("/logout").status_code, 400)

    def test_readyz_uses_strict_branch_policy(self):
        invalid = payload()
        invalid["branch"] = "Basra"
        write_gallery_atomic(self.gallery, invalid)
        response = self.client.get("/readyz")
        self.assertEqual(response.status_code, 503)
        self.assertTrue(
            any(
                "branch mismatch" in reason
                for reason in response.get_json()["reasons"]
            )
        )


if __name__ == "__main__":
    unittest.main()
