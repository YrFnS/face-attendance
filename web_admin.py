import json
import secrets
import subprocess
import sys
from pathlib import Path

from flask import (
    Flask,
    abort,
    jsonify,
    make_response,
    redirect,
    render_template_string,
    request,
    url_for,
)
from werkzeug.utils import secure_filename

from embedding_gallery import (
    GalleryError,
    gallery_status,
    load_gallery,
    read_sync_status,
    sync_gallery,
)


ROOT = Path(__file__).resolve().parent
FACES = ROOT / "faces"
CONFIG = ROOT / "config.json"
GALLERY = ROOT / "embedding_gallery.json"
SYNC_STATUS = ROOT / "embedding_sync_status.json"
ALLOWED = {".jpg", ".jpeg", ".png", ".webp"}
app = Flask(__name__)

PAGE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Face Attendance</title>
  <style>
  *{box-sizing:border-box}
  body{font-family:Inter,system-ui,Arial,sans-serif;margin:0;background:#f4f6f8;color:#17202a}
  .wrap{max-width:1120px;margin:0 auto;padding:34px 24px}
  .top{display:flex;justify-content:space-between;gap:20px;align-items:flex-end;margin-bottom:24px}
  h1{margin:0;font-size:30px}.sub{color:#667085;margin:6px 0 0;line-height:1.5}
  .grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px;margin-bottom:22px}
  .card{background:white;border:1px solid #e3e7ee;border-radius:10px;padding:20px;box-shadow:0 1px 2px #10182812}
  .stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-top:18px}
  .stat{background:#f8fafc;border:1px solid #e4e7ec;border-radius:8px;padding:14px}
  .stat strong{display:block;font-size:22px;margin-top:5px}.label{color:#667085;font-size:12px;text-transform:uppercase;letter-spacing:.04em}
  .ok{color:#067647}.warn{color:#b54708}.bad{color:#b42318}
  label{display:block;font-weight:650;margin-bottom:8px}
  input{font:inherit;width:100%;padding:11px 12px;border:1px solid #cfd6df;border-radius:6px;background:white}
  input[type=file]{padding:9px}.row{display:grid;gap:14px}
  button{font:inherit;font-weight:700;cursor:pointer;border:0;border-radius:6px;padding:11px 16px;background:#0f766e;color:white}
  .secondary{background:#1f2937}.full{width:100%;margin-top:10px}
  .msg{padding:12px 14px;border-radius:8px;margin-bottom:18px;border:1px solid}
  .msg.ok{background:#ecfdf3;border-color:#abefc6}.msg.error{background:#fef3f2;border-color:#fecdca;color:#b42318}
  table{border-collapse:separate;border-spacing:0;width:100%;overflow:hidden;border-radius:8px}
  th{background:#eef2f6;color:#475467;font-size:13px;text-transform:uppercase;letter-spacing:.04em}
  td,th{border-bottom:1px solid #e4e7ec;text-align:left;padding:13px 16px}
  tr:last-child td{border-bottom:0}.count{font-weight:800}.mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:13px}
  .meta{display:grid;gap:8px;margin-top:14px}.meta div{display:flex;justify-content:space-between;gap:16px;border-bottom:1px solid #eef2f6;padding-bottom:8px}
  .meta span:first-child{color:#667085}.empty{text-align:center;color:#667085;padding:24px}
  @media (max-width:820px){.top{display:block}.grid,.stats{grid-template-columns:1fr 1fr}}
  @media (max-width:560px){.grid,.stats{grid-template-columns:1fr}.wrap{padding:24px 14px}}
  </style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <div>
      <h1>Face Attendance</h1>
      <p class="sub">Employee identity is synchronized as embeddings. Attendance servers do not need enrollment photos.</p>
    </div>
  </div>

  {% if msg %}<div class="msg {{ 'error' if error else 'ok' }}">{{ msg }}</div>{% endif %}

  <div class="card" style="margin-bottom:18px">
    <div style="display:flex;justify-content:space-between;gap:18px;align-items:flex-start;flex-wrap:wrap">
      <div>
        <div class="label">Embedding gallery</div>
        {% if gallery.available %}
          <h2 style="margin:6px 0 0" class="{{ 'warn' if gallery.stale else 'ok' }}">{{ 'Stale' if gallery.stale else 'Ready' }}</h2>
        {% else %}
          <h2 style="margin:6px 0 0" class="bad">Unavailable</h2>
        {% endif %}
      </div>
      {% if sync_enabled %}
      <form method="post" action="{{ url_for('sync') }}">
        <button>Sync Embeddings</button>
      </form>
      {% endif %}
    </div>
    <div class="stats">
      <div class="stat"><span class="label">Employees</span><strong>{{ gallery.employee_count or 0 }}</strong></div>
      <div class="stat"><span class="label">Embeddings</span><strong>{{ gallery.embedding_count or 0 }}</strong></div>
      <div class="stat"><span class="label">Dimension</span><strong>{{ gallery.dimension or '-' }}</strong></div>
      <div class="stat"><span class="label">Model</span><strong style="font-size:17px">{{ gallery.model or '-' }}</strong></div>
    </div>
    <div class="meta">
      <div><span>Branch</span><strong>{{ gallery.branch or config.branch_name or '-' }}</strong></div>
      <div><span>Gallery version</span><strong class="mono">{{ gallery.gallery_version or '-' }}</strong></div>
      <div><span>Last local update</span><strong>{{ gallery.updated_at or '-' }}</strong></div>
      <div><span>Last successful sync</span><strong>{{ sync_status.last_success_at or '-' }}</strong></div>
      {% if sync_status.error %}<div><span>Last sync error</span><strong class="bad">{{ sync_status.error }}</strong></div>{% endif %}
    </div>
  </div>

  {% if local_enrollment_enabled %}
  <div class="grid">
    <form class="card" method="post" action="{{ url_for('upload') }}" enctype="multipart/form-data">
      <div class="row">
        <div>
          <label>Employee ID</label>
          <input name="employee" placeholder="HR-EMP-00001" required>
        </div>
        <div>
          <label>Enrollment images</label>
          <input type="file" name="photos" multiple accept="image/*" required>
        </div>
        <button>Upload Images</button>
      </div>
    </form>
    <form class="card" method="post" action="{{ url_for('build') }}">
      <label>Central enrollment gallery</label>
      <p class="sub">Build normalized embeddings after adding or replacing enrollment images. Only enable this on the trusted enrollment server.</p>
      <button class="secondary full">Rebuild Embeddings</button>
    </form>
  </div>
  {% endif %}

  <div class="card">
    <table>
      <tr><th>Employee</th><th>Name</th><th>Embeddings</th></tr>
      {% for row in employees %}
        <tr><td class="mono">{{ row.employee }}</td><td>{{ row.employee_name or '-' }}</td><td class="count">{{ row.count }}</td></tr>
      {% else %}
        <tr><td class="empty" colspan="3">No valid employee embeddings are loaded.</td></tr>
      {% endfor %}
    </table>
  </div>
</div>
</body>
</html>
"""


def load_config():
    try:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def employee_rows(cfg):
    try:
        _, _, payload = load_gallery(
            GALLERY,
            expected_model=cfg.get("model"),
            expected_model_version=cfg.get("model_version"),
            expected_branch=cfg.get("branch_name"),
            require_model_match=bool(cfg.get("require_model_match", True)),
            require_model_version_match=bool(
                cfg.get("require_model_version_match", False)
            ),
            allow_empty=bool(cfg.get("allow_empty_embedding_gallery", False)),
        )
    except GalleryError:
        return []
    return [
        {
            "employee": item["employee"],
            "employee_name": item.get("employee_name", ""),
            "count": len(item.get("embeddings", [])),
        }
        for item in payload["employees"]
    ]


def render_index(msg=None, error=False):
    cfg = load_config()
    status = gallery_status(
        GALLERY,
        max_age_seconds=cfg.get("embedding_max_age_seconds", 86400),
    )
    return render_template_string(
        PAGE,
        config=cfg,
        gallery=status,
        sync_status=read_sync_status(SYNC_STATUS),
        employees=employee_rows(cfg),
        sync_enabled=bool(cfg.get("embedding_sync_enabled", True))
        and bool(cfg.get("central_url")),
        local_enrollment_enabled=bool(
            cfg.get("local_enrollment_enabled", False)
        ),
        msg=msg,
        error=error,
    )


@app.get("/")
def index():
    return render_index(
        msg=request.args.get("msg"),
        error=request.args.get("error") == "1",
    )


@app.post("/sync")
def sync():
    cfg = load_config()
    if not cfg.get("central_url"):
        return redirect(
            url_for("index", msg="central_url is not configured", error="1")
        )
    try:
        result = sync_gallery(cfg, GALLERY, SYNC_STATUS)
        action = "updated" if result["changed"] else "already current"
        message = (
            f"Embedding gallery {action}: {result['employee_count']} employee(s), "
            f"{result['embedding_count']} embedding(s)"
        )
        return redirect(url_for("index", msg=message))
    except GalleryError as exc:
        return redirect(url_for("index", msg=str(exc), error="1"))


@app.post("/upload")
def upload():
    cfg = load_config()
    if not bool(cfg.get("local_enrollment_enabled", False)):
        abort(403)
    raw_employee = request.form.get("employee", "")
    employee = secure_filename(raw_employee).replace("_", "-")
    if not employee:
        return redirect(url_for("index", msg="Employee ID is required", error="1"))

    folder = FACES / employee
    folder.mkdir(parents=True, exist_ok=True)
    saved = 0
    start = len(
        [path for path in folder.iterdir() if path.suffix.lower() in ALLOWED]
    )
    for file in request.files.getlist("photos"):
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in ALLOWED:
            continue
        saved += 1
        file.save(folder / f"{start + saved:03d}{suffix}")
    return redirect(
        url_for("index", msg=f"Uploaded {saved} image(s) for {employee}")
    )


@app.post("/build")
def build():
    cfg = load_config()
    if not bool(cfg.get("local_enrollment_enabled", False)):
        abort(403)
    result = subprocess.run(
        [sys.executable, "face_attendance.py", "build"],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    if result.returncode == 0:
        message = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "Embeddings rebuilt"
        return redirect(url_for("index", msg=message))
    message = (result.stderr or result.stdout or "Build failed").strip()[-500:]
    return redirect(url_for("index", msg=message, error="1"))


def _authorized_export(cfg):
    expected = str(cfg.get("embedding_export_token") or "").strip()
    if not expected or expected.upper() in {"CHANGE_ME", "REPLACE_ME", "CHANGEME"}:
        return False
    supplied = request.headers.get("Authorization", "")
    return secrets.compare_digest(supplied, f"Bearer {expected}")


@app.get("/api/faces/embeddings")
def export_embeddings():
    cfg = load_config()
    if not bool(cfg.get("embedding_export_enabled", False)):
        abort(404)
    if not _authorized_export(cfg):
        abort(401)

    try:
        _, _, payload = load_gallery(
            GALLERY,
            expected_model=cfg.get("model"),
            expected_model_version=cfg.get("model_version"),
            expected_branch=cfg.get("branch_name"),
            require_model_match=True,
            require_model_version_match=bool(
                cfg.get("require_model_version_match", False)
            ),
            allow_empty=False,
        )
    except GalleryError as exc:
        return jsonify({"error": str(exc)}), 503

    requested_branch = str(request.args.get("branch") or "").strip()
    gallery_branch = str(payload.get("branch") or "").strip()
    if requested_branch and requested_branch != gallery_branch:
        abort(404)

    response = make_response(jsonify(payload))
    response.headers["Cache-Control"] = "no-store"
    response.headers["ETag"] = f'"{payload["checksum"]}"'
    return response


if __name__ == "__main__":
    cfg = load_config()
    app.run(host="0.0.0.0", port=int(cfg.get("web_port", 8088)))
