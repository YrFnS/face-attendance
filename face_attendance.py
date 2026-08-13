import argparse
import json
import os
import shlex
import subprocess
import time
from datetime import datetime
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


ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"
FACES = ROOT / "faces"
EMBEDDINGS = ROOT / "embedding_gallery.json"
LEGACY_EMBEDDINGS = ROOT / "embeddings.pkl"
SYNC_STATUS = ROOT / "embedding_sync_status.json"
LOGS = ROOT / "logs"
COOLDOWN_STATE = ROOT / "cooldown_state.json"
COOLDOWN_LOCK = ROOT / "cooldown_state.lock"


def log(message):
    LOGS.mkdir(exist_ok=True)
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}"
    with (LOGS / "watch.log").open("a", encoding="utf-8") as file:
        file.write(line + "\n")
    try:
        print(line, flush=True)
    except OSError:
        pass


def load_config():
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"Missing config file: {CONFIG}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {CONFIG}: {exc}") from exc


def face_app(det_size=None):
    cfg = load_config()
    det_size = int(det_size or cfg.get("det_size", 640))
    app = FaceAnalysis(
        name=cfg.get("model", "buffalo_l"),
        allowed_modules=cfg.get("allowed_modules", ["detection", "recognition"]),
        providers=["CPUExecutionProvider"],
    )
    app.prepare(ctx_id=-1, det_size=(det_size, det_size))
    return app


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
    employee_part = employee or "unknown"
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
    path = folder / f"{stamp}_{employee}_{score:.3f}.jpg"
    if not cv2.imwrite(str(path), crop):
        log(f"could not save checkin crop: {path}")
        return None, False
    return path, temporary


def build_embeddings():
    cfg = load_config()
    FACES.mkdir(exist_ok=True)
    app = face_app(cfg.get("build_det_size", 640))
    employees = []
    min_score = float(cfg.get("build_min_detection_score", 0.6))

    for employee_dir in sorted(FACES.iterdir()):
        if not employee_dir.is_dir():
            continue
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
                    "employee": employee_dir.name,
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
        expected_model=cfg.get("model", "buffalo_l"),
        expected_model_version=cfg.get("model_version"),
        expected_branch=cfg.get("branch_name", ""),
        require_model_match=True,
        require_model_version_match=bool(
            cfg.get("require_model_version_match", False)
        ),
        allow_empty=False,
        max_embeddings_per_employee=int(
            cfg.get("max_embeddings_per_employee", 50)
        ),
    )
    print(
        f"saved {EMBEDDINGS}: {metadata['employee_count']} employee(s), "
        f"{metadata['embedding_count']} embedding(s)"
    )


def enroll_from_camera(employee, photos, delay):
    cfg = load_config()
    if not bool(cfg.get("local_enrollment_enabled", False)):
        raise SystemExit("Local image enrollment is disabled in config.json")
    app = face_app()
    out_dir = FACES / employee
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
    reloader = GalleryReloader(
        EMBEDDINGS,
        expected_model=cfg.get("model", "buffalo_l"),
        expected_model_version=cfg.get("model_version"),
        expected_branch=cfg.get("branch_name", ""),
        require_model_match=bool(cfg.get("require_model_match", True)),
        require_model_version_match=bool(
            cfg.get("require_model_version_match", False)
        ),
        allow_empty=bool(cfg.get("allow_empty_embedding_gallery", False)),
    )
    known, _, _ = reloader.reload(force=True)
    return known


class GalleryRuntime:
    def __init__(self, cfg):
        self.cfg = cfg
        self.last_sync_attempt = 0.0
        self.rejected_signature = None
        self.reloader = GalleryReloader(
            EMBEDDINGS,
            expected_model=cfg.get("model", "buffalo_l"),
            expected_model_version=cfg.get("model_version"),
            expected_branch=cfg.get("branch_name", ""),
            require_model_match=bool(cfg.get("require_model_match", True)),
            require_model_version_match=bool(
                cfg.get("require_model_version_match", False)
            ),
            allow_empty=bool(cfg.get("allow_empty_embedding_gallery", False)),
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
        max_age = int(self.cfg.get("embedding_max_age_seconds", 86400))
        status = gallery_status(EMBEDDINGS, max_age_seconds=max_age)
        if status.get("stale"):
            message = (
                f"embedding gallery is stale: age={status['age_seconds']}s "
                f"max={max_age}s"
            )
            if bool(self.cfg.get("reject_stale_embedding_gallery", False)):
                raise GalleryError(message)
            log(message)

    def start(self):
        migrate_legacy_embeddings(self.cfg)
        self.maybe_sync(force=True)
        try:
            known, metadata, _ = self.reloader.reload(force=True)
        except GalleryError as exc:
            raise SystemExit(
                f"No valid embedding gallery is available: {exc}. "
                "Run 'python sync_embeddings.py' or enable local enrollment and run "
                "'python face_attendance.py build'."
            ) from exc
        self.check_freshness()
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
                    log(f"embedding reload rejected; keeping previous gallery: {exc}")
                    self.rejected_signature = rejected_signature
                return self.reloader.known
            raise


def load_cooldown_state():
    if not COOLDOWN_STATE.exists():
        return {}
    try:
        return json.loads(COOLDOWN_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_cooldown_state(last_seen):
    COOLDOWN_STATE.write_text(
        json.dumps(last_seen, indent=2, sort_keys=True), encoding="utf-8"
    )


def acquire_cooldown_lock(timeout=10):
    start = time.time()
    while True:
        try:
            return os.open(
                str(COOLDOWN_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY
            )
        except FileExistsError:
            if time.time() - start > timeout:
                raise TimeoutError("cooldown lock timed out")
            time.sleep(0.1)


def release_cooldown_lock(lock_fd):
    os.close(lock_fd)
    try:
        COOLDOWN_LOCK.unlink()
    except FileNotFoundError:
        pass


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


def create_checkin_api(employee, log_type, image_path=None):
    cfg = load_config()
    doc = {
        "employee": employee,
        "log_type": log_type,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    response = requests.post(
        f"{cfg['frappe_url'].rstrip('/')}/api/resource/Employee%20Checkin",
        headers=api_headers(),
        json=doc,
        timeout=30,
    )
    response.raise_for_status()
    docname = response.json()["data"]["name"]
    if image_path:
        with open(image_path, "rb") as file:
            upload = requests.post(
                f"{cfg['frappe_url'].rstrip('/')}/api/method/upload_file",
                headers=api_headers(json_request=False),
                data={
                    "doctype": "Employee Checkin",
                    "docname": docname,
                    "is_private": "1",
                },
                files={"file": (image_path.name, file, "image/jpeg")},
                timeout=30,
            )
        upload.raise_for_status()
        log(f"checkin attachment added: {docname} {image_path.name}")
    log(f"checkin created: {employee} {log_type} {docname}")


def create_checkin_bench(employee, log_type, image_path=None):
    doc = {
        "doctype": "Employee Checkin",
        "employee": employee,
        "log_type": log_type,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    inserted = bench_execute("frappe.client.insert", {"doc": doc})
    docname = inserted["name"]
    if image_path:
        try:
            attach_image("Employee Checkin", docname, image_path)
            log(f"checkin attachment added: {docname} {image_path.name}")
        except subprocess.CalledProcessError as exc:
            log(f"checkin attachment failed: {docname} {exc}")
    log(f"checkin created: {employee} {log_type} {docname}")


def create_checkin(employee, log_type, image_path=None):
    cfg = load_config()
    if (
        cfg.get("frappe_url")
        and cfg.get("frappe_api_key")
        and cfg.get("frappe_api_secret")
    ):
        return create_checkin_api(employee, log_type, image_path)
    return create_checkin_bench(employee, log_type, image_path)


def create_checkin_with_cooldown(
    employee, cfg, image_path, dry_run=False, log_type=None
):
    log_type = log_type or cfg["log_type"]
    lock_fd = acquire_cooldown_lock()
    try:
        last_seen = load_cooldown_state()
        now = time.time()
        remaining = int(cfg["cooldown_seconds"]) - int(
            now - last_seen.get(employee, 0)
        )
        if remaining > 0:
            log(f"cooldown skip: {employee} {remaining}s remaining")
            return False
        if dry_run:
            log(f"dry run: would create {employee} {log_type}")
            return True
        create_checkin(employee, log_type, image_path)
        last_seen[employee] = now
        save_cooldown_state(last_seen)
        return True
    finally:
        release_cooldown_lock(lock_fd)


def log_type_for_path(cfg, path):
    if not path:
        return cfg["log_type"]
    parts = {part.lower() for part in Path(path).parts}
    folder = "out" if "out" in parts else "in" if "in" in parts else ""
    return cfg.get("folder_log_types", {}).get(folder, cfg["log_type"])


def process_image(
    image,
    source_name,
    app,
    known,
    cfg,
    dry_run=False,
    attach_source=None,
):
    detect_frame = scaled_frame(image, cfg)
    faces = app.get(detect_frame)
    if not faces:
        log(f"{source_name}: no faces")
        return False

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
        prefix = f"{source_name} face={index}/{len(faces)}"
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
            save_rejected(crop, "quality", cfg, employee, score)
            continue

        log(f"{prefix} match={employee} score={score:.3f} margin={margin:.3f}")
        if (
            not employee
            or score < float(cfg["threshold"])
            or margin < float(cfg.get("min_score_margin", 0.0))
            or employee in seen_this_image
        ):
            save_rejected(crop, "unknown", cfg, employee, score)
            continue

        seen_this_image.add(employee)
        image_path, temporary = save_checkin_image(crop, employee, score, cfg)
        attachment_path = (
            image_path if bool(cfg.get("attach_checkin_crop", True)) else None
        )
        try:
            created_now = create_checkin_with_cooldown(
                employee,
                cfg,
                attachment_path,
                dry_run,
                log_type_for_path(cfg, attach_source),
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
            gallery_status(
                EMBEDDINGS,
                max_age_seconds=cfg.get("embedding_max_age_seconds", 86400),
            ),
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

    run = sub.add_parser("watch")
    run.add_argument("--once", action="store_true")
    run.add_argument("--dry-run", action="store_true")

    folder = sub.add_parser("watch-folder")
    folder.add_argument("--once", action="store_true")
    folder.add_argument("--dry-run", action="store_true")
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
