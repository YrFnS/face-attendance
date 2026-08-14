import argparse
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

import cv2

import face_attendance as attendance
from camera_sources import (
    CameraSourceError,
    load_camera_sources,
    receipt_path,
    source_for_upload_path,
    verify_source_receipt,
)
from event_ledger import make_capture_id, timestamp_from_unix, utc_now
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


def _move_path(source, destination):
    try:
        os.replace(source, destination)
    except OSError:
        shutil.move(str(source), str(destination))


def quarantine(path, reason, cfg, *, dry_run=False):
    if dry_run:
        return None
    if not bool(cfg.get("quarantine_invalid_uploads", True)):
        return None
    source = Path(path)
    source_receipt = receipt_path(source)
    folder = attendance.LOGS / "quarantine" / str(reason)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    destination = folder / f"{stamp}_{source.name}"
    counter = 1
    while destination.exists() or receipt_path(destination).exists():
        destination = folder / f"{stamp}_{counter}_{source.name}"
        counter += 1
    try:
        _move_path(source, destination)
    except OSError:
        return None
    if source_receipt.exists():
        try:
            _move_path(source_receipt, receipt_path(destination))
        except OSError:
            try:
                source_receipt.unlink()
            except FileNotFoundError:
                pass
    return destination


def remove_source(path, cfg):
    if not bool(cfg.get("delete_camera_uploads_after_processing", True)):
        return
    for candidate in (Path(path), receipt_path(path)):
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass


def reject_file(path, reason, cfg, *, dry_run=False):
    if dry_run:
        attendance.log(f"dry run: would reject ftp:{Path(path).name} reason={reason}")
        return "not_retained"
    moved = quarantine(path, reason, cfg, dry_run=dry_run)
    if moved:
        attendance.log(f"ftp:{Path(path).name}: quarantined reason={reason} path={moved}")
        return "quarantined"
    attendance.log(f"ftp:{Path(path).name}: rejected reason={reason}")
    if bool(cfg.get("delete_rejected_camera_uploads", False)):
        remove_source(path, cfg)
        return "deleted"
    return "retained"


def _finish_reason(status, error, reason_code):
    if reason_code:
        return reason_code
    if status == "checkin_created":
        return "checkin_created"
    if status in {"processed", "processed_no_checkin"}:
        return "processed_no_checkin"
    if status == "rejected":
        return "generic_rejected"
    if status == "failed":
        return "generic_failed"
    if status == "uncertain":
        return "generic_failed"
    return "generic_failed"


def finish_claim(
    state,
    claim,
    event_id,
    status,
    error="",
    *,
    reason_code="",
    event_updates=None,
    detail=None,
):
    if not claim or not claim.accepted:
        return False
    lifecycle = {
        "checkin_created": "checkin_created",
        "processed": "processed",
        "processed_no_checkin": "processed",
        "rejected": "rejected",
        "failed": "failed",
        "uncertain": "uncertain",
    }.get(status, "failed")
    try:
        state.transition_event(
            event_id,
            to_state=lifecycle,
            reason_code=_finish_reason(status, error, reason_code),
            actor_type="watcher",
            detail=detail or {},
            event_updates=event_updates or {},
            compatibility_status=status,
            error=error,
        )
        return True
    except Exception as exc:
        attendance.log(
            f"event-state finalization failed event={event_id or '-'} "
            f"status={status}: {exc}"
        )
        return False


def transition_claim(
    state,
    claim,
    event_id,
    *,
    to_state,
    reason_code,
    event_updates=None,
    detail=None,
    compatibility_status="processing",
):
    if not claim or not claim.accepted:
        return False
    state.transition_event(
        event_id,
        to_state=to_state,
        reason_code=reason_code,
        actor_type="watcher",
        detail=detail or {},
        event_updates=event_updates or {},
        compatibility_status=compatibility_status,
    )
    return True


def event_versions(gallery, cfg):
    metadata = dict(
        getattr(getattr(gallery, "reloader", None), "metadata", {}) or {}
    )
    return {
        "gallery_version": str(metadata.get("gallery_version") or ""),
        "gallery_generated_at": str(metadata.get("generated_at") or ""),
        "gallery_model": str(metadata.get("model") or cfg.get("model") or ""),
        "gallery_model_version": str(
            metadata.get("model_version") or cfg.get("model_version") or ""
        ),
        "recognition_model": str(cfg.get("model") or ""),
        "recognition_model_version": str(cfg.get("model_version") or ""),
        "preprocessing_version": str(
            cfg.get("preprocessing_version")
            or f"det-{cfg.get('det_size', 0)}-scale-{cfg.get('process_scale', 1.0)}"
        ),
        "policy_version": str(
            cfg.get("attendance_policy_version") or "directional-v1"
        ),
    }


def pad_reason_code(value):
    value = str(value or "")
    if value.startswith("pad_expected_one_face_found_"):
        return "pad_single_face_required"
    if value.startswith("pad_face_limit_exceeded_"):
        return "pad_face_limit"
    if value == "no_face_for_pad":
        return "no_face"
    return "pad_rejected"


def pad_result_for_index(pad_results, index, pad_gate):
    if 0 <= index - 1 < len(pad_results):
        return pad_results[index - 1]
    return PADResult(
        False,
        None,
        getattr(pad_gate, "expected_provider", "")
        or getattr(pad_gate, "provider", ""),
        reason="not_evaluated",
        face_index=index,
        face_count=max(index, len(pad_results)),
        skipped=True,
    )


def record_pad_only_decisions(
    state,
    claim,
    event_id,
    faces,
    pad_results,
    pad_gate,
    cfg,
    gallery,
    log_type,
    reason_code,
):
    if not claim or not claim.accepted:
        return
    versions = event_versions(gallery, cfg)
    face_count = len(faces)
    for index, face in enumerate(faces, start=1):
        result = pad_result_for_index(pad_results, index, pad_gate)
        width, height = attendance.face_size(face)
        state.record_recognition_decision(
            event_id=event_id,
            face_index=index,
            face_count=face_count,
            bbox=face_bbox(face),
            face_width=width,
            face_height=height,
            detection_score=float(face.det_score),
            best_employee="",
            best_score=0.0,
            runner_up_score=0.0,
            score_margin=0.0,
            pad_passed=bool(result.passed),
            pad_skipped=bool(result.skipped),
            pad_score=result.score,
            pad_provider=result.provider or "",
            pad_model=result.model or "",
            pad_evidence_id=result.evidence_id or "",
            pad_binding_id=result.binding_id or "",
            accepted=False,
            reason_code=reason_code,
            candidate_log_type=log_type,
            retention_state="not_retained",
            **versions,
        )


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
    sources=None,
):
    path = Path(path)
    claim = None
    event_id = ""
    claim_finalized = False
    try:
        if not attendance.wait_until_stable(path):
            attendance.log(f"ftp:{path.name}: skipped unstable file")
            return False

        stat = path.stat()
        source_sha256, source_size = file_sha256(path)
        sources = tuple(sources or load_camera_sources(cfg, ROOT))
        camera_source = source_for_upload_path(sources, path)
        camera_id = camera_source.camera_id
        log_type = camera_source.policy
        event_id = make_event_id(camera_id, log_type, source_sha256)
        capture_id = make_capture_id(
            camera_id,
            source_sha256,
            path.name,
            source_size,
            stat.st_mtime,
        )
        if not dry_run:
            received_at = utc_now()
            claim = state.record_event_receipt(
                event_id=event_id,
                capture_id=capture_id,
                camera_id=camera_id,
                log_type=log_type,
                source_sha256=source_sha256,
                source_name=path.name,
                source_mtime=stat.st_mtime,
                source_size=source_size,
                received_at=received_at,
                effective_at=received_at,
                branch=camera_source.branch,
                source_type=camera_source.source_type,
                source_principal=camera_source.ftp_username,
                source_binding_id=camera_source.binding_id,
                policy=log_type,
                source_at=timestamp_from_unix(stat.st_mtime),
                source_time_provenance="filesystem_mtime_untrusted",
                receipt_state="pending",
                receipt_verified=False,
                receipt_detail={
                    "receipt_file": receipt_path(path).name,
                    "receipt_present": receipt_path(path).exists(),
                    "upload_route": camera_source.upload_route,
                },
                policy_version=str(
                    cfg.get("attendance_policy_version") or "directional-v1"
                ),
            )
            if not claim.accepted:
                attendance.log(
                    f"ftp:{path.name}: replay skipped event={claim.event_id} "
                    f"camera={camera_id} policy={log_type} "
                    f"status={claim.existing_status}"
                )
                if (
                    bool(cfg.get("delete_camera_uploads_after_processing", True))
                    and bool(cfg.get("delete_duplicate_camera_uploads", True))
                ):
                    remove_source(path, cfg)
                return False

        max_bytes = int(cfg.get("max_camera_upload_bytes", 20 * 1024 * 1024))
        if max_bytes and source_size > max_bytes:
            retention = reject_file(path, "upload_too_large", cfg, dry_run=dry_run)
            claim_finalized = finish_claim(
                state,
                claim,
                event_id,
                status="rejected",
                error="upload_too_large",
                reason_code="upload_too_large",
                event_updates={"retention_state": retention},
            )
            return False

        try:
            verified_source, source_receipt = verify_source_receipt(
                path,
                cfg,
                ROOT,
                source_sha256=source_sha256,
                source_size=source_size,
                sources=sources,
            )
        except CameraSourceError as exc:
            retention = reject_file(
                path, "source_binding_invalid", cfg, dry_run=dry_run
            )
            claim_finalized = finish_claim(
                state,
                claim,
                event_id,
                status="rejected",
                error=f"source_binding:{exc}"[:2000],
                reason_code="source_binding_invalid",
                event_updates={
                    "receipt_state": "invalid",
                    "retention_state": retention,
                },
            )
            attendance.log(
                f"ftp:{path.name}: source binding rejected camera={camera_id} "
                f"policy={log_type} error={exc}"
            )
            return False
        if verified_source != camera_source:
            raise CameraSourceError("verified source does not match the upload route")

        receipt_detail = {
            "camera_id": source_receipt.camera_id,
            "source_type": source_receipt.source_type,
            "branch": source_receipt.branch,
            "policy": source_receipt.policy,
            "ftp_username": source_receipt.ftp_username,
            "remote_ip": source_receipt.remote_ip,
            "received_at": source_receipt.received_at,
            "source_sha256": source_receipt.source_sha256,
            "source_size": source_receipt.source_size,
            "source_binding_id": source_receipt.source_binding_id,
            "signature": source_receipt.signature,
            "verified": bool(source_receipt.verified),
        }
        if not dry_run:
            transition_claim(
                state,
                claim,
                event_id,
                to_state="source_verified",
                reason_code="source_verified",
                event_updates={
                    "transport_received_at": source_receipt.received_at,
                    "source_remote_ip": source_receipt.remote_ip,
                    "receipt_state": (
                        "verified" if source_receipt.verified else "route_only"
                    ),
                    "receipt_verified": bool(source_receipt.verified),
                    "receipt_json": receipt_detail,
                },
            )

        receipt_ip = source_receipt.remote_ip or "-"
        receipt_state = "verified" if source_receipt.verified else "route-only"
        source_label = (
            f"camera={camera_id} policy={log_type} branch={camera_source.branch} "
            f"principal={camera_source.ftp_username} ip={receipt_ip} "
            f"receipt={receipt_state}"
        )
        attendance.log(f"ftp:{path.name}: source bound {source_label}")

        time_error = event_time_error(path, cfg, allow_stale=allow_stale)
        if time_error:
            retention = reject_file(path, time_error, cfg, dry_run=dry_run)
            claim_finalized = finish_claim(
                state,
                claim,
                event_id,
                status="rejected",
                error=time_error,
                reason_code=time_error,
                event_updates={"retention_state": retention},
            )
            return False

        image = cv2.imread(str(path))
        if image is None:
            retention = reject_file(path, "unreadable_image", cfg, dry_run=dry_run)
            claim_finalized = finish_claim(
                state,
                claim,
                event_id,
                status="rejected",
                error="unreadable_image",
                reason_code="unreadable_image",
                event_updates={"retention_state": retention},
            )
            return False
        height, width = image.shape[:2]
        max_pixels = int(cfg.get("max_camera_image_pixels", 20_000_000))
        if max_pixels and width * height > max_pixels:
            retention = reject_file(path, "image_too_large", cfg, dry_run=dry_run)
            claim_finalized = finish_claim(
                state,
                claim,
                event_id,
                status="rejected",
                error="image_too_large",
                reason_code="image_too_large",
                event_updates={"retention_state": retention},
            )
            return False

        if not dry_run:
            transition_claim(
                state,
                claim,
                event_id,
                to_state="validating",
                reason_code="image_validated",
                detail={"width": width, "height": height},
            )

        detect_frame = attendance.scaled_frame(image, cfg)
        faces = ordered_faces(app.get(detect_frame))
        if not faces:
            retention = reject_file(path, "no_face", cfg, dry_run=dry_run)
            claim_finalized = finish_claim(
                state,
                claim,
                event_id,
                status="rejected",
                error="no_face",
                reason_code="no_face",
                event_updates={"retention_state": retention},
            )
            return False

        pad_results, pad_crops, pad_event_error = evaluate_pad(
            detect_frame,
            faces,
            cfg,
            pad_gate,
            {
                "camera_id": camera_id,
                "log_type": log_type,
                "branch": camera_source.branch,
                "source_principal": camera_source.ftp_username,
                "source_remote_ip": source_receipt.remote_ip,
                "source_binding_id": camera_source.binding_id,
                "event_id": event_id,
                "source_name": path.name,
                "source_sha256": source_sha256,
            },
        )
        if pad_event_error:
            reason_code = pad_reason_code(pad_event_error)
            record_pad_only_decisions(
                state,
                claim,
                event_id,
                faces,
                pad_results,
                pad_gate,
                cfg,
                gallery,
                log_type,
                reason_code,
            )
            retention = reject_file(path, "pad_rejected", cfg, dry_run=dry_run)
            claim_finalized = finish_claim(
                state,
                claim,
                event_id,
                status="rejected",
                error=f"pad:{pad_event_error}"[:2000],
                reason_code=reason_code,
                event_updates={"retention_state": retention},
            )
            return False

        for result, crop in zip(pad_results, pad_crops):
            score_text = "-" if result.score is None else f"{result.score:.3f}"
            attendance.log(
                f"ftp:{path.name}: pad face={result.face_index}/{result.face_count} "
                f"{source_label} provider={result.provider or '-'} "
                f"model={result.model or '-'} passed={int(result.passed)} "
                f"score={score_text} reason={result.reason or '-'} "
                f"evidence={result.evidence_id or '-'} "
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
            record_pad_only_decisions(
                state,
                claim,
                event_id,
                faces,
                pad_results,
                pad_gate,
                cfg,
                gallery,
                log_type,
                "pad_rejected",
            )
            retention = reject_file(path, "pad_rejected", cfg, dry_run=dry_run)
            claim_finalized = finish_claim(
                state,
                claim,
                event_id,
                status="rejected",
                error=f"pad:{reasons}"[:2000],
                reason_code="pad_rejected",
                event_updates={"retention_state": retention},
            )
            return False

        known = gallery.refresh()
        versions = event_versions(gallery, cfg)
        pad_provider = next(
            (result.provider for result in pad_results if result.provider),
            getattr(pad_gate, "provider", ""),
        )
        pad_model = next(
            (result.model for result in pad_results if result.model),
            "",
        )
        if not dry_run:
            transition_claim(
                state,
                claim,
                event_id,
                to_state="recognizing",
                reason_code="recognition_started",
                event_updates={
                    **versions,
                    "pad_provider": pad_provider or "",
                    "pad_model": pad_model or "",
                },
            )

        def persist_decision(decision):
            if dry_run or not claim or not claim.accepted:
                return None
            index = int(decision["face_index"])
            pad_result = pad_result_for_index(pad_results, index, pad_gate)
            return state.record_recognition_decision(
                event_id=event_id,
                pad_passed=bool(pad_result.passed),
                pad_skipped=bool(pad_result.skipped),
                pad_score=pad_result.score,
                pad_provider=pad_result.provider or "",
                pad_model=pad_result.model or "",
                pad_evidence_id=pad_result.evidence_id or "",
                pad_binding_id=pad_result.binding_id or "",
                policy_version=versions["policy_version"],
                gallery_version=versions["gallery_version"],
                gallery_generated_at=versions["gallery_generated_at"],
                gallery_model=versions["gallery_model"],
                gallery_model_version=versions["gallery_model_version"],
                recognition_model=versions["recognition_model"],
                recognition_model_version=versions["recognition_model_version"],
                preprocessing_version=versions["preprocessing_version"],
                **decision,
            )

        bound_app = BoundFaceApp(faces)
        recognition_cfg = dict(cfg)
        recognition_cfg["log_type"] = log_type
        try:
            created = attendance.process_image(
                image,
                f"ftp:{path.name} event={event_id[:12]} {source_label}",
                bound_app,
                known,
                recognition_cfg,
                dry_run,
                attach_source=None,
                decision_callback=persist_decision,
            )
            if bound_app.calls != 1:
                raise RuntimeError("recognition did not consume the bound face set exactly once")
        except Exception as exc:
            retention = reject_file(
                path, "processing_failed", cfg, dry_run=dry_run
            )
            claim_finalized = finish_claim(
                state,
                claim,
                event_id,
                status="failed",
                error=str(exc),
                reason_code="processing_failed",
                event_updates={"retention_state": retention},
            )
            attendance.log(
                f"ftp:{path.name}: processing failed event={event_id} "
                f"{source_label}: {exc}"
            )
            return False

        if not dry_run:
            if bool(cfg.get("delete_camera_uploads_after_processing", True)):
                remove_source(path, cfg)
                retention = "deleted"
            else:
                retention = "retained"
        else:
            retention = "not_retained"
        claim_finalized = finish_claim(
            state,
            claim,
            event_id,
            status="checkin_created" if created else "processed_no_checkin",
            reason_code="checkin_created" if created else "processed_no_checkin",
            event_updates={"retention_state": retention},
        )
        return bool(created)
    except (FileNotFoundError, ValueError) as exc:
        retention = reject_file(path, "invalid_upload", cfg, dry_run=dry_run)
        if claim and claim.accepted and not claim_finalized:
            claim_finalized = finish_claim(
                state,
                claim,
                event_id,
                status="failed",
                error=f"invalid_upload:{exc}",
                reason_code="invalid_upload",
                event_updates={"retention_state": retention},
            )
        attendance.log(f"ftp:{path.name}: rejected before processing: {exc}")
        return False
    except Exception as exc:
        if claim and claim.accepted and not claim_finalized:
            finish_claim(
                state,
                claim,
                event_id,
                status="failed",
                error=f"unexpected:{exc}",
                reason_code="unexpected_error",
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
        sources = load_camera_sources(cfg, ROOT)
    except CameraSourceError as exc:
        raise SystemExit(f"Invalid camera source configuration: {exc}") from exc

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
    source_summary = ",".join(
        f"{item.camera_id}:{item.policy}:{item.ftp_username}"
        for item in sources
    )
    attendance.log(
        f"secure folder watcher started: {folder}; pad={pad_gate.provider}; "
        f"production_mode={int(bool(cfg.get('production_mode', False)))}; "
        f"sources={source_summary}"
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
                    sources=sources,
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
    parser = argparse.ArgumentParser(description="Source-bound camera upload watcher.")
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
