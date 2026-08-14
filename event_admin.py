import argparse
import json
import os
import shutil
import sqlite3
from pathlib import Path

from camera_sources import CameraSourceError, load_camera_sources, receipt_path
from data_contract import GalleryError, strict_json_loads
from event_ledger import EventLedgerMixin
from event_operations import (
    EventOperationError,
    EventOperationsMixin,
    MAX_OPERATOR_TARGET_NAME_BYTES,
    operator_staging_path,
    redact_text,
)
from runtime_state import (
    RuntimeState,
    RuntimeStateError,
    file_sha256,
    inspect_runtime_database,
)


ROOT = Path(__file__).resolve().parent
DEFAULT_DATABASE = ROOT / "runtime_state.sqlite3"
DEFAULT_CONFIG = ROOT / "config.json"
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png"})
MAX_RECEIPT_BYTES = 32 * 1024


class EventAdminError(RuntimeError):
    pass


class ReadOnlyEventState(EventOperationsMixin, EventLedgerMixin):
    """Read ledger data through a SQLite connection opened with mode=ro."""

    def __init__(self, path):
        raw_path = Path(path).expanduser()
        if raw_path.is_symlink():
            raise EventAdminError("runtime database must not be a symbolic link")
        self.path = Path(os.path.abspath(raw_path))
        report = inspect_runtime_database(self.path, require_latest=True)
        if not report["ok"]:
            raise EventAdminError(
                "runtime database is not ready for read-only event access: "
                + "; ".join(report["errors"])
            )

    def _connect(self):
        uri = self.path.as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection


def mutable_event_state(path):
    raw_path = Path(path).expanduser()
    if raw_path.is_symlink():
        raise EventAdminError("runtime database must not be a symbolic link")
    path = Path(os.path.abspath(raw_path))
    report = inspect_runtime_database(path, require_latest=True)
    if not report["ok"]:
        raise EventAdminError(
            "runtime database must be migrated and verified before an operator action: "
            + "; ".join(report["errors"])
        )
    return RuntimeState(path)


def emit(payload):
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _assert_no_symlink_components(path, label):
    path = Path(path)
    cursor = path
    while True:
        if cursor.exists() and cursor.is_symlink():
            raise EventAdminError(f"{label} must not use a symbolic-link path: {cursor}")
        if cursor == cursor.parent:
            break
        cursor = cursor.parent


def _regular_file(path, label, *, max_bytes=0):
    path = Path(path).expanduser()
    _assert_no_symlink_components(path, label)
    if not path.is_file():
        raise EventAdminError(f"{label} is not a regular file: {path}")
    resolved = path.resolve(strict=True)
    size = resolved.stat().st_size
    if max_bytes and size > int(max_bytes):
        raise EventAdminError(f"{label} exceeds {int(max_bytes)} bytes")
    return resolved


def _event_media_path(event, supplied):
    value = supplied or event.get("retention_path") or event.get("source_path")
    if not value:
        raise EventAdminError(
            "event has no recorded media path; supply --media-path explicitly"
        )
    return _regular_file(value, "event media")


def verify_event_media(event, supplied=None):
    media = _event_media_path(event, supplied)
    expected_digest = str(event.get("source_sha256") or "").lower()
    expected_size = int(event.get("source_size") or 0)
    if media.stat().st_size != expected_size:
        raise EventAdminError(
            "event media size does not match the immutable event receipt"
        )
    digest, size = file_sha256(media)
    if digest != expected_digest:
        raise EventAdminError(
            "event media SHA-256 does not match the immutable event content hash"
        )
    if size != expected_size:
        raise EventAdminError(
            "event media size does not match the immutable event receipt"
        )

    sidecar = receipt_path(media)
    receipt_required = bool(event.get("receipt_verified")) or event.get(
        "receipt_state"
    ) in {"verified", "route_only"}
    if sidecar.exists():
        sidecar = _regular_file(
            sidecar,
            "event source receipt",
            max_bytes=MAX_RECEIPT_BYTES,
        )
    elif receipt_required:
        raise EventAdminError(
            "the verified event media is missing its companion source receipt"
        )
    else:
        sidecar = None
    return media, sidecar


def _load_raw_config(path):
    path = _regular_file(path, "configuration", max_bytes=4 * 1024 * 1024)
    try:
        payload = strict_json_loads(
            path.read_text(encoding="utf-8"),
            field="configuration",
        )
    except (OSError, UnicodeDecodeError, GalleryError) as exc:
        raise EventAdminError(f"configuration is invalid: {exc}") from exc
    if not isinstance(payload, dict):
        raise EventAdminError("configuration must contain a JSON object")
    return path, payload


def _camera_source_for_event(event, config_path):
    config_path, cfg = _load_raw_config(config_path)
    try:
        sources = load_camera_sources(cfg, config_path.parent)
    except CameraSourceError as exc:
        raise EventAdminError(f"camera source configuration is invalid: {exc}") from exc
    matches = [item for item in sources if item.camera_id == event.get("camera_id")]
    if len(matches) != 1:
        raise EventAdminError(
            "the event camera is not bound exactly once in the current configuration"
        )
    source = matches[0]
    for field, actual, expected in (
        ("branch", event.get("branch"), source.branch),
        ("policy", event.get("policy"), source.policy),
        ("source_type", event.get("source_type"), source.source_type),
    ):
        if actual and actual != expected:
            raise EventAdminError(
                f"event {field} {actual!r} does not match current camera binding {expected!r}"
            )
    return source


def _truncate_utf8(value, max_bytes):
    output = []
    used = 0
    for character in str(value):
        encoded = character.encode("utf-8")
        if used + len(encoded) > int(max_bytes):
            break
        output.append(character)
        used += len(encoded)
    return "".join(output)


def _visible_image_name(event):
    original = Path(str(event.get("source_name") or "capture.jpg")).name
    suffix = Path(original).suffix.lower()
    if suffix not in IMAGE_EXTENSIONS:
        raise EventAdminError("event source filename has an unsupported image extension")
    for character in original:
        if ord(character) < 32 or ord(character) == 127:
            raise EventAdminError("event source filename contains a control character")
    prefix = f"reprocess-{str(event['event_id'])}-"
    prefix_bytes = len(prefix.encode("utf-8"))
    suffix_bytes = len(suffix.encode("utf-8"))
    stem_budget = MAX_OPERATOR_TARGET_NAME_BYTES - prefix_bytes - suffix_bytes
    if stem_budget < 1:
        raise EventAdminError("event ID leaves no room for a reprocess filename")
    stem = _truncate_utf8(Path(original).stem, stem_budget)
    if not stem:
        stem = "capture"
    result = prefix + stem + suffix
    if len(result.encode("utf-8")) > MAX_OPERATOR_TARGET_NAME_BYTES:
        raise EventAdminError("reprocess filename exceeds the filesystem limit")
    return result


def _unique_reprocess_target(source, event):
    source.upload_dir.mkdir(parents=True, exist_ok=True)
    filename = _visible_image_name(event)
    candidate = source.upload_dir / filename
    counter = 1
    while (
        candidate.exists()
        or receipt_path(candidate).exists()
        or operator_staging_path(candidate).exists()
        or receipt_path(operator_staging_path(candidate)).exists()
    ):
        suffix = candidate.suffix
        stem = candidate.stem
        marker = f"-{counter}"
        budget = (
            MAX_OPERATOR_TARGET_NAME_BYTES
            - len(suffix.encode("utf-8"))
            - len(marker.encode("utf-8"))
        )
        stem = _truncate_utf8(stem, max(1, budget))
        candidate = source.upload_dir / f"{stem}{marker}{suffix}"
        counter += 1
    return candidate.resolve(strict=False)


def _move_path(source, destination):
    source = Path(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(source, destination)
    except OSError:
        shutil.move(str(source), str(destination))


def _stage_media(media, sidecar, target):
    stage = operator_staging_path(target)
    stage_receipt = receipt_path(stage)
    if stage.exists() or stage_receipt.exists():
        raise EventAdminError(f"operator staging path already exists: {stage}")
    moved_image = False
    moved_receipt = False
    try:
        _move_path(media, stage)
        moved_image = True
        if sidecar is not None:
            _move_path(sidecar, stage_receipt)
            moved_receipt = True
        return stage, stage_receipt if moved_receipt else None
    except Exception:
        if moved_receipt and stage_receipt.exists():
            try:
                _move_path(stage_receipt, sidecar)
            except Exception:
                pass
        if moved_image and stage.exists():
            try:
                _move_path(stage, media)
            except Exception:
                pass
        raise


def _rollback_staged_media(stage, stage_receipt, original, original_receipt):
    restored = None
    try:
        if Path(stage).exists():
            _move_path(stage, original)
            restored = original
        elif Path(original).exists():
            restored = original
    except Exception:
        restored = stage if Path(stage).exists() else original
    if stage_receipt is not None and Path(stage_receipt).exists():
        try:
            _move_path(stage_receipt, original_receipt)
        except Exception:
            pass
    return Path(restored).resolve(strict=False) if restored is not None else None


def _publish_staged_media(stage, stage_receipt, target):
    _move_path(stage, target)
    if stage_receipt is not None and Path(stage_receipt).exists():
        _move_path(stage_receipt, receipt_path(target))
    return target


def _audit_denied(state, *, actor, action, event_id, error):
    try:
        state.audit(
            actor=actor or "unknown",
            action=f"event_{action}_denied",
            remote_addr="local-cli",
            detail={
                "event_id": event_id or "",
                "error": redact_text(str(error))[:1000],
            },
        )
    except Exception:
        pass


def reprocess_event(
    state,
    *,
    event_id,
    actor,
    reason,
    config_path,
    media_path=None,
    action="reprocess_requested",
    publish_lease_seconds=120,
):
    event = state.event_details(event_id, include_history=True)
    if event is None:
        raise EventAdminError(f"event does not exist: {event_id}")
    media, sidecar = verify_event_media(event, media_path)
    source = _camera_source_for_event(event, config_path)
    target = _unique_reprocess_target(source, event)
    stage, stage_receipt = _stage_media(media, sidecar, target)
    result = None
    try:
        result = state.request_event_reprocess(
            event_id,
            actor=actor,
            reason=reason,
            media_path=str(target),
            action=action,
            publish_lease_seconds=publish_lease_seconds,
        )
        try:
            _publish_staged_media(stage, stage_receipt, target)
            state.complete_event_reprocess_publish(
                event_id,
                action_id=result["action_id"],
                media_path=str(target),
            )
        except Exception as exc:
            target_receipt = receipt_path(target)
            restored = _rollback_staged_media(
                target if target.exists() else stage,
                target_receipt if target_receipt.exists() else stage_receipt,
                media,
                sidecar or receipt_path(media),
            )
            failure_reason = f"{reason}; reprocess publication failed: {exc}"
            state.mark_reprocess_publish_failed(
                event_id,
                action_id=result["action_id"],
                actor=actor,
                reason=failure_reason[:1000],
                retention_path=str(restored or media),
                retention_state=(
                    str(event.get("retention_state") or "retained")
                    if str(event.get("retention_state") or "")
                    in {"retained", "quarantined", "temporary"}
                    else "retained"
                ),
            )
            raise EventAdminError(str(exc)) from exc
        return {
            **result,
            "published_path": str(target),
            "source_receipt_published": bool(stage_receipt),
        }
    except Exception:
        if result is None:
            _rollback_staged_media(
                stage,
                stage_receipt,
                media,
                sidecar or receipt_path(media),
            )
        raise


def _add_database_argument(parser):
    parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE,
        help="Path to runtime_state.sqlite3.",
    )


def _add_actor_reason(parser):
    parser.add_argument("--actor", required=True, help="Stable operator identity.")
    parser.add_argument(
        "--reason",
        required=True,
        help="Human review reason; at least five characters.",
    )


def _require_confirmation(args, command):
    if not bool(getattr(args, "confirm", False)):
        raise EventAdminError(
            f"{command} requires --confirm after reviewing the event explanation"
        )


def build_parser():
    parser = argparse.ArgumentParser(
        description="Read and operate the face-attendance event ledger."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    listing = subparsers.add_parser("list", help="List event summaries read-only.")
    _add_database_argument(listing)
    listing.add_argument("--state", default="")
    listing.add_argument("--reason", default="")
    listing.add_argument("--camera", default="")
    listing.add_argument("--branch", default="")
    listing.add_argument("--direction", default="")
    listing.add_argument("--employee", default="")
    listing.add_argument("--since", default="")
    listing.add_argument("--until", default="")
    listing.add_argument("--limit", type=int, default=50)
    listing.add_argument("--offset", type=int, default=0)

    for name in ("inspect", "explain"):
        command = subparsers.add_parser(
            name,
            help=f"{name.capitalize()} one event read-only.",
        )
        _add_database_argument(command)
        command.add_argument("event_id")
        command.add_argument(
            "--include-paths",
            action="store_true",
            help="Include local source and retention paths.",
        )

    reprocess = subparsers.add_parser(
        "reprocess",
        help="Audit and requeue a safe pre-delivery event.",
    )
    _add_database_argument(reprocess)
    _add_actor_reason(reprocess)
    reprocess.add_argument("event_id")
    reprocess.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    reprocess.add_argument("--media-path", type=Path, default=None)
    reprocess.add_argument("--publish-lease-seconds", type=int, default=120)
    reprocess.add_argument("--confirm", action="store_true")

    resolve = subparsers.add_parser(
        "resolve-quarantine",
        help="Resolve quarantined evidence by requeueing or dismissing it.",
    )
    _add_database_argument(resolve)
    _add_actor_reason(resolve)
    resolve.add_argument("event_id")
    resolve.add_argument("--resolution", choices=("reprocess", "dismiss"), required=True)
    resolve.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    resolve.add_argument("--media-path", type=Path, default=None)
    resolve.add_argument("--publish-lease-seconds", type=int, default=120)
    resolve.add_argument(
        "--acknowledge-erpnext-checked",
        action="store_true",
        help="Required before dismissing an event with ambiguous delivery.",
    )
    resolve.add_argument("--confirm", action="store_true")

    dismiss = subparsers.add_parser(
        "dismiss",
        help="Audit and dismiss a local event without retrying delivery.",
    )
    _add_database_argument(dismiss)
    _add_actor_reason(dismiss)
    dismiss.add_argument("event_id")
    dismiss.add_argument(
        "--acknowledge-erpnext-checked",
        action="store_true",
        help="Required for uncertain or delivery-started events.",
    )
    dismiss.add_argument("--confirm", action="store_true")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command in {"list", "inspect", "explain"}:
            state = ReadOnlyEventState(args.database)
            if args.command == "list":
                emit(
                    {
                        "ok": True,
                        "events": state.list_events(
                            state=args.state,
                            reason=args.reason,
                            camera=args.camera,
                            branch=args.branch,
                            direction=args.direction,
                            employee=args.employee,
                            since=args.since,
                            until=args.until,
                            limit=args.limit,
                            offset=args.offset,
                        ),
                    }
                )
                return 0
            payload = (
                state.inspect_event(args.event_id, include_paths=args.include_paths)
                if args.command == "inspect"
                else state.explain_event(args.event_id, include_paths=args.include_paths)
            )
            if payload is None:
                raise EventAdminError(f"event does not exist: {args.event_id}")
            emit({"ok": True, args.command: payload})
            return 0

        _require_confirmation(args, args.command)
        state = mutable_event_state(args.database)
        try:
            if args.command == "reprocess":
                result = reprocess_event(
                    state,
                    event_id=args.event_id,
                    actor=args.actor,
                    reason=args.reason,
                    config_path=args.config,
                    media_path=args.media_path,
                    action="reprocess_requested",
                    publish_lease_seconds=args.publish_lease_seconds,
                )
            elif args.command == "resolve-quarantine":
                event = state.event_details(args.event_id, include_history=False)
                if event is None:
                    raise EventAdminError(f"event does not exist: {args.event_id}")
                if event.get("retention_state") != "quarantined":
                    raise EventAdminError("event is not currently quarantined")
                if args.resolution == "reprocess":
                    result = reprocess_event(
                        state,
                        event_id=args.event_id,
                        actor=args.actor,
                        reason=args.reason,
                        config_path=args.config,
                        media_path=args.media_path,
                        action="quarantine_requeued",
                        publish_lease_seconds=args.publish_lease_seconds,
                    )
                else:
                    result = state.dismiss_event(
                        args.event_id,
                        actor=args.actor,
                        reason=args.reason,
                        action="quarantine_dismissed",
                        acknowledge_delivery_checked=args.acknowledge_erpnext_checked,
                    )
            elif args.command == "dismiss":
                result = state.dismiss_event(
                    args.event_id,
                    actor=args.actor,
                    reason=args.reason,
                    acknowledge_delivery_checked=args.acknowledge_erpnext_checked,
                )
            else:
                raise EventAdminError(f"unsupported command: {args.command}")
        except Exception as exc:
            _audit_denied(
                state,
                actor=getattr(args, "actor", ""),
                action=args.command,
                event_id=getattr(args, "event_id", ""),
                error=exc,
            )
            raise
        emit({"ok": True, "result": result})
        return 0
    except (EventAdminError, EventOperationError, RuntimeStateError, ValueError) as exc:
        parser.exit(1, f"event-admin error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
