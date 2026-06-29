import subprocess
import sys
import json
from pathlib import Path

from flask import Flask, redirect, render_template_string, request, url_for
from werkzeug.utils import secure_filename


ROOT = Path(__file__).resolve().parent
FACES = ROOT / "faces"
CONFIG = ROOT / "config.json"
ALLOWED = {".jpg", ".jpeg", ".png", ".webp"}
app = Flask(__name__)

PAGE = """
<!doctype html>
<title>Face Attendance</title>
<style>
*{box-sizing:border-box}
body{font-family:Inter,system-ui,Arial,sans-serif;margin:0;background:#f4f6f8;color:#17202a}
.wrap{max-width:1040px;margin:0 auto;padding:36px 24px}
.top{display:flex;justify-content:space-between;gap:20px;align-items:flex-end;margin-bottom:24px}
h1{margin:0;font-size:30px}
.sub{color:#667085;margin:6px 0 0}
.grid{display:grid;grid-template-columns:1.4fr .8fr;gap:18px;margin-bottom:22px}
.card{background:white;border:1px solid #e3e7ee;border-radius:8px;padding:20px;box-shadow:0 1px 2px #10182812}
label{display:block;font-weight:650;margin-bottom:8px}
input{font:inherit;width:100%;padding:11px 12px;border:1px solid #cfd6df;border-radius:6px;background:white}
input[type=file]{padding:9px}
button{font:inherit;font-weight:700;cursor:pointer;border:0;border-radius:6px;padding:11px 16px;background:#0f766e;color:white}
.secondary{background:#1f2937;width:100%;margin-top:28px}
.row{display:grid;gap:14px}
.msg{background:#ecfdf3;border:1px solid #abefc6;color:#067647;padding:12px 14px;border-radius:8px;margin-bottom:18px}
table{border-collapse:separate;border-spacing:0;width:100%;overflow:hidden;border-radius:8px}
th{background:#eef2f6;color:#475467;font-size:13px;text-transform:uppercase;letter-spacing:.04em}
td,th{border-bottom:1px solid #e4e7ec;text-align:left;padding:13px 16px}
tr:last-child td{border-bottom:0}
.count{font-weight:800}
@media (max-width:760px){.top,.grid{display:block}.card{margin-bottom:14px}}
</style>
<div class=wrap>
  <div class=top>
    <div>
      <h1>Employee Face Images</h1>
      <p class=sub>Upload enrollment photos and rebuild recognition embeddings.</p>
    </div>
  </div>
  {% if msg %}<div class=msg>{{ msg }}</div>{% endif %}
  <div class=grid>
    <form class=card method=post action="{{ url_for('upload') }}" enctype=multipart/form-data>
      <div class=row>
        <div>
          <label>Employee ID</label>
          <input name=employee placeholder="HR-EMP-00001" required>
        </div>
        <div>
          <label>Face images</label>
          <input type=file name=photos multiple accept="image/*" required>
        </div>
        <button>Upload Images</button>
      </div>
    </form>
    <form class=card method=post action="{{ url_for('build') }}">
      <label>Recognition data</label>
      <p class=sub>Run after adding or replacing employee images.</p>
      <button class=secondary>Rebuild Embeddings</button>
    </form>
  </div>
  <div class=card>
    <table>
      <tr><th>Employee</th><th>Images</th></tr>
      {% for employee, count in employees %}
        <tr><td>{{ employee }}</td><td class=count>{{ count }}</td></tr>
      {% endfor %}
    </table>
  </div>
</div>
"""


def employees():
    FACES.mkdir(exist_ok=True)
    rows = []
    for folder in sorted(p for p in FACES.iterdir() if p.is_dir()):
        count = sum(1 for p in folder.iterdir() if p.suffix.lower() in ALLOWED)
        rows.append((folder.name, count))
    return rows


@app.get("/")
def index():
    return render_template_string(PAGE, employees=employees(), msg=request.args.get("msg"))


@app.post("/upload")
def upload():
    employee = secure_filename(request.form["employee"]).replace("_", "-")
    folder = FACES / employee
    folder.mkdir(parents=True, exist_ok=True)
    saved = 0
    start = len([p for p in folder.iterdir() if p.suffix.lower() in ALLOWED])
    for file in request.files.getlist("photos"):
        suffix = Path(file.filename).suffix.lower()
        if suffix not in ALLOWED:
            continue
        saved += 1
        file.save(folder / f"{start + saved:03d}{suffix}")
    return redirect(url_for("index", msg=f"Uploaded {saved} image(s) for {employee}"))


@app.post("/build")
def build():
    result = subprocess.run([sys.executable, "face_attendance.py", "build"], cwd=ROOT, text=True, capture_output=True)
    msg = "Embeddings rebuilt" if result.returncode == 0 else result.stderr[-300:] or "Build failed"
    return redirect(url_for("index", msg=msg))


if __name__ == "__main__":
    port = json.loads(CONFIG.read_text()).get("web_port", 8088) if CONFIG.exists() else 8088
    app.run(host="0.0.0.0", port=int(port))
