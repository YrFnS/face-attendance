import json
import unittest

import numpy as np
import requests

from pad import PADConfigError, PADGate, configuration_issues


class FakeResponse:
    def __init__(self, payload, status=200, content_type="application/json"):
        self.status_code = status
        self.body = json.dumps(payload).encode("utf-8")
        self.headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(self.body)),
        }
        self.closed = False

    def iter_content(self, chunk_size=16384):
        yield self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        if callable(self.response):
            return self.response(url, kwargs)
        return self.response


def bound_response(*, live=True, score=0.91, **overrides):
    def factory(_url, kwargs):
        context = json.loads(kwargs["data"]["context"])
        payload = {
            "live": live,
            "score": score,
            "provider": "approved-provider",
            "model": "liveness-v3",
            "evidence_id": "evidence-1",
            "face_binding_id": context["face_binding_id"],
        }
        payload.update(overrides)
        return FakeResponse(payload)

    return factory


class PADTests(unittest.TestCase):
    def config(self, **values):
        cfg = {
            "production_mode": True,
            "pad_provider": "http",
            "pad_required": True,
            "pad_fail_closed": True,
            "pad_require_single_face": True,
            "pad_max_faces_per_event": 8,
            "pad_min_score": 0.8,
            "pad_http_url": "https://pad.example.test/v1/check",
            "pad_http_token": "secret",
            "pad_expected_provider": "approved-provider",
            "pad_allowed_models": ["liveness-v3"],
            "pad_require_binding_echo": True,
            "pad_require_evidence_id": True,
        }
        cfg.update(values)
        return cfg

    def image(self):
        return np.zeros((64, 64, 3), dtype=np.uint8)

    def context(self):
        return {
            "event_id": "a" * 64,
            "source_sha256": "b" * 64,
            "camera_id": "camera-in",
            "log_type": "IN",
            "face_index": 1,
            "face_count": 1,
            "bbox": [1, 2, 30, 40],
        }

    def test_live_face_passes_with_bound_pinned_evidence(self):
        session = FakeSession(bound_response())
        result = PADGate(self.config(), session=session).evaluate(
            self.image(), self.context()
        )
        self.assertTrue(result.passed)
        self.assertEqual(result.evidence_id, "evidence-1")
        self.assertEqual(result.provider, "approved-provider")
        self.assertEqual(result.model, "liveness-v3")
        self.assertEqual(len(result.binding_id), 64)
        self.assertEqual(result.face_index, 1)
        self.assertEqual(result.face_count, 1)
        sent = json.loads(session.calls[0][1]["data"]["context"])
        self.assertEqual(sent["face_binding_id"], result.binding_id)
        self.assertEqual(sent["face_crop_sha256"], result.crop_sha256)
        self.assertIn("Authorization", session.calls[0][1]["headers"])

    def test_low_score_is_rejected(self):
        session = FakeSession(bound_response(score=0.4))
        result = PADGate(self.config(), session=session).evaluate(
            self.image(), self.context()
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.reason, "score_below_threshold")

    def test_binding_mismatch_fails_closed(self):
        session = FakeSession(bound_response(face_binding_id="0" * 64))
        result = PADGate(self.config(), session=session).evaluate(
            self.image(), self.context()
        )
        self.assertFalse(result.passed)
        self.assertIn("does not match", result.reason)

    def test_wrong_provider_fails_closed(self):
        session = FakeSession(bound_response(provider="other-provider"))
        result = PADGate(self.config(), session=session).evaluate(
            self.image(), self.context()
        )
        self.assertFalse(result.passed)
        self.assertIn("pinned provider", result.reason)

    def test_unapproved_model_fails_closed(self):
        session = FakeSession(bound_response(model="liveness-v2"))
        result = PADGate(self.config(), session=session).evaluate(
            self.image(), self.context()
        )
        self.assertFalse(result.passed)
        self.assertIn("pad_allowed_models", result.reason)

    def test_missing_evidence_id_fails_closed(self):
        session = FakeSession(bound_response(evidence_id=""))
        result = PADGate(self.config(), session=session).evaluate(
            self.image(), self.context()
        )
        self.assertFalse(result.passed)
        self.assertIn("evidence_id is required", result.reason)

    def test_missing_score_fails_closed(self):
        def factory(_url, kwargs):
            context = json.loads(kwargs["data"]["context"])
            return FakeResponse(
                {
                    "live": True,
                    "provider": "approved-provider",
                    "model": "liveness-v3",
                    "evidence_id": "evidence-1",
                    "face_binding_id": context["face_binding_id"],
                }
            )

        result = PADGate(self.config(), session=FakeSession(factory)).evaluate(
            self.image(), self.context()
        )
        self.assertFalse(result.passed)
        self.assertIn("explicit score", result.reason)

    def test_provider_error_fails_closed(self):
        session = FakeSession(error=requests.ConnectionError("offline"))
        result = PADGate(self.config(), session=session).evaluate(
            self.image(), self.context()
        )
        self.assertFalse(result.passed)
        self.assertIn("provider_error", result.reason)

    def test_optional_provider_can_fail_open(self):
        session = FakeSession(error=requests.ConnectionError("offline"))
        cfg = self.config(
            production_mode=False,
            pad_required=False,
            pad_fail_closed=False,
            pad_expected_provider="",
            pad_allowed_models=[],
            pad_require_binding_echo=False,
            pad_require_evidence_id=False,
        )
        result = PADGate(cfg, session=session).evaluate(self.image(), self.context())
        self.assertTrue(result.passed)
        self.assertTrue(result.skipped)

    def test_required_disabled_provider_is_invalid(self):
        with self.assertRaises(PADConfigError):
            PADGate({"pad_provider": "disabled", "pad_required": True})

    def test_production_requires_provider_model_and_evidence_controls(self):
        cfg = self.config(
            pad_expected_provider="",
            pad_allowed_models=[],
            pad_require_binding_echo=False,
            pad_require_evidence_id=False,
        )
        text = "; ".join(configuration_issues(cfg))
        self.assertIn("pin the approved PAD provider", text)
        self.assertIn("allowlist at least one", text)
        self.assertIn("pad_require_binding_echo", text)
        self.assertIn("pad_require_evidence_id", text)


if __name__ == "__main__":
    unittest.main()
