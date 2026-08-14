import argparse
import json
import os
import shlex
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np
import requests
from insightface.app import FaceAnalysis

from embedding_gallery import (
    GalleryError,
    GalleryReloader,
    build_gallery_payload,
    gallery_signature,
    gallery_status,
    match_employee,
    norm,
    sync_gallery,
    write_gallery_atomic,
)
from data_contract import (
    employee_directory,
    employee_filename_token,
    employee_id_from_storage_component,
    filename_token,
    safe_log_message,
    validate_employee_id,
    validate_erp_docname,
    validate_log_type,
)
from erpnext_adapter import (
    BenchERPNextAdapter,
    EmployeeCheckinRequest,
    RESTERPNextAdapter,
    erp_event_time as adapter_erp_event_time,
    select_erpnext_transport,
)
from model_runtime import ModelRuntimeError, create_face_analysis
from secret_store import ConfigLoadError, load_runtime_config
from runtime_policy import (
    effective_gallery_options,
    enforce_gallery_freshness,
    inspect_gallery,
    load_runtime_gallery,
)
from watcher_entrypoints import require_legacy_dry_run


ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"
FACES = ROOT / "faces"
EMBEDDINGS = ROOT / "embedding_gallery.json"
LEGACY_EMBEDDINGS = ROOT / "embeddings.pkl"
SYNC_STATUS = ROOT / "embedding_sync_status.json"
LOGS = ROOT / "logs"


def log(message):
    LOGS.mkdir(exist_ok=True)
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} " + safe_log_message(message)
    with (LOGS / "watch.log").open("a", encoding="utf-8") as file:
        file.write(line + "\n")
    try:
        print(line, flush=True)
    except OSError:
        pass


def load_config():
    try:
        return load_runtime_config(CONFIG)
    except ConfigLoadError as exc:
        raise SystemExit(str(exc)) from exc




def face_app(
    det_size=None,
    *,
    cfg=None,
    verified_model_directory=None,
):
    cfg = cfg or load_config()
    det_size = int(det_size or cfg.get("det_size", 640))
    try:
        return create_face_analysis(
            FaceAnalysis,
            cfg,
            ROOT,
            det_size=det_size,
            verified_model_directory=verified_model_directory,
        )
    except (ModelRuntimeError, ValueError) as exc:
        raise SystemExit(f"Face model runtime validation failed: {exc}") from exc


def scaled_frame(frame, cfg):
    scale = float(cfg.get("process_scale", 1.0))
    if scale <= 1:
        return frame
    return cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)


def best_face(app, image):
    faces = app.get(image)
    if not faces:
        return None
    return max(
        faces,
        key=lambda face: (face.bbox[2] - face.bbox[0])
        * (face.bbox[3] - face.bbox[1]),
    )


def face_crop(image, face, margin=0.25):
    height, width = image.shape[:2]
    x1, y1, x2, y2 = [float(value) for value in face.bbox]
    box_width, box_height = x2 - x1, y2 - y1
    padding = margin * max(box_width, box_height)
    x1 = max(0, int(x1 - padding))
    y1 = max(0, int(y1 - padding))
    x2 = min(width, int(x2 + padding))
    y2 = min(height, int(y2 + padding))
    return image[y1:y2, x1:x2]


def face_size(face):
    x1, y1, x2, y2 = [float(value) for value in face.bbox]
    return x2 - x1, y2 - y1


def save_rejected(crop, reason, cfg, employee=None, score=None):
    if not bool(cfg.get("save_rejected_crops", True)) or crop.size == 0:
        return None
    folder = LOGS / "unknown"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    reason = filename_token(reason, "rejection reason")
    employee_part = employee_filename_token(employee) if employee else "unknown"
    score_part = "" if score is None else f"_{score:.3f}"
    path = folder / f"{stamp}_{reason}_{employee_part}{score_part}.jpg"
    cv2.imwrite(str(path), crop)
    return path


def save_checkin_image(crop, employee, score, cfg):
    keep_crop = bool(cfg.get("save_checkin_crops", True))
    attach_crop = bool(cfg.get("attach_checkin_crop", True))
    if crop.size == 0 or not (keep_crop or attach_crop):
        return None, False

    temporary = not keep_crop
    folder = LOGS / ("tmp" if temporary else "checkins")
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    employee = validate_employee_id(employee)
    path = folder / f"{stamp}_{employee_filename_token(employee)}_{score:.3f}.jpg"
    if not cv2.imwrite(str(path), crop):
        log(f"could not save checkin crop: {path}")
        return None, False
    return path, temporary


def build_embeddings():
    cfg = load_config()
    FACES.mkdir(exist_ok=True)
    app = face_app(cfg.get("build_det_size", 640), cfg=cfg)
    employees = []
    min_score = float(cfg.get("build_min_detection_score", 0.6))

    for employee_dir in sorted(FACES.iterdir()):
        if employee_dir.is_symlink():
            raise SystemExit("Enrollment directory must not be a symbolic link")
        if not employee_dir.is_dir():
            continue
        employee_id = employee_id_from_storage_component(employee_dir.name)
        vectors = []
        for image_path in sorted(employee_dir.glob("*")):
            image = cv2.imread(str(image_path))
            if image is None:
                print(f"{employee_dir.name}: skipped unreadable {image_path.name}")
                continue
            face = best_face(app, image)
            if face is None:
                print(f"{employee_dir.name}: skipped no-face {image_path.name}")
                continue
            if float(face.det_score) < min_score:
                print(
                    f"{employee_dir.name}: skipped weak face {image_path.name} "
                    f"score={float(face.det_score):.3f}"
                )
                continue
            vectors.append(norm(face.embedding))

        if vectors:
            employees.append(
                {
                    "employee": employee_id,
                    "embedding": norm(np.mean(vectors, axis=0)),
                    "embeddings": vectors,
                }
            )
            print(f"{employee_dir.name}: {len(vectors)} embedding(s)")

    if not employees:
        raise SystemExit(f"No usable faces found under {FACES}")

    payload = build_gallery_payload(
        employees,
        model=cfg.get("model", "buffalo_l"),
        model_version=cfg.get("model_version", ""),
        branch=cfg.get("branch_name", ""),
    )
    _, metadata = write_gallery_atomic(
        EMBEDDINGS,
        payload,
        **effective_gallery_options(cfg),
    )
    print(
        f"saved {EMBEDDINGS}: {metadata['employee_count']} employee(s), "
        f"{metadata['embedding_count']} embedding(s)"
    )


def enroll_from_camera(employee, photos, delay):
    cfg = load_config()
    if not bool(cfg.get("local_enrollment_enabled", False)):
        raise SystemExit("Local image enrollment is disabled in config.json")
    employee = validate_employee_id(employee)
    app = face_app(cfg=cfg)
    out_dir = employee_directory(FACES, employee)
    out_dir.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(cfg["camera_url"], cv2.CAP_FFMPEG)
    if not cap.isOpened():
        raise SystemExit("Could not open camera RTSP stream")
    saved = 0
    last = 0.0
    while saved < photos:
        ok, frame = cap.read()
        if not ok:
            continue
        if time.time() - last < delay:
            continue
        detect_frame = scaled_frame(frame, cfg)
        faces = app.get(detect_frame)
        if len(faces) != 1:
            log(f"enroll skipped: expected 1 face, found {len(faces)}")
            continue
        face = faces[0]
        if float(face.det_score) < 0.75:
            log(f"enroll skipped: weak face score={float(face.det_score):.3f}")
            continue
        crop = face_crop(detect_frame, face)
        if crop.size == 0:
            continue
        saved += 1
        last = time.time()
        path = out_dir / f"{saved:02d}.jpg"
        cv2.imwrite(str(path), crop)
        print(f"saved {path}")
    cap.release()
    build_embeddings()


def migrate_legacy_embeddings(cfg):
    del cfg
    if EMBEDDINGS.exists() or not LEGACY_EMBEDDINGS.exists():
        return False
    raise GalleryError(
        "automatic embeddings.pkl migration is disabled because pickle "
        "deserialization can execute code. Stop attendance services, verify "
        "the file SHA-256 from a trusted record, and run "
        "'python legacy_gallery_converter.py --expected-sha256 <sha256> "
        "--acknowledge-pickle-code-execution-risk'"
    )


def load_embeddings():
    cfg = load_config()
    migrate_legacy_embeddings(cfg)
    known, _, _, _ = load_runtime_gallery(cfg, EMBEDDINGS)
    return known


class GalleryRuntime:
    def __init__(self, cfg):
        self.cfg = cfg
        self.last_sync_attempt = 0.0
        self.rejected_signature = None
        self.reloader = GalleryReloader(
            EMBEDDINGS,
            **effective_gallery_options(cfg),
        )

    def sync_enabled(self):
        return bool(self.cfg.get("embedding_sync_enabled", True)) and bool(
            self.cfg.get("central_url")
        )

    def maybe_sync(self, force=False):
        if not self.sync_enabled():
            return
        interval = max(
            10, int(self.cfg.get("embedding_sync_interval_seconds", 300))
        )
        now = time.monotonic()
        if not force and now - self.last_sync_attempt < interval:
            return
        self.last_sync_attempt = now
        try:
            result = sync_gallery(self.cfg, EMBEDDINGS, SYNC_STATUS)
            action = "updated" if result["changed"] else "unchanged"
            log(
                f"embedding sync {action}: version={result['gallery_version']} "
                f"employees={result['employee_count']} "
                f"embeddings={result['embedding_count']}"
            )
        except GalleryError as exc:
            log(f"embedding sync failed; keeping current gallery: {exc}")

    def check_freshness(self):
        status = enforce_gallery_freshness(
            self.cfg,
            self.reloader.generated_at,
            path=EMBEDDINGS,
        )
        if status.get("stale"):
            log(
                "embedding gallery is stale but permitted outside strict "
                f"production: age={status['age_seconds']}s "
                f"max={status['max_age_seconds']}s"
            )
        return status

    def start(self):
        migrate_legacy_embeddings(self.cfg)
        self.maybe_sync(force=True)
        try:
            known, metadata, _ = self.reloader.reload(force=True)
            self.check_freshness()
        except GalleryError as exc:
            raise SystemExit(
                f"No valid embedding gallery is available: {exc}. "
                "Run 'python sync_embeddings.py' or enable local enrollment and run "
                "'python face_attendance.py build'."
            ) from exc
        log(
            f"embedding gallery loaded: version={metadata.get('gallery_version')} "
            f"employees={metadata.get('employee_count')} "
            f"embeddings={metadata.get('embedding_count')}"
        )
        return known

    def refresh(self):
        self.maybe_sync()
        try:
            known, metadata, changed = self.reloader.reload()
            self.check_freshness()
            self.rejected_signature = None
            if changed:
                log(
                    f"embedding gallery reloaded: version={metadata.get('gallery_version')} "
                    f"employees={metadata.get('employee_count')} "
                    f"embeddings={metadata.get('embedding_count')}"
                )
            return known
        except GalleryError as exc:
            if self.reloader.known:
                rejected_signature = gallery_signature(EMBEDDINGS)
                if rejected_signature != self.rejected_signature:
                    log(
                        "embedding reload rejected; keeping previous gallery: "
                        f"{exc}"
                    )
                    self.rejected_signature = rejected_signature
                return self.reloader.known
            raise


def bench_execute(method, kwargs):
    cfg = load_config()
    command = " ".join(
        [
            "cd",
            shlex.quote(cfg["bench_dir"]),
            "&&",
            "bench",
            "--site",
            shlex.quote(cfg["site"]),
            "execute",
            shlex.quote(method),
            "--kwargs",
            shlex.quote(json.dumps(kwargs)),
        ]
    )
    cmd = (
        [
            "wsl",
            "-d",
            cfg.get("wsl_distro", "Ubuntu-24.04"),
            "--",
            "bash",
            "-lc",
            command,
        ]
        if os.name == "nt"
        else ["bash", "-lc", command]
    )
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    output = result.stdout.strip()
    return json.loads(output) if output else None


def windows_to_wsl_path(path):
    path = path.resolve()
    drive = path.drive.rstrip(":").lower()
    parts = [part for part in path.parts[1:]]
    return "/mnt/" + drive + "/" + "/".join(parts)


def bench_file_path(path):
    return windows_to_wsl_path(path) if os.name == "nt" else str(path.resolve())


def bench_console(script):
    cfg = load_config()
    command = " ".join(
        [
            "cd",
            shlex.quote(cfg["bench_dir"]),
            "&&",
            "bench",
            "--site",
            shlex.quote(cfg["site"]),
            "console",
        ]
    )
    cmd = (
        [
            "wsl",
            "-d",
            cfg.get("wsl_distro", "Ubuntu-24.04"),
            "--",
            "bash",
            "-lc",
            command,
        ]
        if os.name == "nt"
        else ["bash", "-lc", command]
    )
    subprocess.run(cmd, input=script, check=True, capture_output=True, text=True)


def attach_image(doctype, docname, image_path):
    bench_console(
        "\n".join(
            [
                "from pathlib import Path",
                "import frappe",
                "from frappe.utils.file_manager import save_file",
                f"path = Path({bench_file_path(image_path)!r})",
                f"save_file(path.name, path.read_bytes(), {doctype!r}, {docname!r}, is_private=1)",
                "frappe.db.commit()",
            ]
        )
    )


def api_headers(json_request=True):
    cfg = load_config()
    headers = {
        "Authorization": f"token {cfg['frappe_api_key']}:{cfg['frappe_api_secret']}"
    }
    if json_request:
        headers["Content-Type"] = "application/json"
    return headers


def erp_event_time(value=None):
    return adapter_erp_event_time(value)


def erpnext_transport_name(cfg=None):
    return select_erpnext_transport(cfg or load_config())


def create_checkin_api(employee, log_type, image_path=None, event_time=None):
    cfg = load_config()
    adapter = RESTERPNextAdapter(
        base_url=cfg.get("frappe_url"),
        api_key=cfg.get("frappe_api_key"),
        api_secret=cfg.get("frappe_api_secret"),
        allow_insecure=bool(cfg.get("allow_insecure_frappe_url", False)),
        session=requests,
        timeout_seconds=cfg.get("erpnext_request_timeout_seconds", 30),
    )
    request = EmployeeCheckinRequest.build(employee, log_type, event_time)
    result = adapter.create_employee_checkin(request, image_path)
    if image_path:
        log(f"checkin attachment added: {result.docname} {Path(image_path).name}")
    log(f"checkin created: {request.employee} {request.log_type} {result.docname}")
    return result.docname


def create_checkin_bench(employee, log_type, image_path=None, event_time=None):
    request = EmployeeCheckinRequest.build(employee, log_type, event_time)

    def attach(docname, path):
        attach_image("Employee Checkin", docname, path)
        log(f"checkin attachment added: {docname} {path.name}")

    def attachment_failed(exc):
        log(f"checkin attachment failed: {exc}")

    adapter = BenchERPNextAdapter(
        execute=bench_execute,
        attach=attach,
        attachment_error_handler=attachment_failed,
    )
    result = adapter.create_employee_checkin(request, image_path)
    log(f"checkin created: {request.employee} {request.log_type} {result.docname}")
    return result.docname


def create_checkin(employee, log_type, image_path=None, event_time=None):
    cfg = load_config()
    transport = select_erpnext_transport(cfg)
    if transport == "rest":
        return create_checkin_api(employee, log_type, image_path, event_time)
    return create_checkin_bench(employee, log_type, image_path, event_time)


def create_checkin_with_cooldown(
    employee, cfg, image_path, dry_run=False, log_type=None
):
    employee = validate_employee_id(employee)
    log_type = validate_log_type(log_type or cfg["log_type"])
    if dry_run:
        log(f"dry run: would create {employee} {log_type}")
        return True
    raise RuntimeError(
        "filesystem cooldown state has been removed; live attendance must use "
        "the canonical watcher transactional policy callback"
    )


def log_type_for_path(cfg, path):
    if not path:
        return validate_log_type(cfg["log_type"])
    parts = {part.lower() for part in Path(path).parts}
    folder = "out" if "out" in parts else "in" if "in" in parts else ""
    return validate_log_type(
        cfg.get("folder_log_types", {}).get(folder, cfg["log_type"])
    )


def process_image(
    image,
    source_name,
    app,
    known,
    cfg,
    dry_run=False,
    attach_source=None,
    decision_callback=None,
    attendance_callback=None,
):
    detect_frame = scaled_frame(image, cfg)
    faces = app.get(detect_frame)
    if not faces:
        log(f"{source_name}: no faces")
        return False

    selected_log_type = log_type_for_path(cfg, attach_source)

    def emit_decision(payload):
        if decision_callback is not None:
            return decision_callback(dict(payload))
        return None

    created = False
    seen_this_image = set()
    for index, face in enumerate(faces, start=1):
        x1, y1, x2, y2 = [max(0, int(value)) for value in face.bbox]
        crop = detect_frame[y1:y2, x1:x2]
        if crop.size and bool(cfg.get("save_latest_face", False)):
            LOGS.mkdir(exist_ok=True)
            cv2.imwrite(str(LOGS / f"latest_face_{index}.jpg"), crop)
            cv2.imwrite(str(LOGS / "latest_face.jpg"), crop)

        width, height = face_size(face)
        score, employee, margin = match_employee(known, face.embedding)
        runner_up_score = score - margin
        prefix = f"{source_name} face={index}/{len(faces)}"
        decision = {
            "face_index": index,
            "face_count": len(faces),
            "bbox": [x1, y1, x2, y2],
            "face_width": float(width),
            "face_height": float(height),
            "detection_score": float(face.det_score),
            "best_employee": employee or "",
            "best_score": float(score),
            "runner_up_score": float(runner_up_score),
            "score_margin": float(margin),
            "candidate_log_type": selected_log_type,
        }
        if (
            width < int(cfg["min_face_width"])
            or height < int(cfg["min_face_height"])
            or float(face.det_score) < float(cfg["min_detection_score"])
        ):
            log(
                f"{prefix} rejected=size_or_detection "
                f"size={width:.0f}x{height:.0f} "
                f"det={float(face.det_score):.3f} "
                f"best={employee} score={score:.3f} margin={margin:.3f}"
            )
            retained = save_rejected(crop, "quality", cfg, employee, score)
            emit_decision(
                {
                    **decision,
                    "accepted": False,
                    "reason_code": "quality_rejected",
                    "retention_state": "retained" if retained else "not_retained",
                }
            )
            continue

        log(f"{prefix} match={employee} score={score:.3f} margin={margin:.3f}")
        if not employee:
            reason_code = "unknown_employee"
        elif score < float(cfg["threshold"]):
            reason_code = "score_below_threshold"
        elif margin < float(cfg.get("min_score_margin", 0.0)):
            reason_code = "margin_below_threshold"
        elif employee in seen_this_image:
            reason_code = "duplicate_face"
        else:
            reason_code = ""
        if reason_code:
            retained = save_rejected(crop, "unknown", cfg, employee, score)
            emit_decision(
                {
                    **decision,
                    "accepted": False,
                    "reason_code": reason_code,
                    "retention_state": "retained" if retained else "not_retained",
                }
            )
            continue

        employee = validate_employee_id(employee)
        seen_this_image.add(employee)
        image_path, temporary = save_checkin_image(crop, employee, score, cfg)
        attachment_path = (
            image_path if bool(cfg.get("attach_checkin_crop", True)) else None
        )
        retention_state = (
            "temporary"
            if temporary and image_path
            else "retained"
            if image_path
            else "not_retained"
        )
        candidate = {
            **decision,
            "best_employee": employee,
            "accepted": True,
            "reason_code": "accepted_candidate",
            "retention_state": retention_state,
        }
        try:
            if attendance_callback is not None:
                created_now = attendance_callback(
                    employee=employee,
                    log_type=selected_log_type,
                    image_path=attachment_path,
                    dry_run=dry_run,
                    decision=dict(candidate),
                )
            else:
                emit_decision(candidate)
                created_now = create_checkin_with_cooldown(
                    employee,
                    cfg,
                    attachment_path,
                    dry_run,
                    selected_log_type,
                )
            created = created_now or created
        finally:
            if temporary and image_path:
                try:
                    image_path.unlink()
                except FileNotFoundError:
                    pass
    return created


def cleanup_old_audit_files(cfg):
    retention_days = int(cfg.get("audit_retention_days", 0))
    if retention_days <= 0:
        return
    cutoff = time.time() - retention_days * 86400
    removed = 0
    for folder in (LOGS / "unknown", LOGS / "checkins", LOGS / "tmp"):
        if not folder.exists():
            continue
        for path in folder.iterdir():
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except FileNotFoundError:
                pass
    if removed:
        log(f"audit cleanup removed {removed} expired file(s)")


def watch(once=False, dry_run=False):
    require_legacy_dry_run("watch", dry_run=dry_run)
    cfg = load_config()
    app = face_app()
    gallery = GalleryRuntime(cfg)
    known = gallery.start()
    cleanup_old_audit_files(cfg)
    last_cleanup = time.monotonic()

    cap = cv2.VideoCapture(cfg["camera_url"], cv2.CAP_FFMPEG)
    if not cap.isOpened():
        raise SystemExit("Could not open camera RTSP stream")

    frame_no = 0
    failures = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            failures += 1
            if failures >= 50:
                cap.release()
                time.sleep(5)
                cap = cv2.VideoCapture(cfg["camera_url"], cv2.CAP_FFMPEG)
                failures = 0
            time.sleep(1)
            continue
        failures = 0
        frame_no += 1
        if frame_no % int(cfg["frame_stride"]):
            continue

        known = gallery.refresh()
        created = process_image(frame, "rtsp", app, known, cfg, dry_run)
        if time.monotonic() - last_cleanup >= 3600:
            cleanup_old_audit_files(cfg)
            last_cleanup = time.monotonic()
        if once and created:
            break
    cap.release()


def image_files(folder):
    extensions = {".jpg", ".jpeg", ".png"}
    try:
        files = (
            path
            for path in folder.rglob("*")
            if path.is_file() and path.suffix.lower() in extensions
        )
        return sorted(files, key=lambda path: path.stat().st_mtime)
    except FileNotFoundError:
        return []


def wait_until_stable(path):
    last_size = -1
    for _ in range(20):
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return False
        if size == last_size:
            return True
        last_size = size
        time.sleep(0.25)
    return False


def watch_folder(once=False, dry_run=False, scan_existing=False):
    require_legacy_dry_run("watch-folder", dry_run=dry_run)
    cfg = load_config()
    folder = Path(cfg.get("camera_uploads_dir", ROOT / "camera_uploads"))
    folder.mkdir(parents=True, exist_ok=True)
    app = face_app()
    gallery = GalleryRuntime(cfg)
    known = gallery.start()
    cleanup_old_audit_files(cfg)
    last_cleanup = time.monotonic()
    seen = set() if scan_existing else {str(path) for path in image_files(folder)}
    log(f"folder watcher started: {folder}")

    while True:
        created = False
        known = gallery.refresh()
        for path in image_files(folder):
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            if not wait_until_stable(path):
                log(f"ftp:{path.name}: skipped unstable file")
                continue
            image = cv2.imread(str(path))
            if image is None:
                log(f"ftp:{path.name}: unreadable image")
                continue
            processed = False
            try:
                created = (
                    process_image(
                        image,
                        f"ftp:{path.name}",
                        app,
                        known,
                        cfg,
                        dry_run,
                        attach_source=path,
                    )
                    or created
                )
                processed = True
            finally:
                if processed and bool(
                    cfg.get("delete_camera_uploads_after_processing", False)
                ):
                    try:
                        path.unlink()
                        seen.discard(key)
                    except FileNotFoundError:
                        pass

        if time.monotonic() - last_cleanup >= 3600:
            cleanup_old_audit_files(cfg)
            last_cleanup = time.monotonic()
        if once and created:
            break
        if scan_existing:
            break
        time.sleep(1)


def sync_embeddings():
    cfg = load_config()
    try:
        result = sync_gallery(cfg, EMBEDDINGS, SYNC_STATUS)
    except GalleryError as exc:
        raise SystemExit(str(exc)) from exc
    action = "updated" if result["changed"] else "already current"
    print(
        f"embedding gallery {action}: {result['employee_count']} employee(s), "
        f"{result['embedding_count']} embedding(s)"
    )


def print_embedding_status():
    cfg = load_config()
    print(
        json.dumps(
            inspect_gallery(cfg, EMBEDDINGS),
            ensure_ascii=False,
            indent=2,
        )
    )


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build", help="Build a gallery from local enrollment images.")
    sub.add_parser("sync", help="Sync embeddings from the central server.")
    sub.add_parser("status", help="Show local embedding gallery status.")

    enroll = sub.add_parser("enroll")
    enroll.add_argument("employee")
    enroll.add_argument("--photos", type=int, default=5)
    enroll.add_argument("--delay", type=float, default=1.5)

    run = sub.add_parser(
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
    folder.add_argument("--scan-existing", action="store_true")
    args = parser.parse_args()

    if args.cmd == "build":
        build_embeddings()
    elif args.cmd == "sync":
        sync_embeddings()
    elif args.cmd == "status":
        print_embedding_status()
    elif args.cmd == "enroll":
        enroll_from_camera(args.employee, args.photos, args.delay)
    elif args.cmd == "watch":
        watch(once=args.once, dry_run=args.dry_run)
    elif args.cmd == "watch-folder":
        watch_folder(
            once=args.once,
            dry_run=args.dry_run,
            scan_existing=args.scan_existing,
        )


if __name__ == "__main__":
    main()
