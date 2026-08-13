import json
import secrets
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np
from flask import (
    Flask,
    abort,
    jsonify,
    make_response,
    redirect,
    render_template_string,
    request,
    session,
    url_for,
)
from werkzeug.utils import secure_filename

from embedding_gallery import GalleryError, gallery_status, load_gallery, read_sync_status
from production_readiness import check_production_readiness
from runtime_policy import effective_gallery_options
from runtime_state import RuntimeState, resolve_runtime_path
from secure_sync import sync_gallery
from web_security import (
    add_security_headers,
    admin_user,
    auth_configured,
    configure_app,
    csrf_protected,
    csrf_token,
    login_required,
    remote_address,
    safe_next_url,
    validate_csrf,
    verify_password,
)


ROOT = Path(__file__).resolve().parent
FACES = ROOT / "faces"
CONFIG = ROOT / "config.json"
GALLERY = ROOT / "embedding_gallery.json"
SYNC_STATUS = ROOT / "embedding_sync_status.json"
ALLOWED = {".jpg", ".jpeg", ".png", ".webp"}
PLACEHOLDERS = {"", "CHANGE_ME", "REPLACE_ME", "CHANGEME"}
app = Flask(__name__)

STYLE = """
body{font-family:system-ui;margin:2rem auto;max-width:1000px;padding:0 1rem;background:#f5f7f9;color:#17202a}
.card{background:white;padding:1rem;border:1px solid #ddd;border-radius:8px;margin:1rem 0}
input,button{padding:.65rem;margin:.25rem}button{background:#0f766e;color:white;border:0;border-radius:5px}
table{width:100%;border-collapse:collapse}td,th{padding:.5rem;border-bottom:1px solid #ddd;text-align:left}
.bad{color:#b42318}.ok{color:#067647}.warn{background:#fff4ce;padding:.7rem}.mono{font-family:monospace}
ul.issues{padding-left:1.25rem}
"""

LOGIN = """<!doctype html><style>{{style}}</style><div class=card>
<h1>Face Attendance</h1>{% if message %}<p class=bad>{{message}}</p>{% endif %}
<form method=post><input type=hidden name=csrf_token value="{{csrf}}"><input type=hidden name=next value="{{next_url}}">
<p><input name=username placeholder=Username required></p><p><input type=password name=password placeholder=Password required></p>
<button>Sign in</button></form></div>"""

SETUP = """<!doctype html><style>{{style}}</style><div class=card><h1>Admin setup required</h1>
<p class=bad>The web UI is locked.</p><pre>cd /opt/face-attendance
source .venv/bin/activate
python manage_admin.py set-password</pre></div>"""

HOME = """<!doctype html><style>{{style}}</style><h1>Face Attendance</h1>
<form method=post action="{{url_for('logout')}}"><input type=hidden name=csrf_token value="{{csrf}}"><button>Sign out {{user}}</button></form>
{% if msg %}<p class="{{'bad' if error else 'ok'}}">{{msg}}</p>{% endif %}
{% if not cfg.model_license_acknowledged %}<p class=warn><b>Model license not acknowledged.</b> Verify production/commercial terms before live use.</p>{% endif %}
<div class=card><h2>Production readiness: <span class="{{'ok' if readiness.ready else 'bad'}}">{{'ready' if readiness.ready else 'blocked'}}</span></h2>
<p>production_mode={{'on' if cfg.production_mode else 'off'}}</p>
{% if readiness.issues %}<ul class=issues>{% for issue in readiness.issues %}<li class="{{'bad' if issue.severity == 'blocker' else ''}}"><b>{{issue.code}}</b>: {{issue.message}}</li>{% endfor %}</ul>{% endif %}
</div>
<div class=card><h2>Embedding gallery: {{'stale' if gallery.stale else 'ready' if gallery.available else 'unavailable'}}</h2>
<p>Employees {{gallery.employee_count or 0}} · Embeddings {{gallery.embedding_count or 0}} · Model {{gallery.model or '-'}} · Version <span class=mono>{{gallery.gallery_version or '-'}}</span></p>
<p>Last sync {{sync.last_success_at or '-'}}{% if sync.error %} · <span class=bad>{{sync.error}}</span>{% endif %}</p>
{% if sync_enabled %}<form method=post action="{{url_for('sync')}}"><input type=hidden name=csrf_token value="{{csrf}}"><button>Sync embeddings</button></form>{% endif %}</div>
{% if enroll %}<div class=card><form method=post action="{{url_for('upload')}}" enctype=multipart/form-data><input type=hidden name=csrf_token value="{{csrf}}"><input name=employee placeholder="Employee ID" required><input type=file name=photos multiple required><button>Upload</button></form>
<form method=post action="{{url_for('build')}}"><input type=hidden name=csrf_token value="{{csrf}}"><button>Rebuild embeddings</button></form></div>{% endif %}
<div class=card><table><tr><th>Employee</th><th>Name</th><th>Templates</th></tr>{% for x in employees %}<tr><td class=mono>{{x.employee}}</td><td>{{x.employee_name or '-'}}</td><td>{{x.count}}</td></tr>{% else %}<tr><td colspan=3>No embeddings loaded</td></tr>{% endfor %}</table></div>"""


def load_config():
    try:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def apply_security_config():
    return configure_app(app, load_config())


apply_security_config()


def state_store(cfg=None):
    cfg = cfg or load_config()
    return RuntimeState(
        resolve_runtime_path(ROOT, cfg.get("runtime_state_db"), "runtime_state.sqlite3")
    )


def audit(action, detail=None, actor=None):
    try:
        state_store().audit(
            actor=actor or admin_user() or "anonymous",
            action=action,
            remote_addr=remote_address(),
            detail=detail or {},
        )
    except Exception:
        app.logger.exception("audit write failed")


def payload(cfg):
    return load_gallery(
        GALLERY,
        **effective_gallery_options(cfg),
    )[2]


def employees(cfg):
    try:
        rows = payload(cfg)["employees"]
    except GalleryError:
        return []
    return [
        {
            "employee": item["employee"],
            "employee_name": item.get("employee_name", ""),
            "count": len(item.get("embeddings", [])),
        }
        for item in rows
    ]


def readiness(cfg):
    return check_production_readiness(
        cfg,
        ROOT,
        verify_model_files=False,
        gallery_path=GALLERY,
    )


@app.after_request
def headers(response):
    return add_security_headers(response, load_config())


@app.get("/healthz")
def health():
    return jsonify(ok=True, service="face-attendance-web")


@app.get("/readyz")
def ready():
    cfg = load_config()
    report = readiness(cfg)
    gallery = report.gallery
    reasons = []
    if not auth_configured(cfg):
        reasons.append("admin authentication is not configured")
    if not gallery.get("available") or not gallery.get(
        "policy_valid", False
    ):
        reasons.append(
            gallery.get("error") or "gallery unavailable"
        )
    if bool(cfg.get("production_mode", False)):
        reasons.extend(issue.message for issue in report.blockers)
    reasons = list(dict.fromkeys(reasons))
    return (
        jsonify(
            ok=not reasons,
            reasons=reasons,
            gallery=gallery,
            production=report.to_dict(),
        ),
        200 if not reasons else 503,
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    cfg = load_config()
    if not auth_configured(cfg):
        return render_template_string(SETUP, style=STYLE), 503
    if request.method == "GET":
        if session.get("admin_authenticated"):
            return redirect(url_for("index"))
        return render_template_string(
            LOGIN,
            style=STYLE,
            csrf=csrf_token(),
            next_url=safe_next_url(request.args.get("next")),
            message="",
        )
    validate_csrf()
    user = str(request.form.get("username") or "").strip()
    password = str(request.form.get("password") or "")
    next_url = safe_next_url(request.form.get("next"))
    limiter_key = f"login:{remote_address()}"
    store = state_store(cfg)
    allowed, retry = store.login_allowed(limiter_key)
    if not allowed:
        audit("login_locked", {"username": user}, "anonymous")
        return (
            render_template_string(
                LOGIN,
                style=STYLE,
                csrf=csrf_token(),
                next_url=next_url,
                message=f"Try again in {retry} seconds.",
            ),
            429,
        )
    expected = str(cfg.get("web_admin_username") or "")
    valid = secrets.compare_digest(user, expected) and verify_password(
        password, cfg.get("web_admin_password_hash")
    )
    if not valid:
        store.record_login_failure(
            limiter_key,
            max_attempts=cfg.get("web_login_attempts", 5),
            window_seconds=cfg.get("web_login_window_seconds", 300),
            lockout_seconds=cfg.get("web_lockout_seconds", 900),
        )
        audit("login_failed", {"username": user}, "anonymous")
        return (
            render_template_string(
                LOGIN,
                style=STYLE,
                csrf=csrf_token(),
                next_url=next_url,
                message="Invalid username or password.",
            ),
            401,
        )
    store.clear_login_failures(limiter_key)
    session.clear()
    session.update(admin_authenticated=True, admin_user=expected)
    session.permanent = True
    csrf_token()
    audit("login_succeeded", actor=expected)
    return redirect(next_url)


@app.post("/logout")
@login_required
@csrf_protected
def logout():
    audit("logout", actor=admin_user())
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
@login_required
def index():
    cfg = load_config()
    report = readiness(cfg)
    return render_template_string(
        HOME,
        style=STYLE,
        cfg=cfg,
        readiness=report,
        gallery=report.gallery,
        sync=read_sync_status(SYNC_STATUS),
        employees=employees(cfg),
        sync_enabled=bool(
            cfg.get("embedding_sync_enabled", True)
            and cfg.get("central_url")
        ),
        enroll=bool(cfg.get("local_enrollment_enabled", False)),
        user=admin_user(),
        csrf=csrf_token(),
        msg=request.args.get("msg"),
        error=request.args.get("error") == "1",
    )


@app.post("/sync")
@login_required
@csrf_protected
def sync():
    cfg = load_config()
    if not cfg.get("central_url"):
        return redirect(url_for("index", msg="central_url is not configured", error="1"))
    try:
        result = sync_gallery(cfg, GALLERY, SYNC_STATUS)
        action = (
            "not modified"
            if result.get("not_modified")
            else "updated"
            if result["changed"]
            else "already current"
        )
        audit(
            "embedding_sync",
            {"action": action, "version": result.get("gallery_version")},
        )
        return redirect(
            url_for(
                "index",
                msg=(
                    f"Gallery {action}: {result['employee_count']} employees, "
                    f"{result['embedding_count']} embeddings"
                ),
            )
        )
    except GalleryError as exc:
        audit("embedding_sync_failed", {"error": str(exc)})
        return redirect(url_for("index", msg=str(exc), error="1"))


@app.post("/upload")
@login_required
@csrf_protected
def upload():
    cfg = load_config()
    if not cfg.get("local_enrollment_enabled"):
        abort(403)
    employee = secure_filename(request.form.get("employee", "")).replace("_", "-")
    if not employee:
        return redirect(url_for("index", msg="Employee ID is required", error="1"))
    files = request.files.getlist("photos")
    max_files = max(1, int(cfg.get("max_enrollment_files_per_request", 20)))
    if len(files) > max_files:
        return redirect(url_for("index", msg=f"Maximum {max_files} files", error="1"))
    max_bytes = max(1024, int(cfg.get("max_enrollment_image_bytes", 10485760)))
    max_pixels = max(1, int(cfg.get("max_enrollment_image_pixels", 20000000)))
    folder = FACES / employee
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


@app.post("/build")
@login_required
@csrf_protected
def build():
    cfg = load_config()
    if not cfg.get("local_enrollment_enabled"):
        abort(403)
    try:
        result = subprocess.run(
            [sys.executable, "face_attendance.py", "build"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=max(60, int(cfg.get("embedding_build_timeout_seconds", 1800))),
        )
    except subprocess.TimeoutExpired:
        audit("embedding_build_failed", {"error": "timeout"})
        return redirect(url_for("index", msg="Build timed out", error="1"))
    text = (
        result.stdout
        if result.returncode == 0
        else result.stderr or result.stdout or "Build failed"
    ).strip()
    message = text.splitlines()[-1] if text else "Embeddings rebuilt"
    audit(
        "embedding_build_succeeded" if result.returncode == 0 else "embedding_build_failed",
        {"message": message[-500:]},
    )
    return redirect(
        url_for(
            "index",
            msg=message[-500:],
            error="1" if result.returncode else None,
        )
    )


def export_allowed(cfg):
    expected = str(cfg.get("embedding_export_token") or "").strip()
    return expected.upper() not in PLACEHOLDERS and secrets.compare_digest(
        request.headers.get("Authorization", ""), f"Bearer {expected}"
    )


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
    requested = str(request.args.get("branch") or "").strip()
    actual = str(data.get("branch") or "").strip()
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


if __name__ == "__main__":
    cfg = load_config()
    app.run(
        host=str(cfg.get("web_bind_host", "127.0.0.1")),
        port=int(cfg.get("web_port", 8088)),
    )
