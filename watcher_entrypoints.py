"""Production and legacy watcher entry-point policy."""

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
