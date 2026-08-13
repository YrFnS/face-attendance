import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from embedding_gallery import GalleryError, write_gallery_atomic
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
        "generated_at": "2026-08-13T00:00:00Z",
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
            "allow_insecure_central_url": False,
            "allow_unauthenticated_embedding_sync": False,
            "allow_insecure_frappe_url": False,
            "pad_allow_insecure_url": False,
            "pad_allow_unauthenticated_local": False,
            "embedding_sync_enabled": True,
            "central_url": "https://central.example.test",
            "central_api_token": "secret",
        }

    def tearDown(self):
        self.temp.cleanup()

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
        ):
            with self.subTest(key=key):
                cfg = dict(self.cfg, **{key: value})
                codes = {item[0] for item in strict_profile_issues(cfg)}
                self.assertIn(code, codes)
                with self.assertRaisesRegex(
                    GalleryError, "strict production gallery policy"
                ):
                    effective_gallery_options(cfg)

    def test_branch_and_model_version_must_match(self):
        write_gallery_atomic(self.gallery, payload(branch="Basra"))
        status = inspect_gallery(self.cfg, self.gallery)
        self.assertFalse(status["available"])
        self.assertIn("branch mismatch", status["error"])

        write_gallery_atomic(
            self.gallery, payload(model_version="v2")
        )
        status = inspect_gallery(self.cfg, self.gallery)
        self.assertFalse(status["available"])
        self.assertIn("model version mismatch", status["error"])

    def test_stale_gallery_fails_closed_in_production(self):
        write_gallery_atomic(self.gallery, payload())
        stale = time.time() - 7200
        os.utime(self.gallery, (stale, stale))
        status = inspect_gallery(self.cfg, self.gallery)
        self.assertTrue(status["available"])
        self.assertFalse(status["policy_valid"])
        self.assertTrue(status["stale"])

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
        status = gallery_freshness_status(
            cfg, time.time() - 20
        )
        self.assertTrue(status["stale"])
        self.assertTrue(status["policy_valid"])


if __name__ == "__main__":
    unittest.main()
