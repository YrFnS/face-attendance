import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from camera_sources import (
    load_camera_sources,
    receipt_path,
    source_by_username,
    verify_source_receipt,
)
from ftp_receiver import AtomicUploadMixin, unique_destination


class FakeUploadHandler(AtomicUploadMixin):
    def __init__(self, username, remote_ip):
        self.username = username
        self.remote_ip = remote_ip
        self.messages = []

    def _log(self, message):
        self.messages.append(message)


class FTPReceiverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.uploads = self.root / "camera_uploads"
        self.cfg = {
            "production_mode": True,
            "branch_name": "Baghdad",
            "camera_uploads_dir": str(self.uploads),
            "camera_source_receipt_required": True,
            "camera_source_receipt_secret": "receipt-secret-" + "x" * 32,
            "ftp_permissions": "elw",
            "ftp_staging_enabled": True,
            "ftp_users": {
                "camera_in": {
                    "password": "camera-in-password-unique",
                    "permissions": "elw",
                }
            },
            "camera_sources": {
                "entrance-in": {
                    "source_type": "holowits_ftp",
                    "branch": "Baghdad",
                    "policy": "IN",
                    "ftp_username": "camera_in",
                    "upload_dir": str(self.uploads / "in"),
                    "allowed_networks": ["192.0.2.10/32"],
                }
            },
        }
        self.sources = load_camera_sources(self.cfg, self.root)
        self.source = source_by_username(self.sources, "camera_in")

    def tearDown(self):
        self.temp.cleanup()

    def handler(self, remote_ip="192.0.2.10"):
        handler = FakeUploadHandler("camera_in", remote_ip)
        handler.user_sources = {"camera_in": self.source}
        handler.receipt_config = self.cfg
        handler.max_upload_bytes = 1024 * 1024
        handler.staging_enabled = True
        return handler

    def staged_file(self, content=b"image"):
        path = self.source.upload_dir / ".incoming" / "camera_in" / "event.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_completed_upload_moves_to_bound_route_and_writes_verified_receipt(self):
        staged = self.staged_file(b"camera-event")
        handler = self.handler()
        handler.on_file_received(str(staged))
        destination = self.source.upload_dir / "event.jpg"
        self.assertFalse(staged.exists())
        self.assertEqual(destination.read_bytes(), b"camera-event")
        digest = hashlib.sha256(b"camera-event").hexdigest()
        source, receipt = verify_source_receipt(
            destination,
            self.cfg,
            self.root,
            source_sha256=digest,
            source_size=destination.stat().st_size,
        )
        self.assertEqual(source.camera_id, "entrance-in")
        self.assertTrue(receipt.verified)
        self.assertEqual(receipt.remote_ip, "192.0.2.10")
        self.assertTrue(any("upload complete" in item for item in handler.messages))

    def test_disallowed_network_cannot_leave_an_upload_or_receipt(self):
        staged = self.staged_file()
        handler = self.handler("192.0.2.99")
        handler.on_file_received(str(staged))
        destination = self.source.upload_dir / "event.jpg"
        self.assertFalse(staged.exists())
        self.assertFalse(destination.exists())
        self.assertFalse(receipt_path(destination).exists())
        self.assertTrue(any("not allowed" in item for item in handler.messages))

    def test_upload_outside_bound_staging_route_is_removed(self):
        outside = self.root / "other" / "event.jpg"
        outside.parent.mkdir(parents=True)
        outside.write_bytes(b"image")
        handler = self.handler()
        handler.on_file_received(str(outside))
        self.assertFalse(outside.exists())
        self.assertFalse((self.source.upload_dir / "event.jpg").exists())
        self.assertTrue(any("outside its bound staging route" in item for item in handler.messages))

    def test_existing_or_orphaned_receipt_forces_unique_destination(self):
        self.source.upload_dir.mkdir(parents=True, exist_ok=True)
        orphan = self.source.upload_dir / "event.jpg"
        receipt_path(orphan).write_text(json.dumps({"orphan": True}), encoding="utf-8")
        destination = unique_destination(self.source.upload_dir, "event.jpg")
        self.assertNotEqual(destination, orphan)
        self.assertEqual(destination.suffix, ".jpg")


if __name__ == "__main__":
    unittest.main()
