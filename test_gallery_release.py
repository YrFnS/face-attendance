import base64
import unittest
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from embedding_gallery import GalleryError, validate_gallery
from gallery_release import (
    record_acceptance,
    release_scope,
    scope_state,
    sign_gallery_payload,
    validate_installed_release,
    validate_release,
)


def payload():
    return {
        "schema_version": 1,
        "gallery_version": "release-v1",
        "generated_at": "2026-08-13T12:00:00Z",
        "model": "buffalo_l",
        "model_version": "approved-v1",
        "dimension": 3,
        "normalized": True,
        "branch": "Baghdad",
        "employees": [
            {"employee": "HR-EMP-1", "embeddings": [[1.0, 0.0, 0.0]]}
        ],
    }


class GalleryReleaseTests(unittest.TestCase):
    def setUp(self):
        self.private = Ed25519PrivateKey.generate()
        public = self.private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        public_text = base64.urlsafe_b64encode(public).decode("ascii").rstrip("=")
        self.cfg = {
            "production_mode": True,
            "central_url": "https://faces.example.test",
            "embedding_gallery_path": "/api/faces/embeddings",
            "branch_name": "Baghdad",
            "model": "buffalo_l",
            "model_version": "approved-v1",
            "embedding_release_publisher": "central-enrollment",
            "embedding_release_trusted_keys": {
                "key-2026": {
                    "publisher": "central-enrollment",
                    "public_key": public_text,
                }
            },
            "embedding_release_future_tolerance_seconds": 60,
        }
        self.now = datetime(2026, 8, 13, 12, 5, tzinfo=timezone.utc)

    def signed(self, sequence=1, generated_at="2026-08-13T12:00:00Z"):
        return sign_gallery_payload(
            payload(),
            self.private,
            publisher="central-enrollment",
            key_id="key-2026",
            sequence=sequence,
            generated_at=generated_at,
            validation_options={
                "expected_model": "buffalo_l",
                "expected_model_version": "approved-v1",
                "expected_branch": "Baghdad",
                "require_model_version_match": True,
            },
        )

    def test_valid_signed_release_is_verified(self):
        result = validate_release(self.signed(), self.cfg, now=self.now)
        self.assertTrue(result["verified"])
        self.assertEqual(result["sequence"], 1)
        self.assertEqual(result["publisher"], "central-enrollment")

    def test_changed_identity_is_rejected(self):
        signed = self.signed()
        signed["employees"][0]["employee"] = "HR-EMP-CHANGED"
        signed, _, _ = validate_gallery(
            signed,
            expected_model="buffalo_l",
            expected_model_version="approved-v1",
            expected_branch="Baghdad",
            require_model_version_match=True,
        )
        with self.assertRaisesRegex(GalleryError, "signature is invalid"):
            validate_release(signed, self.cfg, now=self.now)

    def test_lower_sequence_is_rejected(self):
        newer = self.signed(sequence=2, generated_at="2026-08-13T12:01:00Z")
        previous = validate_release(newer, self.cfg, now=self.now)
        with self.assertRaisesRegex(GalleryError, "rollback refused"):
            validate_release(self.signed(sequence=1), self.cfg, previous, now=self.now)

    def test_same_sequence_different_content_is_rejected(self):
        previous = validate_release(self.signed(sequence=3), self.cfg, now=self.now)
        changed = payload()
        changed["employees"].append(
            {"employee": "HR-EMP-2", "embeddings": [[0.0, 1.0, 0.0]]}
        )
        second = sign_gallery_payload(
            changed,
            self.private,
            publisher="central-enrollment",
            key_id="key-2026",
            sequence=3,
            generated_at="2026-08-13T12:00:00Z",
        )
        with self.assertRaisesRegex(GalleryError, "equivocation"):
            validate_release(second, self.cfg, previous, now=self.now)

    def test_future_generated_at_is_rejected(self):
        future = self.signed(
            sequence=4,
            generated_at=(self.now + timedelta(minutes=5)).isoformat(),
        )
        with self.assertRaisesRegex(GalleryError, "too far in the future"):
            validate_release(future, self.cfg, now=self.now)

    def test_invalid_generated_at_is_rejected(self):
        unsigned = payload()
        unsigned["generated_at"] = "not-a-time"
        unsigned, _, _ = validate_gallery(unsigned)
        cfg = dict(self.cfg, production_mode=False, embedding_release_required=False)
        with self.assertRaisesRegex(GalleryError, "RFC 3339"):
            validate_release(unsigned, cfg, now=self.now)

    def test_scope_is_bound_to_source_branch_and_model(self):
        first, descriptor = release_scope(
            "https://faces.example.test/api/faces/embeddings", self.cfg
        )
        second, _ = release_scope(
            "https://faces.example.test/api/faces/embeddings",
            dict(self.cfg, branch_name="Basra"),
        )
        third, _ = release_scope(
            "https://other.example.test/api/faces/embeddings", self.cfg
        )
        self.assertNotEqual(first, second)
        self.assertNotEqual(first, third)
        self.assertEqual(descriptor["branch"], "Baghdad")

    def test_release_history_is_bounded(self):
        scope_id, descriptor = release_scope(
            "https://faces.example.test/api/faces/embeddings", self.cfg
        )
        status = {}
        for sequence in range(1, 6):
            signed = self.signed(
                sequence=sequence,
                generated_at=f"2026-08-13T12:0{sequence}:00Z",
            )
            info = validate_release(signed, self.cfg, now=self.now)
            scopes = record_acceptance(
                status,
                scope_id,
                descriptor,
                info,
                etag=f'"{sequence}"',
                history_limit=3,
            )
            status = {"release_scopes": scopes}
        state = scope_state(status, scope_id)
        self.assertEqual(len(state["history"]), 3)
        self.assertEqual(state["history"][0]["sequence"], 3)
        self.assertEqual(state["sequence"], 5)

    def test_installed_production_gallery_requires_matching_state(self):
        signed = self.signed()
        with self.assertRaisesRegex(GalleryError, "no accepted release state"):
            validate_installed_release(
                signed,
                self.cfg,
                {},
                source_url="https://faces.example.test/api/faces/embeddings",
                now=self.now,
            )
        scope_id, descriptor = release_scope(
            "https://faces.example.test/api/faces/embeddings", self.cfg
        )
        info = validate_release(signed, self.cfg, now=self.now)
        scopes = record_acceptance(
            {}, scope_id, descriptor, info, etag='"one"'
        )
        accepted = validate_installed_release(
            signed,
            self.cfg,
            {"release_scopes": scopes},
            source_url="https://faces.example.test/api/faces/embeddings",
            now=self.now,
        )
        self.assertTrue(accepted["verified"])


if __name__ == "__main__":
    unittest.main()
