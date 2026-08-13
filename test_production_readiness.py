import os
import tempfile
import time
import unittest
from pathlib import Path

from embedding_gallery import write_gallery_atomic
from model_manifest import build_manifest, write_manifest_atomic
from production_readiness import check_production_readiness
from web_security import hash_password


def gallery_payload(*, branch="Baghdad", model_version="v1"):
    return {
        "schema_version": 1,
        "gallery_version": "readiness-test",
        "generated_at": "2026-08-13T00:00:00Z",
        "model": "licensed_model",
        "model_version": model_version,
        "dimension": 3,
        "normalized": True,
        "branch": branch,
        "employees": [
            {
                "employee": "HR-EMP-1",
                "embeddings": [[1.0, 0.0, 0.0]],
            }
        ],
    }


class ProductionReadinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.valid_password_hash = hash_password(
            "correct horse battery staple"
        )

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.insightface_root = self.root / "insightface"
        self.model_dir = (
            self.insightface_root / "models" / "licensed_model"
        )
        self.model_dir.mkdir(parents=True)
        (self.model_dir / "recognition.onnx").write_bytes(b"model")
        self.manifest = self.root / "model_manifest.json"
        write_manifest_atomic(
            self.manifest,
            build_manifest(
                model_directory=self.model_dir,
                model="licensed_model",
                model_version="v1",
                license_reference="contract-123",
            ),
        )
        self.gallery = self.root / "embedding_gallery.json"
        write_gallery_atomic(self.gallery, gallery_payload())
        self.cert = self.root / "cert.pem"
        self.key = self.root / "key.pem"
        self.cert.write_text("cert", encoding="utf-8")
        self.key.write_text("key", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def valid_config(self):
        return {
            "production_mode": True,
            "branch_name": "Baghdad",
            "model": "licensed_model",
            "model_version": "v1",
            "model_directory": str(self.model_dir),
            "model_manifest_path": str(self.manifest),
            "model_manifest_require_complete": True,
            "model_integrity_verify_on_start": True,
            "model_license_acknowledged": True,
            "model_license_reference": "contract-123",
            "require_model_match": True,
            "require_model_version_match": True,
            "allow_empty_embedding_gallery": False,
            "reject_stale_embedding_gallery": True,
            "embedding_max_age_seconds": 3600,
            "embedding_sync_enabled": True,
            "central_url": "https://central.example.test",
            "central_api_token": "secret",
            "allow_insecure_central_url": False,
            "allow_unauthenticated_embedding_sync": False,
            "pad_provider": "http",
            "pad_required": True,
            "pad_fail_closed": True,
            "pad_require_single_face": True,
            "pad_min_score": 0.8,
            "pad_http_url": "https://pad.example.test/v1/check",
            "pad_http_token": "secret",
            "pad_allow_insecure_url": False,
            "pad_allow_unauthenticated_local": False,
            "web_admin_username": "admin",
            "web_admin_password_hash": self.valid_password_hash,
            "web_session_secret": "s" * 48,
            "web_bind_host": "127.0.0.1",
            "web_cookie_secure": True,
            "web_hsts_enabled": True,
            "https_reverse_proxy_acknowledged": True,
            "frappe_url": "https://erp.example.test",
            "allow_insecure_frappe_url": False,
            "ftp_tls_enabled": True,
            "ftp_tls_certfile": str(self.cert),
            "ftp_tls_keyfile": str(self.key),
            "ftp_tls_control_required": True,
            "ftp_tls_data_required": True,
            "ftp_staging_enabled": True,
            "ftp_permissions": "elw",
            "camera_ids": {
                "in": "camera-in",
                "out": "camera-out",
            },
        }

    def report(self, cfg=None, **kwargs):
        return check_production_readiness(
            cfg or self.valid_config(),
            self.root,
            gallery_path=self.gallery,
            **kwargs,
        )

    def test_valid_production_config_is_ready(self):
        report = self.report()
        self.assertTrue(report.ready, report.to_dict())
        self.assertTrue(report.model_integrity["ok"])
        self.assertEqual(
            Path(report.model_integrity["insightface_root"]),
            self.insightface_root,
        )
        self.assertTrue(report.gallery["policy_valid"])

    def test_missing_strict_identity_is_blocked(self):
        cfg = self.valid_config()
        cfg.update(
            branch_name="",
            model_version="",
            require_model_version_match=False,
        )
        codes = {issue.code for issue in self.report(cfg).blockers}
        self.assertIn("branch_name_missing", codes)
        self.assertIn("model_version_missing", codes)
        self.assertIn("model_version_match_not_required", codes)

    def test_malformed_admin_hash_is_blocked(self):
        cfg = self.valid_config()
        cfg["web_admin_password_hash"] = (
            "scrypt$16384$8$1$salt$hash"
        )
        report = self.report(cfg, verify_model_files=False)
        self.assertIn(
            "web_admin_auth_invalid",
            {issue.code for issue in report.blockers},
        )

    def test_wrong_branch_gallery_is_blocked(self):
        write_gallery_atomic(
            self.gallery, gallery_payload(branch="Basra")
        )
        report = self.report(verify_model_files=False)
        self.assertIn(
            "embedding_gallery_invalid",
            {issue.code for issue in report.blockers},
        )
        self.assertIn("branch mismatch", report.gallery["error"])

    def test_stale_gallery_is_blocked(self):
        stale = time.time() - 7200
        os.utime(self.gallery, (stale, stale))
        report = self.report(verify_model_files=False)
        self.assertIn(
            "embedding_gallery_policy_failed",
            {issue.code for issue in report.blockers},
        )
        self.assertTrue(report.gallery["stale"])

    def test_changed_model_file_is_blocked(self):
        (self.model_dir / "recognition.onnx").write_bytes(b"changed")
        report = self.report()
        self.assertIn(
            "model_integrity_failed",
            {issue.code for issue in report.blockers},
        )

    def test_skip_hash_still_checks_inventory_and_sizes(self):
        (self.model_dir / "extra.onnx").write_bytes(b"extra")
        report = self.report(verify_model_files=False)
        self.assertFalse(report.model_integrity["ok"])
        self.assertFalse(report.model_integrity["hashes_verified"])
        self.assertTrue(
            any(
                "unlisted" in message
                for message in report.model_integrity["errors"]
            )
        )

    def test_missing_pad_and_license_are_blockers(self):
        cfg = self.valid_config()
        cfg.update(
            model_license_acknowledged=False,
            model_license_reference="",
            pad_provider="disabled",
            pad_required=False,
        )
        codes = {
            issue.code
            for issue in self.report(
                cfg, verify_model_files=False
            ).blockers
        }
        self.assertIn("model_license_not_acknowledged", codes)
        self.assertIn("pad_not_required", codes)
        self.assertIn("pad_provider_disabled", codes)

    def test_plain_ftp_requires_isolation_ack(self):
        cfg = self.valid_config()
        cfg["ftp_tls_enabled"] = False
        cfg["camera_network_isolated_acknowledged"] = False
        report = self.report(cfg)
        self.assertIn(
            "camera_transport_unprotected",
            {issue.code for issue in report.blockers},
        )

    def test_disabled_ftp_staging_is_a_blocker(self):
        cfg = self.valid_config()
        cfg["ftp_staging_enabled"] = False
        report = self.report(cfg)
        self.assertIn(
            "ftp_staging_disabled",
            {issue.code for issue in report.blockers},
        )

    def test_non_upload_ftp_permissions_are_a_blocker(self):
        cfg = self.valid_config()
        cfg["ftp_users"] = {
            "camera_in": {"permissions": "elrw"},
            "camera_out": {"permissions": "elw"},
        }
        report = self.report(cfg)
        self.assertIn(
            "ftp_permissions_unsafe",
            {issue.code for issue in report.blockers},
        )


if __name__ == "__main__":
    unittest.main()
