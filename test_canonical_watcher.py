import ast
import unittest
from pathlib import Path

from watcher_entrypoints import CANONICAL_WATCHER, require_legacy_dry_run


ROOT = Path(__file__).resolve().parent


class CanonicalWatcherTests(unittest.TestCase):
    def test_live_legacy_commands_are_refused(self):
        for command in ("watch", "watch-folder"):
            with self.subTest(command=command):
                with self.assertRaisesRegex(SystemExit, "watch_service.py"):
                    require_legacy_dry_run(command, dry_run=False)

    def test_legacy_dry_run_remains_available(self):
        for command in ("watch", "watch-folder"):
            with self.subTest(command=command):
                self.assertIsNone(require_legacy_dry_run(command, dry_run=True))

    def test_face_attendance_guards_before_side_effects(self):
        tree = ast.parse((ROOT / "face_attendance.py").read_text(encoding="utf-8"))
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
        }
        for function_name, command in (
            ("watch", "watch"),
            ("watch_folder", "watch-folder"),
        ):
            first = functions[function_name].body[0]
            call = first.value
            self.assertEqual(call.func.id, "require_legacy_dry_run")
            self.assertEqual(call.args[0].value, command)

    def test_windows_launchers_use_only_canonical_watcher(self):
        for filename in (
            "start_face_attendance.ps1",
            "start_ftp_only.ps1",
            "install_auto_start.ps1",
        ):
            text = (ROOT / filename).read_text(encoding="utf-8")
            self.assertIn(CANONICAL_WATCHER, text)
            self.assertNotIn("face_attendance.py", text)
            self.assertIn("$PSScriptRoot", text)

    def test_windows_tasks_cover_ftp_and_watcher(self):
        install = (ROOT / "install_auto_start.ps1").read_text(encoding="utf-8")
        stop = (ROOT / "stop_auto_start.ps1").read_text(encoding="utf-8")
        for task_name in (
            "Face Attendance FTP Receiver",
            "Face Attendance Watcher",
        ):
            self.assertIn(task_name, install)
            self.assertIn(task_name, stop)

    def test_linux_service_and_installer_are_canonical(self):
        service = (
            ROOT / "deploy/systemd/face-attendance-watch.service"
        ).read_text(encoding="utf-8")
        installer = (ROOT / "install_linux.sh").read_text(encoding="utf-8")
        self.assertIn("/opt/face-attendance/watch_service.py", service)
        self.assertIn(
            'if [ -s "$APP_DIR/embedding_gallery.json" ]; then', installer
        )
        self.assertNotIn(
            '|| [ -s "$APP_DIR/embeddings.pkl" ]', installer
        )


if __name__ == "__main__":
    unittest.main()
