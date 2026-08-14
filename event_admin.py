import argparse
import json
from pathlib import Path

from event_operations import (
    EventInspectionError,
    EventInspector,
    EventOperationError,
    EventSourceResolver,
    QUARANTINE_SCAN_LIMIT_DEFAULT,
    redact_event_value,
)
from processing_recovery import ProcessingRecoveryError
from runtime_state import RuntimeState, RuntimeStateError


ROOT = Path(__file__).resolve().parent
DEFAULT_DATABASE = ROOT / "runtime_state.sqlite3"
DEFAULT_CONFIG = ROOT / "config.json"


def emit(payload):
    print(
        json.dumps(
            redact_event_value(payload),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


def add_database_argument(parser):
    parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE,
        help="Path to runtime_state.sqlite3.",
    )


def add_config_argument(parser):
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="Path to config.json for source-route and receipt verification.",
    )


def add_operator_arguments(parser, *, watcher_stopped=False):
    parser.add_argument("--actor", required=True, help="Audited operator identity.")
    parser.add_argument(
        "--reason",
        required=True,
        help="Required human-readable reason.",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required acknowledgement for this database mutation.",
    )
    if watcher_stopped:
        parser.add_argument(
            "--confirm-watcher-stopped",
            action="store_true",
            help="Required acknowledgement that the canonical watcher is stopped.",
        )


def require_confirmation(parser, args, *, watcher_stopped=False):
    if not args.confirm:
        parser.error("mutation requires --confirm")
    if watcher_stopped and not args.confirm_watcher_stopped:
        parser.error("source requeue requires --confirm-watcher-stopped")


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Read and safely resolve attendance events. Read-only commands never "
            "migrate or modify the database. Delivery retry/cancel is intentionally "
            "unavailable."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    listing = subparsers.add_parser("list", help="List event summaries read-only.")
    add_database_argument(listing)
    listing.add_argument("--state", default="")
    listing.add_argument("--reason-code", default="")
    listing.add_argument("--camera", default="")
    listing.add_argument("--branch", default="")
    listing.add_argument("--direction", choices=("", "IN", "OUT"), default="")
    listing.add_argument("--employee", default="")
    listing.add_argument("--from-time", default="")
    listing.add_argument("--to-time", default="")
    listing.add_argument("--limit", type=int, default=50)
    listing.add_argument("--offset", type=int, default=0)
    listing.add_argument("--order", choices=("newest", "oldest"), default="newest")

    inspect = subparsers.add_parser(
        "inspect",
        help="Inspect one event and all safe evidence read-only.",
    )
    add_database_argument(inspect)
    inspect.add_argument("event_id")

    explain = subparsers.add_parser(
        "explain",
        help="Explain one event and recommended safe action read-only.",
    )
    add_database_argument(explain)
    explain.add_argument("event_id")

    dismiss = subparsers.add_parser(
        "dismiss",
        help="Dismiss a pre-delivery event with an append-only operator audit.",
    )
    add_database_argument(dismiss)
    add_operator_arguments(dismiss)
    dismiss.add_argument("event_id")

    reprocess = subparsers.add_parser(
        "reprocess",
        help=(
            "Requeue a rejected/failed pre-delivery event whose verified source is "
            "still on its original camera route."
        ),
    )
    add_database_argument(reprocess)
    add_config_argument(reprocess)
    add_operator_arguments(reprocess, watcher_stopped=True)
    reprocess.add_argument("event_id")

    quarantine = subparsers.add_parser(
        "resolve-quarantine",
        help="Audit a quarantine decision or requeue verified evidence.",
    )
    add_database_argument(quarantine)
    add_config_argument(quarantine)
    add_operator_arguments(quarantine)
    quarantine.add_argument("event_id")
    quarantine.add_argument(
        "--resolution",
        choices=("retain", "requeue"),
        required=True,
    )
    quarantine.add_argument(
        "--max-scan",
        type=int,
        default=QUARANTINE_SCAN_LIMIT_DEFAULT,
        help="Maximum regular files inspected below logs/quarantine.",
    )
    quarantine.add_argument(
        "--confirm-watcher-stopped",
        action="store_true",
        help="Required only for requeue.",
    )
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            inspector = EventInspector(args.database)
            emit(
                inspector.list_events(
                    lifecycle_state=args.state,
                    reason_code=args.reason_code,
                    camera_id=args.camera,
                    branch=args.branch,
                    direction=args.direction,
                    employee=args.employee,
                    from_time=args.from_time,
                    to_time=args.to_time,
                    limit=args.limit,
                    offset=args.offset,
                    order=args.order,
                )
            )
            return 0

        if args.command in {"inspect", "explain"}:
            inspector = EventInspector(args.database)
            payload = (
                inspector.inspect_event(args.event_id)
                if args.command == "inspect"
                else inspector.explain_event(args.event_id)
            )
            if payload is None:
                parser.exit(2, f"event not found: {args.event_id}\n")
            emit(payload)
            return 0

        if args.command == "dismiss":
            require_confirmation(parser, args)
            state = RuntimeState(args.database)
            emit(
                state.operator_dismiss_event(
                    args.event_id,
                    actor=args.actor,
                    reason=args.reason,
                )
            )
            return 0

        if args.command == "reprocess":
            require_confirmation(parser, args, watcher_stopped=True)
            state = RuntimeState(args.database)
            event = state.get_event(args.event_id)
            if event is None:
                parser.exit(2, f"event not found: {args.event_id}\n")
            resolver = EventSourceResolver(args.config)
            source_path = resolver.verify_original_source(event)
            emit(
                state.operator_reprocess_event(
                    args.event_id,
                    actor=args.actor,
                    reason=args.reason,
                    source_path=source_path,
                )
            )
            return 0

        if args.command == "resolve-quarantine":
            require_confirmation(parser, args)
            if args.resolution == "requeue" and not args.confirm_watcher_stopped:
                parser.error("quarantine requeue requires --confirm-watcher-stopped")
            state = RuntimeState(args.database)
            event = state.get_event(args.event_id)
            if event is None:
                parser.exit(2, f"event not found: {args.event_id}\n")
            resolver = EventSourceResolver(args.config)
            if args.resolution == "retain":
                source_path = resolver.find_quarantine_source(
                    event,
                    max_scan=args.max_scan,
                )
                emit(
                    state.operator_record_quarantine_resolution(
                        args.event_id,
                        actor=args.actor,
                        reason=args.reason,
                        source_path=source_path,
                        resolution="retain",
                    )
                )
                return 0

            move = resolver.requeue_quarantine_source(
                event,
                max_scan=args.max_scan,
            )
            try:
                result = state.operator_reprocess_event(
                    args.event_id,
                    actor=args.actor,
                    reason=args.reason,
                    source_path=move.destination_image,
                    action="quarantine_requeued",
                )
            except Exception:
                resolver.rollback_requeue(move)
                raise
            emit(
                {
                    **result,
                    "quarantine_source": str(move.quarantine_image),
                    "requeued_source": str(move.destination_image),
                }
            )
            return 0

    except (
        EventInspectionError,
        EventOperationError,
        ProcessingRecoveryError,
        RuntimeStateError,
        OSError,
        ValueError,
    ) as exc:
        parser.exit(1, f"event-admin error: {exc}\n")
    return 0


if __name__ == "__main__":
    main()
