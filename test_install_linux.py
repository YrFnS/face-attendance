import unittest
from pathlib import Path


class InstallLinuxTests(unittest.TestCase):
    def test_custom_app_directory_is_applied_to_all_services(self):
        script = Path("install_linux.sh").read_text(encoding="utf-8")
        self.assertIn('WorkingDirectory="%s"', script)
        self.assertIn("ExecStart=\\n", script)
        for runtime_path in (
            ".venv/bin/python",
            ".venv/bin/gunicorn",
            "ftp_receiver.py",
            "watch_service.py",
            "delivery_service.py",
            "sync_embeddings.py",
            "gunicorn.conf.py",
        ):
            self.assertIn(f"$APP_DIR/{runtime_path}", script)
        self.assertIn("delivery_attachments", script)
        self.assertIn("attachment_spool", script)


if __name__ == "__main__":
    unittest.main()
