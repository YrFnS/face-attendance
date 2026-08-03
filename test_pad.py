import json
import unittest

import numpy as np
import requests

from pad import PADConfigError, PADGate


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
        return self.response


class PADTests(unittest.TestCase):
    def config(self, **values):
        cfg = {
            "pad_provider": "http",
            "pad_required": True,
            "pad_fail_closed": True,
            "pad_min_score": 0.8,
            "pad_http_url": "https://pad.example.test/v1/check",
            "pad_http_token": "secret",
        }
        cfg.update(values)
        return cfg

    def image(self):
        return np.zeros((64, 64, 3), dtype=np.uint8)

    def test_live_face_passes(self):
        session = FakeSession(FakeResponse({"live": True, "score": 0.91, "evidence_id": "e1"}))
        result = PADGate(self.config(), session=session).evaluate(self.image(), {"camera_id": "in"})
        self.assertTrue(result.passed)
        self.assertEqual(result.evidence_id, "e1")
        self.assertIn("Authorization", session.calls[0][1]["headers"])

    def test_low_score_is_rejected(self):
        session = FakeSession(FakeResponse({"live": True, "score": 0.4}))
        result = PADGate(self.config(), session=session).evaluate(self.image())
        self.assertFalse(result.passed)
        self.assertEqual(result.reason, "score_below_threshold")

    def test_provider_error_fails_closed(self):
        session = FakeSession(error=requests.ConnectionError("offline"))
        result = PADGate(self.config(), session=session).evaluate(self.image())
        self.assertFalse(result.passed)
        self.assertIn("provider_error", result.reason)

    def test_optional_provider_can_fail_open(self):
        session = FakeSession(error=requests.ConnectionError("offline"))
        cfg = self.config(pad_required=False, pad_fail_closed=False)
        result = PADGate(cfg, session=session).evaluate(self.image())
        self.assertTrue(result.passed)
        self.assertTrue(result.skipped)

    def test_required_disabled_provider_is_invalid(self):
        with self.assertRaises(PADConfigError):
            PADGate({"pad_provider": "disabled", "pad_required": True})


if __name__ == "__main__":
    unittest.main()
