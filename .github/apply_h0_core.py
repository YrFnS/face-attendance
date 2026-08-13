from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(path, old, new):
    path = ROOT / path
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected one match in {path}, found {count}")
    path.write_text(text.replace(old, new), encoding="utf-8")


(ROOT / "watcher_entrypoints.py").write_text(
    '''"""Production and legacy watcher entry-point policy."""

CANONICAL_WATCHER = "watch_service.py"
LEGACY_WATCHER_COMMANDS = frozenset({"watch", "watch-folder"})


def require_legacy_dry_run(command, *, dry_run):
    """Refuse legacy watcher commands unless they are explicitly non-delivering."""
    command = str(command or "").strip()
    if command not in LEGACY_WATCHER_COMMANDS:
        raise ValueError(f"unknown legacy watcher command: {command or '<empty>'}")
    if bool(dry_run):
        return
    raise SystemExit(
        f"Refusing live legacy watcher '{command}'. It bypasses production "
        "readiness, PAD, replay protection, and the durable event ledger. "
        f"Use 'python {CANONICAL_WATCHER}' for live processing. For controlled "
        f"diagnostics only, rerun 'python face_attendance.py {command} --dry-run'."
    )
''',
    encoding="utf-8",
)

replace_once(
    "face_attendance.py",
    """    write_gallery_atomic,
)


ROOT = Path(__file__).resolve().parent
""",
    """    write_gallery_atomic,
)
from watcher_entrypoints import require_legacy_dry_run


ROOT = Path(__file__).resolve().parent
""",
)
replace_once(
    "face_attendance.py",
    """def watch(once=False, dry_run=False):
    cfg = load_config()
""",
    """def watch(once=False, dry_run=False):
    require_legacy_dry_run("watch", dry_run=dry_run)
    cfg = load_config()
""",
)
replace_once(
    "face_attendance.py",
    """def watch_folder(once=False, dry_run=False, scan_existing=False):
    cfg = load_config()
""",
    """def watch_folder(once=False, dry_run=False, scan_existing=False):
    require_legacy_dry_run("watch-folder", dry_run=dry_run)
    cfg = load_config()
""",
)
replace_once(
    "face_attendance.py",
    """    run = sub.add_parser("watch")
    run.add_argument("--once", action="store_true")
    run.add_argument("--dry-run", action="store_true")

    folder = sub.add_parser("watch-folder")
    folder.add_argument("--once", action="store_true")
    folder.add_argument("--dry-run", action="store_true")
""",
    """    run = sub.add_parser(
        "watch",
        help="Legacy RTSP diagnostics only; live processing is refused.",
    )
    run.add_argument("--once", action="store_true")
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="Required safety flag for the legacy RTSP diagnostic path.",
    )

    folder = sub.add_parser(
        "watch-folder",
        help="Legacy folder diagnostics only; live processing is refused.",
    )
    folder.add_argument("--once", action="store_true")
    folder.add_argument(
        "--dry-run",
        action="store_true",
        help="Required safety flag for the legacy folder diagnostic path.",
    )
""",
)
