import tempfile
import unittest
from pathlib import Path

from model_manifest import build_manifest, write_manifest_atomic
from production_readiness import check_production_readiness


class ProductionReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.model_dir = self.root / "model"
        self.model_dir.mkdir()
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
        self.cert = self.root / "cert.pem"
        self.key = self.root / "key.pem"
        self.cert.write_text("cert", encoding="utf-8")
        self.key.write_text("key", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def valid_config(self):
        return {
            "production_mode": True,
            "model": "licensed_model",
            "model_version": "v1",
            "model_directory": str(self.model_dir),
            "model_manifest_path": str(self.manifest),
            "model_manifest_require_complete": True,
            "model_license_acknowledged": True,
            "model_license_reference": "contract-123",
            "pad_provider": "http",
            "pad_required": True,
            "pad_fail_closed": True,
            "pad_min_score": 0.8,
            "pad_http_url": "https://pad.example.test/v1/check",
            "pad_http_token": "secret",
            "web_admin_username": "admin",
            "web_admin_password_hash": "scrypt$16384$8$1$salt$hash",
            "web_session_secret": "s" * 48,
            "web_bind_host": "127.0.0.1",
            "web_cookie_secure": True,
            "web_hsts_enabled": True,
            "https_reverse_proxy_acknowledged": True,
            "central_url": "https://central.example.test",
            "frappe_url": "https://erp.example.test",
            "ftp_tls_enabled": True,
            "ftp_tls_certfile": str(self.cert),
            "ftp_tls_keyfile": str(self.key),
            "ftp_tls_control_required": True,
            "ftp_tls_data_required": True,
            "camera_ids": {"in": "camera-in", "out": "camera-out"},
        }

    def test_valid_production_config_is_ready(self):
        report = check_production_readiness(self.valid_config(), self.root)
        self.assertTrue(report.ready, report.to_dict())

    def test_missing_pad_and_license_are_blockers(self):
        cfg = self.valid_config()
        cfg.update(
            model_license_acknowledged=False,
            model_license_reference="",
            pad_provider="disabled",
            pad_required=False,
        )
        report = check_production_readiness(cfg, self.root, verify_model_files=False)
        codes = {issue.code for issue in report.blockers}
        self.assertIn("model_license_not_acknowledged", codes)
        self.assertIn("pad_not_required", codes)
        self.assertIn("pad_provider_disabled", codes)

    def test_plain_ftp_requires_isolation_ack(self):
        cfg = self.valid_config()
        cfg["ftp_tls_enabled"] = False
        cfg["camera_network_isolated_acknowledged"] = False
        report = check_production_readiness(cfg, self.root)
        self.assertIn(
            "camera_transport_unprotected",
            {issue.code for issue in report.blockers},
        )


if __name__ == "__main__":
    unittest.main()
