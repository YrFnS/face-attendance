import argparse
import os
import shutil
import time
from pathlib import Path

import cv2

import face_attendance as attendance
from pad import PADConfigError, PADGate, PADResult
from production_readiness import check_production_readiness, format_report
from runtime_state import RuntimeState, file_sha256, make_event_id, resolve_runtime_path


ROOT = Path(__file__).resolve().parent


def service_config():
    cfg = attendance.load_config()
    # Network synchronization is deliberately separated from recognition. The
    # systemd timer writes the gallery atomically and GalleryRuntime hot-reloads it.
    cfg["embedding_sync_enabled"] = bool(
        cfg.get("embedding_sync_inline_enabled", False)
    )
    return cfg


def state_for_config(cfg):
    path = resolve_runtime_path(ROOT, cfg.get("runtime_state_db"), "runtime_state.sqlite3")
    return RuntimeState(path)


def image_files(folder):
    return [
        path
        for path in attendance.image_files(folder)
        if ".incoming" not in path.parts and not path.name.startswith(".")
    ]


def direction_for_path(path):
    parts = {part.lower() for part in Path(path).parts}
    if "out" in parts:
        return "out"
    if "in" in parts:
        return "in"
    return ""


def camera_context(cfg, path):
    direction = direction_for_path(path)
    log_type = cfg.get("folder_log_types", {}).get(
        direction, cfg.get("log_type", "IN")
    )
    camera_ids = cfg.get("camera_ids") or {}
    camera_id = str(
        camera_ids.get(direction)
        or camera_ids.get(str(log_type).lower())
        or direction
        or "default-camera"
    )
    return camera_id, str(log_type)


def event_time_error(path, cfg, allow_stale=False):
    stat = Path(path).stat()
    now = time.time()
    future_tolerance = max(
        0, int(cfg.get("camera_event_future_tolerance_seconds", 60))
    )
    if stat.st_mtime > now + future_tolerance:
        return "future_timestamp"
    max_age = max(0, int(cfg.get("max_camera_event_age_seconds", 600)))
    if not allow_stale and max_age and now - stat.st_mtime > max_age:
        return "stale_event"
    return ""


def quarantine(path, reason, cfg, *, dry_run=False):
    if dry_run:
        return None
    if not bool(cfg.get("quarantine_invalid_uploads", True)):
        return None
    source = Path(path)
    folder = attendance.LOGS / "quarantine" / str(reason)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    destination = folder / f"{stamp}_{source.name}"
    counter = 1
    while destination.exists():
        destination = folder / f"{stamp}_{counter}_{source.name}"
        counter += 1
    try:
        os.replace(source, destination)
        return destination
    except OSError:
        try:
            shutil.move(str(source), str(destination))
            return destination
        except FileNotFoundError:
            return None


def remove_source(path, cfg):
    if not bool(cfg.get("delete_camera_uploads_after_processing", True)):
        return
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass


def reject_file(path, reason, cfg, *, dry_run=False):
    if dry_run:
        attendance.log(f"dry run: would reject ftp:{Path(path).name} reason={reason}")
        return
    moved = quarantine(path, reason, cfg, dry_run=dry_run)
    if moved:
        attendance.log(f"ftp:{Path(path).name}: quarantined reason={reason} path={moved}")
    else:
        attendance.log(f"ftp:{Path(path).name}: rejected reason={reason}")
        if bool(cfg.get("delete_rejected_camera_uploads", False)):
            remove_source(path, cfg)


def evaluate_pad(image, app, cfg, pad_gate, context):
    if not pad_gate.enabled and not pad_gate.required:
        return None, None

    detect_frame = attendance.scaled_frame(image, cfg)
    faces = app.get(detect_frame)
    if not faces:
        return PADResult(False, None, pad_gate.provider, reason="no_face_for_pad"), None
    if bool(cfg.get("pad_require_single_face", True)) and len(faces) != 1:
        return (
            PADResult(
                False,
                None,
                pad_gate.provider,
                reason=f"pad_expected_one_face_found_{len(faces)}",
            ),
            None,
        )

    face = max(
        faces,
        key=lambda item: (item.bbox[2] - item.bbox[0])
        * (item.bbox[3] - item.bbox[1]),
    )
    width, height = attendance.face_size(face)
    if (
        width < int(cfg.get("min_face_width", 65))
        or height < int(cfg.get("min_face_height", 80))
        or float(face.det_score) < float(cfg.get("min_detection_score", 0.5))
    ):
        return PADResult(False, None, pad_gate.provider, reason="pad_face_quality"), None

    crop = attendance.face_crop(
        detect_frame, face, margin=float(cfg.get("pad_crop_margin", 0.25))
    )
    return pad_gate.evaluate(crop, context), crop


def process_path(
    path,
    app,
    known,
    gallery,
    cfg,
    state,
    pad_gate,
    *,
    dry_run=False,
    allow_stale=False,
):
    path = Path(path)
    try:
        if not attendance.wait_until_stable(path):
            attendance.log(f"ftp:{path.name}: skipped unstable file")
            return False
        time_error = event_time_error(path, cfg, allow_stale=allow_stale)
        if time_error:
            reject_file(path, time_error, cfg, dry_run=dry_run)
            return False
        max_bytes = int(cfg.get("max_camera_upload_bytes", 20 * 1024 * 1024))
        source_sha256, source_size = file_sha256(path, max_bytes=max_bytes)
        camera_id, log_type = camera_context(cfg, path)
        event_id = make_event_id(camera_id, log_type, source_sha256)
        claim = None
        if not dry_run:
            claim = state.claim_event(
                event_id=event_id,
                camera_id=camera_id,
                log_type=log_type,
                source_sha256=source_sha256,
                source_name=path.name,
                source_mtime=path.stat().st_mtime,
                source_size=source_size,
            )
            if not claim.accepted:
                attendance.log(
                    f"ftp:{path.name}: replay skipped event={claim.event_id} "
                    f"status={claim.existing_status}"
                )
                if (
                    bool(cfg.get("delete_camera_uploads_after_processing", True))
                    and bool(cfg.get("delete_duplicate_camera_uploads", True))
                ):
                    remove_source(path, cfg)
                return False

        image = cv2.imread(str(path))
        if image is None:
            if claim:
                state.finish_event(event_id, status="rejected", error="unreadable_image")
            reject_file(path, "unreadable_image", cfg, dry_run=dry_run)
            return False
        height, width = image.shape[:2]
        max_pixels = int(cfg.get("max_camera_image_pixels", 20_000_000))
        if max_pixels and width * height > max_pixels:
            if claim:
                state.finish_event(event_id, status="rejected", error="image_too_large")
            reject_file(path, "image_too_large", cfg, dry_run=dry_run)
            return False

        pad_result, pad_crop = evaluate_pad(
            image,
            app,
            cfg,
            pad_gate,
            {
                "camera_id": camera_id,
                "log_type": log_type,
                "event_id": event_id,
                "source_name": path.name,
                "source_sha256": source_sha256,
            },
        )
        if pad_result is not None:
            score_text = "-" if pad_result.score is None else f"{pad_result.score:.3f}"
            attendance.log(
                f"ftp:{path.name}: pad provider={pad_result.provider} "
                f"passed={int(pad_result.passed)} score={score_text} "
                f"reason={pad_result.reason or '-'} evidence={pad_result.evidence_id or '-'}"
            )
            if not pad_result.passed:
                if claim:
                    state.finish_event(
                        event_id,
                        status="rejected",
                        error=f"pad:{pad_result.reason}"[:2000],
                    )
                if pad_crop is not None and getattr(pad_crop, "size", 0):
                    attendance.save_rejected(pad_crop, "pad", cfg)
                reject_file(path, "pad_rejected", cfg, dry_run=dry_run)
                return False

        known = gallery.refresh()
        try:
            created = attendance.process_image(
                image,
                f"ftp:{path.name} event={event_id[:12]} camera={camera_id}",
                app,
                known,
                cfg,
                dry_run,
                attach_source=path,
            )
        except Exception as exc:
            if claim:
                state.finish_event(event_id, status="failed", error=str(exc))
            attendance.log(f"ftp:{path.name}: processing failed event={event_id}: {exc}")
            reject_file(path, "processing_failed", cfg, dry_run=dry_run)
            return False

        if claim:
            state.finish_event(
                event_id,
                status="checkin_created" if created else "processed_no_checkin",
            )
        if not dry_run:
            remove_source(path, cfg)
        return bool(created)
    except (FileNotFoundError, ValueError) as exc:
        attendance.log(f"ftp:{path.name}: rejected before processing: {exc}")
        reject_file(path, "invalid_upload", cfg, dry_run=dry_run)
        return False


def run(*, once=False, dry_run=False, allow_stale=False):
    cfg = service_config()
    report = check_production_readiness(
        cfg,
        ROOT,
        verify_model_files=bool(cfg.get("model_integrity_verify_on_start", True)),
    )
    for issue in report.issues:
        attendance.log(
            f"production readiness {issue.severity}: {issue.code}: {issue.message}"
        )
    if bool(cfg.get("production_mode", False)) and not dry_run and report.blockers:
        raise SystemExit("Production readiness check failed:\n" + format_report(report))

    try:
        pad_gate = PADGate(cfg)
    except PADConfigError as exc:
        if bool(cfg.get("pad_required", False)) or bool(cfg.get("production_mode", False)):
            raise SystemExit(f"Invalid PAD configuration: {exc}") from exc
        attendance.log(f"PAD disabled after configuration error: {exc}")
        fallback = dict(cfg)
        fallback.update(pad_provider="disabled", pad_required=False)
        pad_gate = PADGate(fallback)

    folder = Path(cfg.get("camera_uploads_dir", ROOT / "camera_uploads"))
    folder.mkdir(parents=True, exist_ok=True)
    state = state_for_config(cfg)
    app = attendance.face_app()
    gallery = attendance.GalleryRuntime(cfg)
    known = gallery.start()
    attendance.cleanup_old_audit_files(cfg)
    state.prune_events(cfg.get("event_retention_days", 30))
    last_cleanup = time.monotonic()
    attendance.log(
        f"secure folder watcher started: {folder}; pad={pad_gate.provider}; "
        f"production_mode={int(bool(cfg.get('production_mode', False)))}"
    )

    while True:
        created = False
        known = gallery.refresh()
        for path in image_files(folder):
            created = (
                process_path(
                    path,
                    app,
                    known,
                    gallery,
                    cfg,
                    state,
                    pad_gate,
                    dry_run=dry_run,
                    allow_stale=allow_stale,
                )
                or created
            )
        if time.monotonic() - last_cleanup >= 3600:
            attendance.cleanup_old_audit_files(cfg)
            removed = state.prune_events(cfg.get("event_retention_days", 30))
            if removed:
                attendance.log(f"event-state cleanup removed {removed} old event(s)")
            last_cleanup = time.monotonic()
        if once:
            return created
        time.sleep(max(0.25, float(cfg.get("folder_poll_seconds", 1.0))))


def main():
    parser = argparse.ArgumentParser(description="Replay-resistant camera upload watcher.")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--allow-stale",
        action="store_true",
        help="Allow old camera files for controlled testing only.",
    )
    args = parser.parse_args()
    run(once=args.once, dry_run=args.dry_run, allow_stale=args.allow_stale)


if __name__ == "__main__":
    main()
