import json
import tempfile
import unittest
from pathlib import Path

import requests

from embedding_gallery import load_gallery, read_sync_status
from secure_sync import sync_gallery


def payload(*, dimension=3, version="sync-test"):
    return {
        "schema_version": 1,
        "gallery_version": version,
        "generated_at": "2026-07-29T00:00:00Z",
        "model": "buffalo_l",
        "model_version": "",
        "dimension": dimension,
        "normalized": True,
        "branch": "Baghdad",
        "employees": [
            {"employee": "HR-EMP-1", "embeddings": [[1.0, 0.0, 0.0]]}
        ],
    }


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = (
            json.dumps(body).encode("utf-8")
            if isinstance(body, dict)
            else body or b""
        )
        self.headers = headers or {}
        self.closed = False

    def iter_content(self, chunk_size=65536):
        for index in range(0, len(self._body), chunk_size):
            yield self._body[index : index + chunk_size]

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
        session = FakeSession(
            [
                FakeResponse(
                    body=payload(),
                    headers={
                        "Content-Type": "application/json",
                        "ETag": '"remote-etag"',
                    },
                )
            ]
        )
        result = sync_gallery(
            self.cfg,
            self.gallery,
            self.status,
            session=session,
            sleep=lambda _: None,
        )
        self.assertTrue(result["changed"])
        self.assertTrue(self.gallery.exists())
        self.assertEqual(read_sync_status(self.status)["etag"], '"remote-etag"')
        self.assertEqual(session.calls[0][1]["timeout"], (5.0, 30.0))
        self.assertFalse(session.calls[0][1]["allow_redirects"])

    def test_conditional_request_accepts_304(self):
        first = FakeSession(
            [
                FakeResponse(
                    body=payload(),
                    headers={
                        "Content-Type": "application/json",
                        "ETag": '"remote-etag"',
                    },
                )
            ]
        )
        sync_gallery(
            self.cfg,
            self.gallery,
            self.status,
            session=first,
            sleep=lambda _: None,
        )
        second = FakeSession(
            [FakeResponse(status=304, headers={"ETag": '"remote-etag"'})]
        )
        result = sync_gallery(
            self.cfg,
            self.gallery,
            self.status,
            session=second,
            sleep=lambda _: None,
        )
        self.assertTrue(result["not_modified"])
        self.assertFalse(result["changed"])
        self.assertEqual(
            second.calls[0][1]["headers"]["If-None-Match"], '"remote-etag"'
        )

    def test_non_json_response_is_rejected(self):
        session = FakeSession(
            [FakeResponse(body=b"not json", headers={"Content-Type": "text/html"})]
        )
        with self.assertRaisesRegex(Exception, "application/json"):
            sync_gallery(
                self.cfg,
                self.gallery,
                self.status,
                session=session,
                sleep=lambda _: None,
            )
        self.assertFalse(self.gallery.exists())

    def test_oversized_response_is_rejected(self):
        cfg = dict(self.cfg, embedding_max_response_bytes=1024)
        session = FakeSession(
            [
                FakeResponse(
                    body=b"x" * 2048,
                    headers={"Content-Type": "application/json"},
                )
            ]
        )
        with self.assertRaisesRegex(Exception, "max response size"):
            sync_gallery(
                cfg,
                self.gallery,
                self.status,
                session=session,
                sleep=lambda _: None,
            )

    def test_failed_sync_preserves_previous_gallery(self):
        first = FakeSession(
            [
                FakeResponse(
                    body=payload(version="valid"),
                    headers={"Content-Type": "application/json"},
                )
            ]
        )
        sync_gallery(
            self.cfg,
            self.gallery,
            self.status,
            session=first,
            sleep=lambda _: None,
        )
        invalid = payload(dimension=512, version="invalid")
        second = FakeSession(
            [
                FakeResponse(
                    body=invalid,
                    headers={"Content-Type": "application/json"},
                )
            ]
        )
        with self.assertRaisesRegex(Exception, "dimension mismatch"):
            sync_gallery(
                self.cfg,
                self.gallery,
                self.status,
                session=second,
                sleep=lambda _: None,
            )
        _, metadata, _ = load_gallery(self.gallery)
        self.assertEqual(metadata["gallery_version"], "valid")

    def test_placeholder_token_is_rejected(self):
        cfg = dict(self.cfg, central_api_token="CHANGE_ME")
        with self.assertRaisesRegex(Exception, "non-placeholder"):
            sync_gallery(
                cfg,
                self.gallery,
                self.status,
                session=FakeSession([]),
                sleep=lambda _: None,
            )

    def test_remote_plain_http_is_rejected(self):
        cfg = dict(self.cfg, central_url="http://192.0.2.10")
        with self.assertRaisesRegex(Exception, "HTTPS"):
            sync_gallery(
                cfg,
                self.gallery,
                self.status,
                session=FakeSession([]),
                sleep=lambda _: None,
            )

    def test_same_origin_https_redirect_is_followed(self):
        first = FakeResponse(status=302, headers={"Location": "/v2/embeddings"})
        second = FakeResponse(
            body=payload(), headers={"Content-Type": "application/json"}
        )
        session = FakeSession([first, second])
        result = sync_gallery(
            self.cfg,
            self.gallery,
            self.status,
            session=session,
            sleep=lambda _: None,
        )
        self.assertEqual(
            session.calls[1][0], "https://central.example.test/v2/embeddings"
        )
        self.assertEqual(
            session.calls[1][1]["headers"]["Authorization"], "Bearer secret"
        )
        self.assertEqual(
            result["source_url"], "https://central.example.test/v2/embeddings"
        )
        self.assertTrue(first.closed)

    def test_cross_origin_redirect_is_rejected_before_following(self):
        response = FakeResponse(
            status=302, headers={"Location": "https://evil.example.test/gallery"}
        )
        session = FakeSession([response])
        with self.assertRaisesRegex(Exception, "cross-origin"):
            sync_gallery(
                self.cfg,
                self.gallery,
                self.status,
                session=session,
                sleep=lambda _: None,
            )
        self.assertEqual(len(session.calls), 1)
        self.assertTrue(response.closed)

    def test_https_downgrade_redirect_is_rejected(self):
        response = FakeResponse(
            status=302,
            headers={"Location": "http://central.example.test/gallery"},
        )
        session = FakeSession([response])
        with self.assertRaisesRegex(Exception, "HTTPS-to-HTTP"):
            sync_gallery(
                self.cfg,
                self.gallery,
                self.status,
                session=session,
                sleep=lambda _: None,
            )
        self.assertEqual(len(session.calls), 1)

    def test_redirect_limit_is_enforced(self):
        cfg = dict(self.cfg, embedding_max_redirects=1)
        first = FakeResponse(status=302, headers={"Location": "/one"})
        second = FakeResponse(status=302, headers={"Location": "/two"})
        session = FakeSession([first, second])
        with self.assertRaisesRegex(Exception, "maximum redirects"):
            sync_gallery(
                cfg,
                self.gallery,
                self.status,
                session=session,
                sleep=lambda _: None,
            )
        self.assertEqual(len(session.calls), 2)
        self.assertTrue(second.closed)


if __name__ == "__main__":
    unittest.main()
