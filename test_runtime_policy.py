import base64
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from embedding_gallery import GalleryError, write_gallery_atomic, write_sync_status
from gallery_release import (
    configured_source_url,
    record_acceptance,
    release_scope,
    sign_gallery_payload,
    validate_release,
)
from runtime_policy import (
    effective_gallery_options,
    gallery_freshness_status,
    inspect_gallery,
    strict_profile_issues,
)


def payload(*, branch="Baghdad", model_version="v1"):
    return {
        "schema_version": 1,
        "gallery_version": "runtime-test",
        "generated_at": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "model": "buffalo_l",
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


class RuntimePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.gallery = self.root / "embedding_gallery.json"
        self.status = self.root / "embedding_sync_status.json"
        self.private = Ed25519PrivateKey.generate()
        public = self.private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        public_text = (
            base64.urlsafe_b64encode(public).decode("ascii").rstrip("=")
        )
        self.cfg = {
            "production_mode": True,
            "branch_name": "Baghdad",
            "model": "buffalo_l",
            "model_version": "v1",
            "require_model_match": True,
            "require_model_version_match": True,
            "allow_empty_embedding_gallery": False,
            "reject_stale_embedding_gallery": True,
            "embedding_max_age_seconds": 3600,
            "max_gallery_employees": 100,
            "max_embeddings_per_employee": 5,
            "model_manifest_require_complete": True,
            "model_integrity_verify_on_start": True,
            "pad_require_single_face": True,
            "embedding_sync_inline_enabled": False,
            "allow_insecure_central_url": False,
            "allow_unauthenticated_embedding_sync": False,
            "allow_insecure_frappe_url": False,
            "pad_allow_insecure_url": False,
            "pad_allow_unauthenticated_local": False,
            "embedding_sync_enabled": True,
            "central_url": "https://central.example.test",
            "central_api_token": "secret",
            "embedding_release_publisher": "central-enrollment",
            "embedding_release_trusted_keys": {
                "key-2026": {
                    "publisher": "central-enrollment",
                    "public_key": public_text,
                }
            },
        }

    def tearDown(self):
        self.temp.cleanup()

    def signed(self, *, branch="Baghdad", model_version="v1", generated_at=None):
        item = payload(branch=branch, model_version=model_version)
        return sign_gallery_payload(
            item,
            self.private,
            publisher="central-enrollment",
            key_id="key-2026",
            sequence=1,
            generated_at=generated_at,
            validation_options={
                "expected_model": "buffalo_l",
                "expected_model_version": model_version,
                "expected_branch": branch,
                "require_model_version_match": True,
            },
        )

    def activate(self, item):
        write_gallery_atomic(
            self.gallery,
            item,
            expected_model="buffalo_l",
            expected_model_version=item["model_version"],
            expected_branch=item["branch"],
            require_model_version_match=True,
        )
        scope_id, descriptor = release_scope(
            configured_source_url(self.cfg), self.cfg
        )
        info = validate_release(item, self.cfg)
        scopes = record_acceptance(
            {},
            scope_id,
            descriptor,
            info,
            etag='"runtime-test"',
        )
        write_sync_status(self.status, release_scopes=scopes)

    def test_valid_strict_profile_has_no_issues(self):
        self.assertEqual(strict_profile_issues(self.cfg), ())
        options = effective_gallery_options(self.cfg)
        self.assertTrue(options["require_model_match"])
        self.assertTrue(options["require_model_version_match"])
        self.assertFalse(options["allow_empty"])

    def test_production_cannot_weaken_gallery_controls(self):
        for key, value, code in (
            ("require_model_match", False, "model_match_not_required"),
            (
                "require_model_version_match",
                False,
                "model_version_match_not_required",
            ),
            ("allow_empty_embedding_gallery", True, "empty_gallery_allowed"),
            (
                "reject_stale_embedding_gallery",
                False,
                "stale_gallery_allowed",
            ),
            ("embedding_sync_inline_enabled", True, "inline_sync_enabled"),
        ):
            with self.subTest(key=key):
                cfg = dict(self.cfg, **{key: value})
                codes = {item[0] for item in strict_profile_issues(cfg)}
                self.assertIn(code, codes)
                if key != "embedding_sync_inline_enabled":
                    with self.assertRaisesRegex(
                        GalleryError, "strict production gallery policy"
                    ):
                        effective_gallery_options(cfg)

    def test_branch_and_model_version_must_match(self):
        self.activate(self.signed(branch="Basra"))
        status = inspect_gallery(self.cfg, self.gallery)
        self.assertFalse(status["available"])
        self.assertIn("branch mismatch", status["error"])

        self.activate(self.signed(model_version="v2"))
        status = inspect_gallery(self.cfg, self.gallery)
        self.assertFalse(status["available"])
        self.assertIn("model version mismatch", status["error"])

    def test_staleness_uses_signed_generated_at_not_file_mtime(self):
        generated = (
            datetime.now(timezone.utc) - timedelta(hours=2)
        ).isoformat().replace("+00:00", "Z")
        self.activate(self.signed(generated_at=generated))
        now = datetime.now(timezone.utc).timestamp()
        os.utime(self.gallery, (now, now))
        status = inspect_gallery(self.cfg, self.gallery)
        self.assertTrue(status["available"])
        self.assertFalse(status["policy_valid"])
        self.assertTrue(status["stale"])
        self.assertGreater(status["age_seconds"], 3600)

    def test_installed_gallery_must_match_accepted_release_state(self):
        item = self.signed()
        self.activate(item)
        write_sync_status(self.status, release_scopes={})
        status = inspect_gallery(self.cfg, self.gallery)
        self.assertFalse(status["available"])
        self.assertIn("no accepted release state", status["error"])

    def test_nonproduction_can_report_stale_without_rejecting(self):
        cfg = {
            "production_mode": False,
            "branch_name": "",
            "model": "buffalo_l",
            "model_version": "",
            "require_model_match": True,
            "require_model_version_match": False,
            "allow_empty_embedding_gallery": False,
            "reject_stale_embedding_gallery": False,
            "embedding_max_age_seconds": 10,
        }
        generated = datetime.now(timezone.utc) - timedelta(seconds=20)
        status = gallery_freshness_status(cfg, generated)
        self.assertTrue(status["stale"])
        self.assertTrue(status["policy_valid"])


if __name__ == "__main__":
    unittest.main()
