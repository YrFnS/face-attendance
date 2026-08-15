import json
import tempfile
import unittest
from pathlib import Path

from camera_sources import (
    CameraSourceError,
    camera_source_configuration_issues,
    load_camera_sources,
    receipt_path,
    source_by_username,
    source_for_upload_path,
    verify_source_receipt,
    write_source_receipt,
)


class CameraSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.uploads = self.root / "camera_uploads"
        self.image = self.uploads / "in" / "event.jpg"
        self.image.parent.mkdir(parents=True)
        self.image.write_bytes(b"camera-image")
        self.digest = __import__("hashlib").sha256(b"camera-image").hexdigest()
        self.cfg = {
            "production_mode": True,
            "branch_name": "Baghdad",
            "camera_uploads_dir": str(self.uploads),
            "camera_source_receipt_required": True,
            "camera_source_receipt_secret": "receipt-secret-" + "x" * 32,
            "ftp_permissions": "elw",
            "ftp_users": {
                "camera_in": {
                    "password": "camera-in-password-unique",
                    "permissions": "elw",
                },
                "camera_out": {
                    "password": "camera-out-password-unique",
                    "permissions": "elw",
                },
            },
            "camera_sources": {
                "entrance-in": {
                    "source_type": "holowits_ftp",
                    "branch": "Baghdad",
                    "policy": "IN",
                    "ftp_username": "camera_in",
                    "upload_dir": str(self.uploads / "in"),
                    "allowed_networks": ["192.0.2.10/32"],
                },
                "entrance-out": {
                    "source_type": "holowits_ftp",
                    "branch": "Baghdad",
                    "policy": "OUT",
                    "ftp_username": "camera_out",
                    "upload_dir": str(self.uploads / "out"),
                    "allowed_networks": ["192.0.2.11/32"],
                },
            },
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_valid_registry_binds_route_username_policy_branch_and_network(self):
        sources = load_camera_sources(self.cfg, self.root)
        self.assertEqual(len(sources), 2)
        source = source_by_username(sources, "camera_in")
        self.assertEqual(source.camera_id, "entrance-in")
        self.assertEqual(source.policy, "IN")
        self.assertEqual(source.branch, "Baghdad")
        self.assertTrue(source.allows_ip("192.0.2.10"))
        self.assertFalse(source.allows_ip("192.0.2.12"))
        self.assertEqual(source_for_upload_path(sources, self.image), source)
        self.assertEqual(len(source.binding_id), 64)

    def test_every_credential_must_be_unique_and_bound_once(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["ftp_users"]["camera_out"]["password"] = cfg["ftp_users"]["camera_in"]["password"]
        with self.assertRaisesRegex(CameraSourceError, "reuse the same password"):
            load_camera_sources(cfg, self.root)

        cfg = json.loads(json.dumps(self.cfg))
        cfg["camera_sources"]["entrance-out"]["ftp_username"] = "camera_in"
        with self.assertRaisesRegex(CameraSourceError, "more than one camera"):
            load_camera_sources(cfg, self.root)

        cfg = json.loads(json.dumps(self.cfg))
        cfg["ftp_users"]["unbound"] = {
            "password": "third-unique-camera-password",
            "permissions": "elw",
        }
        with self.assertRaisesRegex(CameraSourceError, "unbound users"):
            load_camera_sources(cfg, self.root)

    def test_upload_route_rejects_symbolic_link_components(self):
        outside = self.root / "real-in"
        outside.mkdir()
        link = self.uploads / "linked-in"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(outside, target_is_directory=True)
        cfg = json.loads(json.dumps(self.cfg))
        cfg["camera_sources"]["entrance-in"]["upload_dir"] = str(link)
        with self.assertRaisesRegex(CameraSourceError, "symbolic-link"):
            load_camera_sources(cfg, self.root)

    def test_routes_must_be_dedicated_nonoverlapping_and_inside_upload_root(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["camera_sources"]["entrance-out"]["upload_dir"] = str(self.uploads / "in" / "nested")
        with self.assertRaisesRegex(CameraSourceError, "must not overlap"):
            load_camera_sources(cfg, self.root)

        cfg = json.loads(json.dumps(self.cfg))
        cfg["camera_sources"]["entrance-in"]["upload_dir"] = str(self.root / "outside")
        with self.assertRaisesRegex(CameraSourceError, "remain under camera_uploads_dir"):
            load_camera_sources(cfg, self.root)

    def test_branch_policy_and_allowed_network_are_strict(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["camera_sources"]["entrance-in"]["branch"] = "Basra"
        with self.assertRaisesRegex(CameraSourceError, "does not match branch_name"):
            load_camera_sources(cfg, self.root)

        cfg = json.loads(json.dumps(self.cfg))
        cfg["camera_sources"]["entrance-in"]["policy"] = "presence"
        with self.assertRaisesRegex(CameraSourceError, "must be IN or OUT"):
            load_camera_sources(cfg, self.root)

        cfg = json.loads(json.dumps(self.cfg))
        cfg["camera_sources"]["entrance-in"]["allowed_networks"] = ["0.0.0.0/0"]
        with self.assertRaisesRegex(CameraSourceError, "entire internet"):
            load_camera_sources(cfg, self.root)

    def test_signed_receipt_round_trip_binds_upload_and_remote_ip(self):
        source = source_by_username(load_camera_sources(self.cfg, self.root), "camera_in")
        path = write_source_receipt(
            self.image,
            source,
            self.cfg,
            remote_ip="192.0.2.10",
            source_sha256=self.digest,
            source_size=self.image.stat().st_size,
            received_at="2026-08-13T23:00:00Z",
        )
        self.assertEqual(path, receipt_path(self.image))
        verified_source, receipt = verify_source_receipt(
            self.image,
            self.cfg,
            self.root,
            source_sha256=self.digest,
            source_size=self.image.stat().st_size,
        )
        self.assertEqual(verified_source, source)
        self.assertTrue(receipt.verified)
        self.assertEqual(receipt.remote_ip, "192.0.2.10")
        self.assertEqual(receipt.ftp_username, "camera_in")
        self.assertEqual(receipt.source_binding_id, source.binding_id)

    def test_receipt_tampering_wrong_file_and_wrong_network_fail_closed(self):
        source = source_by_username(load_camera_sources(self.cfg, self.root), "camera_in")
        write_source_receipt(
            self.image,
            source,
            self.cfg,
            remote_ip="192.0.2.10",
            source_sha256=self.digest,
            source_size=self.image.stat().st_size,
        )
        data = json.loads(receipt_path(self.image).read_text(encoding="utf-8"))
        data["remote_ip"] = "192.0.2.99"
        receipt_path(self.image).write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(CameraSourceError, "not allowed"):
            verify_source_receipt(
                self.image,
                self.cfg,
                self.root,
                source_sha256=self.digest,
                source_size=self.image.stat().st_size,
            )

        write_source_receipt(
            self.image,
            source,
            self.cfg,
            remote_ip="192.0.2.10",
            source_sha256=self.digest,
            source_size=self.image.stat().st_size,
        )
        with self.assertRaisesRegex(CameraSourceError, "SHA-256"):
            verify_source_receipt(
                self.image,
                self.cfg,
                self.root,
                source_sha256="0" * 64,
                source_size=self.image.stat().st_size,
            )

        data = json.loads(receipt_path(self.image).read_text(encoding="utf-8"))
        data["signature"] = "0" * 64
        receipt_path(self.image).write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(CameraSourceError, "signature is invalid"):
            verify_source_receipt(
                self.image,
                self.cfg,
                self.root,
                source_sha256=self.digest,
                source_size=self.image.stat().st_size,
            )

    def test_production_requires_verified_receipt_and_strong_secret(self):
        with self.assertRaisesRegex(CameraSourceError, "receipt is missing"):
            verify_source_receipt(
                self.image,
                self.cfg,
                self.root,
                source_sha256=self.digest,
                source_size=self.image.stat().st_size,
            )
        cfg = dict(self.cfg, camera_source_receipt_secret="CHANGE_ME")
        issues = " ".join(camera_source_configuration_issues(cfg, self.root))
        self.assertIn("must not be a placeholder", issues)

    def test_nonproduction_fixture_can_explicitly_disable_receipts(self):
        cfg = dict(
            self.cfg,
            production_mode=False,
            camera_source_receipt_required=False,
            camera_source_receipt_secret="",
        )
        source, receipt = verify_source_receipt(
            self.image,
            cfg,
            self.root,
            source_sha256=self.digest,
            source_size=self.image.stat().st_size,
        )
        self.assertEqual(source.camera_id, "entrance-in")
        self.assertFalse(receipt.verified)


if __name__ == "__main__":
    unittest.main()
