import json
import tempfile
import unittest
from pathlib import Path

import requests

from embedding_gallery import read_sync_status
from secure_sync import sync_gallery


def payload():
    return {
        "schema_version": 1,
        "gallery_version": "sync-test",
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


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = json.dumps(body).encode("utf-8") if isinstance(body, dict) else body or b""
        self.headers = headers or {}
        self.closed = False

    def iter_content(self, chunk_size=65536):
        for index in range(0, len(self._body), chunk_size):
            yield self._body[index:index + chunk_size]

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class SecureSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.gallery = root / "embedding_gallery.json"
        self.status = root / "embedding_sync_status.json"
        self.cfg = {
            "central_url": "https://central.example.test",
            "central_api_token": "secret",
            "branch_name": "Baghdad",
            "model": "buffalo_l",
            "embedding_sync_retries": 0,
            "embedding_max_response_bytes": 1024 * 1024,
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_sync_writes_gallery_and_etag(self):
        session = FakeSession([FakeResponse(body=payload(), headers={"Content-Type": "application/json", "ETag": '"remote-etag"'})])
        result = sync_gallery(self.cfg, self.gallery, self.status, session=session, sleep=lambda _: None)
        self.assertTrue(result["changed"])
        self.assertTrue(self.gallery.exists())
        self.assertEqual(read_sync_status(self.status)["etag"], '"remote-etag"')
        self.assertEqual(session.calls[0][1]["timeout"], (5.0, 30.0))

    def test_conditional_request_accepts_304(self):
        first = FakeSession([FakeResponse(body=payload(), headers={"Content-Type": "application/json", "ETag": '"remote-etag"'})])
        sync_gallery(self.cfg, self.gallery, self.status, session=first, sleep=lambda _: None)
        second = FakeSession([FakeResponse(status=304, headers={"ETag": '"remote-etag"'})])
        result = sync_gallery(self.cfg, self.gallery, self.status, session=second, sleep=lambda _: None)
        self.assertTrue(result["not_modified"])
        self.assertFalse(result["changed"])
        self.assertEqual(second.calls[0][1]["headers"]["If-None-Match"], '"remote-etag"')

    def test_non_json_response_is_rejected(self):
        session = FakeSession([FakeResponse(body=b"not json", headers={"Content-Type": "text/html"})])
        with self.assertRaisesRegex(Exception, "application/json"):
            sync_gallery(self.cfg, self.gallery, self.status, session=session, sleep=lambda _: None)
        self.assertFalse(self.gallery.exists())

    def test_oversized_response_is_rejected(self):
        cfg = dict(self.cfg, embedding_max_response_bytes=1024)
        session = FakeSession([FakeResponse(body=b"x" * 2048, headers={"Content-Type": "application/json"})])
        with self.assertRaisesRegex(Exception, "max response size"):
            sync_gallery(cfg, self.gallery, self.status, session=session, sleep=lambda _: None)


if __name__ == "__main__":
    unittest.main()
