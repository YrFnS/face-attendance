import argparse
import json
from pathlib import Path

from runtime_state import (
    DEFAULT_BACKUP_DIRECTORY,
    RuntimeState,
    RuntimeStateError,
    create_runtime_backup,
    inspect_runtime_database,
    restore_runtime_backup,
    verify_runtime_backup,
)


ROOT = Path(__file__).resolve().parent
DEFAULT_DATABASE = ROOT / "runtime_state.sqlite3"


def emit(payload):
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def add_database_argument(parser):
    parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE,
        help="Path to runtime_state.sqlite3.",
    )


def add_backup_directory_argument(parser):
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=None,
        help=(
            "Backup directory. The default is a sibling "
            f"{DEFAULT_BACKUP_DIRECTORY}/ directory."
        ),
    )


def main():
    parser = argparse.ArgumentParser(
        description="Inspect, migrate, back up, verify, and restore runtime state."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status", help="Inspect without changing the database.")
    add_database_argument(status)

    verify = subparsers.add_parser("verify", help="Run integrity and schema verification.")
    add_database_argument(verify)
    verify.add_argument(
        "--allow-older",
        action="store_true",
        help="Allow a valid database from an older schema version.",
    )

    migrate = subparsers.add_parser(
        "migrate",
        help="Apply pending forward migrations after creating a verified backup.",
    )
    add_database_argument(migrate)
    add_backup_directory_argument(migrate)

    backup = subparsers.add_parser("backup", help="Create and verify a manual backup.")
    add_database_argument(backup)
    add_backup_directory_argument(backup)
    backup.add_argument("--reason", default="manual")

    check_backup = subparsers.add_parser(
        "verify-backup", help="Verify a backup and its SHA-256 metadata sidecar."
    )
    check_backup.add_argument("backup", type=Path)

    restore = subparsers.add_parser(
        "restore",
        help="Atomically restore a verified backup after making a safety backup.",
    )
    add_database_argument(restore)
    add_backup_directory_argument(restore)
    restore.add_argument("backup", type=Path)
    restore.add_argument(
        "--confirm-restore",
        action="store_true",
        help="Required acknowledgement that all runtime services are stopped.",
    )

    args = parser.parse_args()
    try:
        if args.command == "status":
            report = inspect_runtime_database(args.database, require_latest=False)
            emit(report)
            raise SystemExit(0 if report["ok"] else 1)
        if args.command == "verify":
            report = inspect_runtime_database(
                args.database,
                require_latest=not args.allow_older,
            )
            emit(report)
            raise SystemExit(0 if report["ok"] else 1)
        if args.command == "migrate":
            state = RuntimeState(args.database, backup_dir=args.backup_dir)
            emit(
                {
                    "ok": True,
                    "database": state.migration_status(),
                    "migration_backup": state.last_migration_backup,
                }
            )
            return
        if args.command == "backup":
            emit(
                create_runtime_backup(
                    args.database,
                    args.backup_dir,
                    reason=args.reason,
                )
            )
            return
        if args.command == "verify-backup":
            report = verify_runtime_backup(args.backup)
            emit(report)
            raise SystemExit(0 if report["ok"] else 1)
        if args.command == "restore":
            emit(
                restore_runtime_backup(
                    args.database,
                    args.backup,
                    args.backup_dir,
                    confirm=args.confirm_restore,
                )
            )
            return
    except RuntimeStateError as exc:
        parser.exit(1, f"runtime-state error: {exc}\n")


if __name__ == "__main__":
    main()
