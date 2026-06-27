import subprocess
import sys
from pathlib import Path

from flask import Flask, redirect, render_template_string, request, url_for
from werkzeug.utils import secure_filename


ROOT = Path(__file__).resolve().parent
FACES = ROOT / "faces"
ALLOWED = {".jpg", ".jpeg", ".png", ".webp"}
app = Flask(__name__)

PAGE = """
<!doctype html>
<title>Face Attendance</title>
<style>
body{font-family:system-ui,Arial;margin:32px;max-width:900px}
input,button{font:inherit;padding:8px;margin:4px 0}
button{cursor:pointer}
table{border-collapse:collapse;width:100%;margin-top:24px}
td,th{border-bottom:1px solid #ddd;text-align:left;padding:8px}
.msg{background:#eef7ee;padding:10px;margin:12px 0}
</style>
<h1>Employee Face Images</h1>
{% if msg %}<div class=msg>{{ msg }}</div>{% endif %}
<form method=post action="{{ url_for('upload') }}" enctype=multipart/form-data>
  <p><input name=employee placeholder="Employee ID, example HR-EMP-00001" required></p>
  <p><input type=file name=photos multiple accept="image/*" required></p>
  <button>Upload</button>
</form>
<form method=post action="{{ url_for('build') }}">
  <button>Rebuild Embeddings</button>
</form>
<table>
  <tr><th>Employee</th><th>Images</th></tr>
  {% for employee, count in employees %}
    <tr><td>{{ employee }}</td><td>{{ count }}</td></tr>
  {% endfor %}
</table>
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
    app.run(host="0.0.0.0", port=8088)
