from pathlib import Path
import json, shutil
from data_contract import GalleryError, employee_directory, safe_log_value, validate_employee_id, validate_employee_name, validate_gallery_label
class _App:
    def post(self, *args, **kwargs): return lambda function: function
    def get(self, *args, **kwargs): return lambda function: function
app = _App()
def login_required(function): return function
def csrf_protected(function): return function

@app.post("/upload")
@login_required
@csrf_protected
def upload():
    cfg = load_config()
    if not cfg.get("local_enrollment_enabled"):
        abort(403)
    try:
        employee = validate_employee_id(request.form.get("employee"))
        folder = employee_directory(FACES, employee)
    except GalleryError as exc:
        return redirect(url_for("index", msg=str(exc), error="1"))
    files = request.files.getlist("photos")
    max_files = max(1, int(cfg.get("max_enrollment_files_per_request", 20)))
    if len(files) > max_files:
        return redirect(url_for("index", msg=f"Maximum {max_files} files", error="1"))
    max_bytes = max(1024, int(cfg.get("max_enrollment_image_bytes", 10485760)))
    max_pixels = max(1, int(cfg.get("max_enrollment_image_pixels", 20000000)))
    folder.mkdir(parents=True, exist_ok=True)
    start = sum(path.suffix.lower() in ALLOWED for path in folder.iterdir())
    saved = rejected = 0
    for file in files:
        raw = file.read(max_bytes + 1)
        if (
            Path(file.filename or "").suffix.lower() not in ALLOWED
            or not raw
            or len(raw) > max_bytes
        ):
            rejected += 1
            continue
        image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None or image.shape[0] * image.shape[1] > max_pixels:
            rejected += 1
            continue
        ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 95])
        if not ok:
            rejected += 1
            continue
        saved += 1
        (folder / f"{start + saved:03d}.jpg").write_bytes(encoded.tobytes())
    audit("enrollment_upload", {"employee": employee, "saved": saved, "rejected": rejected})
    return redirect(url_for("index", msg=f"Uploaded {saved}; rejected {rejected}"))

@app.get("/api/faces/embeddings")
def export_embeddings():
    cfg = load_config()
    if not cfg.get("embedding_export_enabled"):
        abort(404)
    if not export_allowed(cfg):
        abort(401)
    try:
        data = payload(cfg)
    except GalleryError as exc:
        return jsonify(error=str(exc)), 503
    try:
        requested = validate_gallery_label(
            request.args.get("branch"),
            "branch",
            required=False,
            max_chars=128,
        )
    except GalleryError as exc:
        return jsonify(error=str(exc)), 400
    actual = str(data.get("branch") or "")
    if requested and requested != actual:
        abort(404)
    checksum = str(data["checksum"])
    response = (
        make_response("", 304)
        if request.if_none_match and request.if_none_match.contains(checksum)
        else make_response(jsonify(data))
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["ETag"] = f'"{checksum}"'
    return response

def copy_faces(source, dest, manifest_path, dry_run):
    cfg = load_config()
    manifest = load_manifest(manifest_path)
    report = {"copied": [], "skipped": []}

    for folder in sorted(source.iterdir(), key=lambda path: path.name):
        if folder.is_symlink():
            report["skipped"].append(
                {"folder": safe_log_value(folder.name), "reason": "symlinked folder"}
            )
            continue
        if not folder.is_dir():
            continue

        files = image_files(folder)
        if not files:
            report["skipped"].append(
                {"folder": folder.name, "reason": "no supported images"}
            )
            continue

        matches = find_employee(cfg, folder.name, manifest)
        if len(matches) != 1:
            report["skipped"].append(
                {
                    "folder": folder.name,
                    "reason": "employee match count is not 1",
                    "matches": matches,
                    "image_count": len(files),
                }
            )
            continue

        try:
            employee_id = validate_employee_id(matches[0].get("name"))
            employee_name = validate_employee_name(
                matches[0].get("employee_name"), "employee_name"
            )
            target = employee_directory(dest, employee_id)
        except GalleryError as exc:
            report["skipped"].append(
                {
                    "folder": folder.name,
                    "reason": str(exc),
                    "image_count": len(files),
                }
            )
            continue
        copied = []
        if not dry_run:
            target.mkdir(parents=True, exist_ok=True)

        for index, file_path in enumerate(files, start=1):
            if file_path.is_symlink():
                continue
            suffix = file_path.suffix.lower()
            target_path = target / f"local_{index:03d}{suffix}"
            if not dry_run and not target_path.exists():
                shutil.copy2(file_path, target_path)
            copied.append(str(target_path))

        report["copied"].append(
            {
                "folder": folder.name,
                "employee": employee_id,
                "employee_name": employee_name,
                "source": matches[0].get("source", "frappe"),
                "image_count": len(copied),
                "files": copied,
            }
        )

    REPORT.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"matched {len(report['copied'])} folder(s)")
    print(f"skipped {len(report['skipped'])} folder(s)")
    print(f"report: {REPORT}")
