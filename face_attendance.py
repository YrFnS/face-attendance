import argparse
import json
import os
import pickle
import shlex
import subprocess
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from insightface.app import FaceAnalysis


ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"
FACES = ROOT / "faces"
EMBEDDINGS = ROOT / "embeddings.pkl"
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
    return json.loads(CONFIG.read_text())


def face_app(det_size=None):
    cfg = load_config()
    det_size = int(det_size or cfg.get("det_size", 640))
    app = FaceAnalysis(name=cfg.get("model", "buffalo_s"), providers=["CPUExecutionProvider"])
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
    return max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))


def norm(vector):
    vector = np.asarray(vector, dtype=np.float32)
    return vector / np.linalg.norm(vector)


def face_crop(image, face, margin=0.25):
    h, w = image.shape[:2]
    x1, y1, x2, y2 = [float(v) for v in face.bbox]
    bw, bh = x2 - x1, y2 - y1
    pad = margin * max(bw, bh)
    x1 = max(0, int(x1 - pad))
    y1 = max(0, int(y1 - pad))
    x2 = min(w, int(x2 + pad))
    y2 = min(h, int(y2 + pad))
    return image[y1:y2, x1:x2]


def face_size(face):
    x1, y1, x2, y2 = [float(v) for v in face.bbox]
    return x2 - x1, y2 - y1


def save_rejected(crop, reason, employee=None, score=None):
    if crop.size == 0:
        return
    folder = LOGS / "unknown"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    emp = employee or "unknown"
    score_part = "" if score is None else f"_{score:.3f}"
    cv2.imwrite(str(folder / f"{stamp}_{reason}_{emp}{score_part}.jpg"), crop)


def save_checkin_image(crop, employee, score):
    folder = LOGS / "checkins"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = folder / f"{stamp}_{employee}_{score:.3f}.jpg"
    cv2.imwrite(str(path), crop)
    return path


def build_embeddings():
    cfg = load_config()
    app = face_app(cfg.get("build_det_size", 640))
    known = []
    for employee_dir in sorted(FACES.iterdir()):
        if not employee_dir.is_dir():
            continue
        vectors = []
        for image_path in sorted(employee_dir.glob("*")):
            image = cv2.imread(str(image_path))
            if image is None:
                continue
            face = best_face(app, image)
            if face is not None:
                vectors.append(norm(face.embedding))
        if vectors:
            known.append(
                {
                    "employee": employee_dir.name,
                    "embedding": norm(np.mean(vectors, axis=0)),
                    "embeddings": vectors,
                }
            )
            print(f"{employee_dir.name}: {len(vectors)} photo(s)")
    if not known:
        raise SystemExit(f"No usable faces found under {FACES}")
    EMBEDDINGS.write_bytes(pickle.dumps(known))
    print(f"saved {EMBEDDINGS}")


def enroll_from_camera(employee, photos, delay):
    cfg = load_config()
    app = face_app()
    out_dir = FACES / employee
    out_dir.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(cfg["camera_url"], cv2.CAP_FFMPEG)
    if not cap.isOpened():
        raise SystemExit("Could not open camera RTSP stream")
    saved = 0
    last = 0
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
        if face.det_score < 0.75:
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


def load_embeddings():
    if not EMBEDDINGS.exists():
        raise SystemExit("Run build or enroll first")
    return pickle.loads(EMBEDDINGS.read_bytes())


def load_cooldown_state():
    if not COOLDOWN_STATE.exists():
        return {}
    try:
        return json.loads(COOLDOWN_STATE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_cooldown_state(last_seen):
    COOLDOWN_STATE.write_text(json.dumps(last_seen, indent=2, sort_keys=True))


def acquire_cooldown_lock(timeout=10):
    start = time.time()
    while True:
        try:
            return os.open(str(COOLDOWN_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
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


def match_employee(known, embedding):
    embedding = norm(embedding)
    scores = []
    for item in known:
        vectors = item.get("embeddings") or [item["embedding"]]
        scores.append((max(float(np.dot(vector, embedding)) for vector in vectors), item["employee"]))
    return max(scores, default=(0.0, None))


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
    cmd = ["wsl", "-d", "Ubuntu-24.04", "--", "bash", "-lc", command]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    output = result.stdout.strip()
    return json.loads(output) if output else None


def windows_to_wsl_path(path):
    path = path.resolve()
    drive = path.drive.rstrip(":").lower()
    parts = [part for part in path.parts[1:]]
    return "/mnt/" + drive + "/" + "/".join(parts)


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
    cmd = ["wsl", "-d", "Ubuntu-24.04", "--", "bash", "-lc", command]
    subprocess.run(cmd, input=script, check=True, capture_output=True, text=True)


def attach_image(doctype, docname, image_path):
    bench_console(
        "\n".join(
            [
                "from pathlib import Path",
                "import frappe",
                "from frappe.utils.file_manager import save_file",
                f"path = Path({windows_to_wsl_path(image_path)!r})",
                f"save_file(path.name, path.read_bytes(), {doctype!r}, {docname!r}, is_private=1)",
                "frappe.db.commit()",
            ]
        )
    )


def create_checkin(employee, log_type, image_path=None):
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


def create_checkin_with_cooldown(employee, cfg, image_path, dry_run=False):
    lock_fd = acquire_cooldown_lock()
    try:
        last_seen = load_cooldown_state()
        now = time.time()
        remaining = int(cfg["cooldown_seconds"]) - int(now - last_seen.get(employee, 0))
        if remaining > 0:
            log(f"cooldown skip: {employee} {remaining}s remaining")
            return False
        if dry_run:
            log(f"dry run: would create {employee} {cfg['log_type']}")
            return True
        create_checkin(employee, cfg["log_type"], image_path)
        last_seen[employee] = now
        save_cooldown_state(last_seen)
        return True
    finally:
        release_cooldown_lock(lock_fd)


def process_image(image, source_name, app, known, cfg, last_seen, dry_run=False, attach_source=None):
    detect_frame = scaled_frame(image, cfg)
    faces = app.get(detect_frame)
    if not faces:
        log(f"{source_name}: no faces")
        return False

    created = False
    seen_this_image = set()
    for index, face in enumerate(faces, start=1):
        x1, y1, x2, y2 = [max(0, int(v)) for v in face.bbox]
        crop = detect_frame[y1:y2, x1:x2]
        if crop.size:
            cv2.imwrite(str(LOGS / f"latest_face_{index}.jpg"), crop)
            cv2.imwrite(str(LOGS / "latest_face.jpg"), crop)
        width, height = face_size(face)
        score, employee = match_employee(known, face.embedding)
        prefix = f"{source_name} face={index}/{len(faces)}"
        if (
            width < int(cfg["min_face_width"])
            or height < int(cfg["min_face_height"])
            or face.det_score < float(cfg["min_detection_score"])
        ):
            log(
                f"{prefix} rejected=size_or_detection "
                f"size={width:.0f}x{height:.0f} det={float(face.det_score):.3f} "
                f"best={employee} score={score:.3f}"
            )
            save_rejected(crop, "quality", employee, score)
            continue
        log(f"{prefix} match={employee} score={score:.3f}")
        if not employee or score < float(cfg["threshold"]) or employee in seen_this_image:
            save_rejected(crop, "unknown", employee, score)
            continue
        seen_this_image.add(employee)
        image_path = attach_source or save_checkin_image(crop, employee, score)
        created = create_checkin_with_cooldown(employee, cfg, image_path, dry_run) or created
    return created


def watch(once=False, dry_run=False):
    cfg = load_config()
    app = face_app()
    known = load_embeddings()
    cap = cv2.VideoCapture(cfg["camera_url"], cv2.CAP_FFMPEG)
    if not cap.isOpened():
        raise SystemExit("Could not open camera RTSP stream")

    last_seen = load_cooldown_state()
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
        created = process_image(frame, "rtsp", app, known, cfg, last_seen, dry_run)
        if once and created:
            break
    cap.release()


def image_files(folder):
    exts = {".jpg", ".jpeg", ".png"}
    return sorted((p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in exts), key=lambda p: p.stat().st_mtime)


def wait_until_stable(path):
    last_size = -1
    for _ in range(20):
        size = path.stat().st_size
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
    known = load_embeddings()
    last_seen = load_cooldown_state()
    seen = set() if scan_existing else {str(path) for path in image_files(folder)}
    log(f"folder watcher started: {folder}")

    while True:
        created = False
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
            created = process_image(image, f"ftp:{path.name}", app, known, cfg, last_seen, dry_run, attach_source=path) or created
        if once and created:
            break
        if scan_existing:
            break
        time.sleep(1)


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build")
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
    elif args.cmd == "enroll":
        enroll_from_camera(args.employee, args.photos, args.delay)
    elif args.cmd == "watch":
        watch(once=args.once, dry_run=args.dry_run)
    elif args.cmd == "watch-folder":
        watch_folder(once=args.once, dry_run=args.dry_run, scan_existing=args.scan_existing)


if __name__ == "__main__":
    main()
