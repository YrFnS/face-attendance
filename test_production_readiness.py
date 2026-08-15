import base64
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from embedding_gallery import write_gallery_atomic, write_sync_status
from gallery_release import (
    configured_source_url,
    record_acceptance,
    release_scope,
    sign_gallery_payload,
    validate_release,
)
from model_manifest import build_manifest, write_manifest_atomic
from production_readiness import check_production_readiness
from secret_store import RuntimeConfig
from web_security import hash_password


def gallery_payload(*, branch="Baghdad", model_version="v1"):
    return {
        "schema_version": 1,
        "gallery_version": "readiness-test",
        "generated_at": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
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
        self.status = self.root / "embedding_sync_status.json"
        self.cert = self.root / "cert.pem"
        self.key = self.root / "key.pem"
        self.cert.write_text("cert", encoding="utf-8")
        self.key.write_text("key", encoding="utf-8")

        self.private = Ed25519PrivateKey.generate()
        public = self.private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        self.public_text = (
            base64.urlsafe_b64encode(public).decode("ascii").rstrip("=")
        )
        self.activate_gallery()

    def tearDown(self):
        self.temp.cleanup()

    def valid_config(self):
        cfg = {
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
            "embedding_sync_inline_enabled": False,
            "central_url": "https://central.example.test",
            "central_api_credential_id": "readiness-node-2026",
            "central_api_credentials": {
                "readiness-node-2026": {
                    "token": "c" * 48,
                    "scopes": ["gallery:read"],
                    "branches": ["Baghdad"],
                    "models": ["licensed_model"],
                    "model_versions": ["v1"],
                    "enabled": True,
                }
            },
            "production_external_secrets_required": True,
            "embedding_release_publisher": "central-enrollment",
            "embedding_release_trusted_keys": {
                "key-2026": {
                    "publisher": "central-enrollment",
                    "public_key": self.public_text,
                }
            },
            "allow_insecure_central_url": False,
            "allow_unauthenticated_embedding_sync": False,
            "pad_provider": "http",
            "pad_required": True,
            "pad_fail_closed": True,
            "pad_require_single_face": True,
            "pad_min_score": 0.8,
            "pad_http_url": "https://pad.example.test/v1/check",
            "pad_http_token": "p" * 48,
            "pad_expected_provider": "approved-provider",
            "pad_allowed_models": ["liveness-v3"],
            "pad_require_binding_echo": True,
            "pad_require_evidence_id": True,
            "pad_max_faces_per_event": 8,
            "pad_allow_insecure_url": False,
            "pad_allow_unauthenticated_local": False,
            "web_auth_mode": "local",
            "web_mfa_required": False,
            "web_admin_username": "admin",
            "web_admin_password_hash": self.valid_password_hash,
            "web_session_secret": "s" * 48,
            "web_bind_host": "127.0.0.1",
            "web_cookie_secure": True,
            "web_hsts_enabled": True,
            "https_reverse_proxy_acknowledged": True,
            "web_trust_proxy_headers": True,
            "web_trusted_proxy_networks": ["127.0.0.1/32", "::1/128"],
            "web_forwarded_for_header": "X-Forwarded-For",
            "web_max_forwarded_hops": 8,
            "frappe_url": "https://erp.example.test",
            "allow_insecure_frappe_url": False,
            "erpnext_idempotency_required": True,
            "erpnext_idempotency_contract_version": (
                "face-attendance/erpnext-checkin-idempotency/v1"
            ),
            "erpnext_idempotency_create_method": (
                "face_attendance_idempotency.api."
                "create_or_get_employee_checkin"
            ),
            "erpnext_idempotency_probe_method": (
                "face_attendance_idempotency.api.get_contract"
            ),
            "erpnext_expected_site": "erp.example.test",
            "erpnext_expected_idempotency_fingerprint": "f" * 64,
            "erpnext_idempotency_probe_cache_seconds": 300,
            "ftp_tls_enabled": True,
            "ftp_tls_certfile": str(self.cert),
            "ftp_tls_keyfile": str(self.key),
            "ftp_tls_control_required": True,
            "ftp_tls_data_required": True,
            "ftp_staging_enabled": True,
            "ftp_permissions": "elw",
            "camera_uploads_dir": str(self.root / "camera_uploads"),
            "camera_source_receipt_required": True,
            "camera_source_receipt_secret": "receipt-secret-" + "x" * 32,
            "camera_source_receipt_future_tolerance_seconds": 300,
            "ftp_users": {
                "camera_in": {
                    ("pass" + "word"): "camera-in-unique-value",
                    "permissions": "elw",
                },
                "camera_out": {
                    ("pass" + "word"): "camera-out-unique-value",
                    "permissions": "elw",
                },
            },
            "camera_sources": {
                "camera-in": {
                    "source_type": "holowits_ftp",
                    "branch": "Baghdad",
                    "policy": "IN",
                    "ftp_username": "camera_in",
                    "upload_dir": str(self.root / "camera_uploads" / "in"),
                    "allowed_networks": ["192.0.2.10/32"],
                },
                "camera-out": {
                    "source_type": "holowits_ftp",
                    "branch": "Baghdad",
                    "policy": "OUT",
                    "ftp_username": "camera_out",
                    "upload_dir": str(self.root / "camera_uploads" / "out"),
                    "allowed_networks": ["192.0.2.11/32"],
                },
            },
        }
        return RuntimeConfig(
            cfg,
            secret_sources={
                "central_api_credentials.readiness-node-2026.token": "systemd://central_gallery_token",
                "pad_http_token": "systemd://pad_http_token",
                "web_admin_password_hash": "systemd://web_admin_password_hash",
                "web_session_secret": "systemd://web_session_secret",
                "camera_source_receipt_secret": "systemd://camera_source_receipt_secret",
                "ftp_users.camera_in.password": "systemd://ftp_camera_in_password",
                "ftp_users.camera_out.password": "systemd://ftp_camera_out_password",
            },
        )

    def activate_gallery(
        self,
        *,
        branch="Baghdad",
        model_version="v1",
        generated_at=None,
        sequence=1,
    ):
        cfg = self.valid_config()
        item = gallery_payload(
            branch=branch,
            model_version=model_version,
        )
        signed = sign_gallery_payload(
            item,
            self.private,
            publisher="central-enrollment",
            key_id="key-2026",
            sequence=sequence,
            generated_at=generated_at,
            validation_options={
                "expected_model": "licensed_model",
                "expected_model_version": model_version,
                "expected_branch": branch,
                "require_model_version_match": True,
            },
        )
        write_gallery_atomic(
            self.gallery,
            signed,
            expected_model="licensed_model",
            expected_model_version=model_version,
            expected_branch=branch,
            require_model_version_match=True,
        )
        scope_id, descriptor = release_scope(
            configured_source_url(cfg), cfg
        )
        info = validate_release(signed, cfg)
        scopes = record_acceptance(
            {},
            scope_id,
            descriptor,
            info,
            etag='"readiness-test"',
        )
        write_sync_status(self.status, release_scopes=scopes)

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
        self.assertTrue(
            report.gallery["release_validation"]["verified"]
        )

    def test_missing_erpnext_idempotency_proof_is_blocked(self):
        cfg = self.valid_config()
        cfg.update(
            erpnext_idempotency_required=False,
            erpnext_expected_site="",
            erpnext_expected_idempotency_fingerprint="",
        )
        report = self.report(cfg, verify_model_files=False)
        issues = [
            issue.message
            for issue in report.blockers
            if issue.code == "delivery_worker_configuration_invalid"
        ]
        self.assertTrue(issues)
        text = " ".join(issues)
        self.assertIn("erpnext_idempotency_required", text)
        self.assertIn("erpnext_expected_site", text)
        self.assertIn("erpnext_expected_idempotency_fingerprint", text)

    def test_missing_pad_provider_and_model_pins_are_blocked(self):
        cfg = self.valid_config()
        cfg.update(
            pad_expected_provider="",
            pad_allowed_models=[],
            pad_require_binding_echo=False,
            pad_require_evidence_id=False,
        )
        report = self.report(cfg, verify_model_files=False)
        messages = " ".join(issue.message for issue in report.blockers)
        self.assertIn("pin the approved PAD provider", messages)
        self.assertIn("allowlist at least one", messages)
        self.assertIn("pad_require_binding_echo", messages)
        self.assertIn("pad_require_evidence_id", messages)

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
        self.activate_gallery(branch="Basra")
        report = self.report(verify_model_files=False)
        self.assertIn(
            "embedding_gallery_invalid",
            {issue.code for issue in report.blockers},
        )
        self.assertIn("branch mismatch", report.gallery["error"])

    def test_stale_release_is_blocked_even_with_fresh_file_mtime(self):
        generated = (
            datetime.now(timezone.utc) - timedelta(hours=2)
        ).isoformat().replace("+00:00", "Z")
        self.activate_gallery(generated_at=generated)
        now = datetime.now(timezone.utc).timestamp()
        os.utime(self.gallery, (now, now))
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

    def test_missing_camera_source_binding_is_blocked(self):
        cfg = self.valid_config()
        cfg.pop("camera_sources")
        report = self.report(cfg, verify_model_files=False)
        self.assertIn(
            "camera_source_binding_invalid",
            {issue.code for issue in report.blockers},
        )

    def test_inline_secret_and_untrusted_proxy_are_blocked(self):
        cfg = self.valid_config()
        cfg.secret_sources.pop("web_session_secret")
        cfg["web_trust_proxy_headers"] = False
        report = self.report(cfg, verify_model_files=False)
        codes = {issue.code for issue in report.blockers}
        self.assertIn("external_secret_delivery_invalid", codes)
        self.assertIn("trusted_proxy_configuration_invalid", codes)

    def test_gallery_credential_scope_mismatch_is_blocked(self):
        cfg = self.valid_config()
        cfg["central_api_credentials"] = {
            "readiness-node-2026": {
                **cfg["central_api_credentials"]["readiness-node-2026"],
                "branches": ["Basra"],
            }
        }
        report = self.report(cfg, verify_model_files=False)
        self.assertIn(
            "gallery_credentials_invalid",
            {issue.code for issue in report.blockers},
        )

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
