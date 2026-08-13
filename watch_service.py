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


class BoundFaceApp:
    """Return the already PAD-evaluated faces exactly once to recognition."""

    def __init__(self, faces):
        self._faces = tuple(faces)
        self.calls = 0

    def get(self, _image):
        self.calls += 1
        if self.calls != 1:
            raise RuntimeError("bound face set was requested more than once")
        return list(self._faces)


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
        except OSError:
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


def finish_claim(state, claim, event_id, status, error=""):
    if not claim or not claim.accepted:
        return False
    try:
        state.finish_event(event_id, status=status, error=error)
        return True
    except Exception as exc:
        attendance.log(
            f"event-state finalization failed event={event_id or '-'} "
            f"status={status}: {exc}"
        )
        return False


def ordered_faces(faces):
    return sorted(
        list(faces or []),
        key=lambda face: (
            float(face.bbox[1]),
            float(face.bbox[0]),
            float(face.bbox[3]),
            float(face.bbox[2]),
        ),
    )


def face_bbox(face):
    return [int(round(float(value))) for value in face.bbox]


def evaluate_pad(detect_frame, faces, cfg, pad_gate, context):
    if not pad_gate.enabled and not pad_gate.required:
        return [], [], ""
    if not faces:
        return [], [], "no_face_for_pad"
    if len(faces) > int(pad_gate.max_faces):
        return [], [], f"pad_face_limit_exceeded_{len(faces)}"
    if bool(cfg.get("pad_require_single_face", True)) and len(faces) != 1:
        return [], [], f"pad_expected_one_face_found_{len(faces)}"

    results = []
    crops = []
    face_count = len(faces)
    for index, face in enumerate(faces, start=1):
        width, height = attendance.face_size(face)
        if (
            width < int(cfg.get("min_face_width", 65))
            or height < int(cfg.get("min_face_height", 80))
            or float(face.det_score) < float(cfg.get("min_detection_score", 0.5))
        ):
            results.append(
                PADResult(
                    False,
                    None,
                    pad_gate.expected_provider or pad_gate.provider,
                    reason="pad_face_quality",
                    face_index=index,
                    face_count=face_count,
                )
            )
            crops.append(None)
            continue

        crop = attendance.face_crop(
            detect_frame,
            face,
            margin=float(cfg.get("pad_crop_margin", 0.25)),
        )
        face_context = dict(context)
        face_context.update(
            face_index=index,
            face_count=face_count,
            bbox=face_bbox(face),
        )
        results.append(pad_gate.evaluate(crop, face_context))
        crops.append(crop)
    return results, crops, ""


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
    claim = None
    event_id = ""
    claim_finalized = False
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
            claim_finalized = finish_claim(
                state, claim, event_id, status="rejected", error="unreadable_image"
            )
            reject_file(path, "unreadable_image", cfg, dry_run=dry_run)
            return False
        height, width = image.shape[:2]
        max_pixels = int(cfg.get("max_camera_image_pixels", 20_000_000))
        if max_pixels and width * height > max_pixels:
            claim_finalized = finish_claim(
                state, claim, event_id, status="rejected", error="image_too_large"
            )
            reject_file(path, "image_too_large", cfg, dry_run=dry_run)
            return False

        detect_frame = attendance.scaled_frame(image, cfg)
        faces = ordered_faces(app.get(detect_frame))
        pad_results, pad_crops, pad_event_error = evaluate_pad(
            detect_frame,
            faces,
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
        if pad_event_error:
            claim_finalized = finish_claim(
                state,
                claim,
                event_id,
                status="rejected",
                error=f"pad:{pad_event_error}"[:2000],
            )
            reject_file(path, "pad_rejected", cfg, dry_run=dry_run)
            return False

        for result, crop in zip(pad_results, pad_crops):
            score_text = "-" if result.score is None else f"{result.score:.3f}"
            attendance.log(
                f"ftp:{path.name}: pad face={result.face_index}/{result.face_count} "
                f"provider={result.provider or '-'} model={result.model or '-'} "
                f"passed={int(result.passed)} score={score_text} "
                f"reason={result.reason or '-'} evidence={result.evidence_id or '-'} "
                f"binding={result.binding_id[:16] or '-'}"
            )
            if not result.passed and crop is not None and getattr(crop, "size", 0):
                attendance.save_rejected(crop, "pad", cfg)

        strict_pad_evidence = bool(cfg.get("production_mode", False)) or pad_gate.required
        if pad_results and not all(
            result.passed and not (strict_pad_evidence and result.skipped)
            for result in pad_results
        ):
            reasons = ",".join(
                f"{result.face_index}:{result.reason or 'rejected'}"
                for result in pad_results
                if not result.passed or (strict_pad_evidence and result.skipped)
            )
            claim_finalized = finish_claim(
                state,
                claim,
                event_id,
                status="rejected",
                error=f"pad:{reasons}"[:2000],
            )
            reject_file(path, "pad_rejected", cfg, dry_run=dry_run)
            return False

        known = gallery.refresh()
        bound_app = BoundFaceApp(faces)
        try:
            created = attendance.process_image(
                image,
                f"ftp:{path.name} event={event_id[:12]} camera={camera_id}",
                bound_app,
                known,
                cfg,
                dry_run,
                attach_source=path,
            )
            if bound_app.calls != 1:
                raise RuntimeError("recognition did not consume the bound face set exactly once")
        except Exception as exc:
            claim_finalized = finish_claim(
                state, claim, event_id, status="failed", error=str(exc)
            )
            attendance.log(f"ftp:{path.name}: processing failed event={event_id}: {exc}")
            reject_file(path, "processing_failed", cfg, dry_run=dry_run)
            return False

        claim_finalized = finish_claim(
            state,
            claim,
            event_id,
            status="checkin_created" if created else "processed_no_checkin",
        )
        if not dry_run:
            remove_source(path, cfg)
        return bool(created)
    except (FileNotFoundError, ValueError) as exc:
        if claim and claim.accepted and not claim_finalized:
            claim_finalized = finish_claim(
                state,
                claim,
                event_id,
                status="failed",
                error=f"invalid_upload:{exc}",
            )
        attendance.log(f"ftp:{path.name}: rejected before processing: {exc}")
        reject_file(path, "invalid_upload", cfg, dry_run=dry_run)
        return False
    except Exception as exc:
        if claim and claim.accepted and not claim_finalized:
            finish_claim(
                state,
                claim,
                event_id,
                status="failed",
                error=f"unexpected:{exc}",
            )
        attendance.log(f"ftp:{path.name}: unexpected processing error: {exc}")
        raise


def run(*, once=False, dry_run=False, allow_stale=False):
    cfg = service_config()
    report = check_production_readiness(
        cfg,
        ROOT,
        verify_model_files=bool(cfg.get("model_integrity_verify_on_start", True)),
        gallery_path=ROOT / "embedding_gallery.json",
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
    verified_model_directory = (
        report.model_integrity.get("model_directory")
        if report.model_integrity.get("ok")
        else None
    )
    app = attendance.face_app(
        cfg=cfg,
        verified_model_directory=verified_model_directory,
    )
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
