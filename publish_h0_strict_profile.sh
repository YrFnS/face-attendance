#!/usr/bin/env bash
set -euo pipefail

python - <<'PY'
from pathlib import Path

path = Path("apply_h0_strict_profile.py")
quote = chr(39) * 3
lines = []
for line in path.read_text(encoding="utf-8").splitlines():
    if line.startswith('write("') and ", '" in line and line.endswith("')"):
        prefix, payload = line.split(", '", 1)
        line = prefix + ", " + quote + payload[:-2] + quote + ")"
    lines.append(line)
path.write_text("\n".join(lines) + "\n", encoding="utf-8")

target = Path("secure_sync.py")
source = target.read_text(encoding="utf-8")
source = source.replace(
    "def _gallery_options(cfg):\n",
    r"def _gallery_options(cfg):\n",
    1,
)
source = source.replace(
    "\n\ndef _local_metadata",
    r"\n\ndef _local_metadata",
    1,
)
target.write_text(source, encoding="utf-8")
PY

python -m py_compile apply_h0_strict_profile.py
python apply_h0_strict_profile.py

python - <<'PY'
from pathlib import Path
path = Path("secure_sync.py")
source = path.read_text(encoding="utf-8")
source = source.replace(r"\n\ndef _local_metadata", "\n\ndef _local_metadata", 1)
path.write_text(source, encoding="utf-8")
PY

python -m pip install --upgrade pip
python -m pip install \
  'flask>=3.0.3,<4' \
  'gunicorn>=26.0.0,<27' \
  'numpy>=1.26.4' \
  'opencv-python-headless>=4.10.0,<5' \
  'pyftpdlib>=2.0.1,<3' \
  'requests>=2.32.3,<3'

python -m py_compile *.py
python -m unittest -v \
  test_embedding_gallery.py \
  test_match_employee.py \
  test_secure_sync.py \
  test_legacy_gallery_converter.py \
  test_runtime_policy.py \
  test_model_runtime.py \
  test_runtime_state.py \
  test_web_security.py \
  test_web_admin.py \
  test_model_manifest.py \
  test_pad.py \
  test_production_readiness.py \
  test_watch_service.py \
  test_canonical_watcher.py \
  test_install_linux.py
python -m json.tool config.example.json >/dev/null
bash -n install_linux.sh
bash -n deploy/firewall/ufw-rules.example.sh
git diff --check

git config user.name 'github-actions[bot]'
git config user.email '41898282+github-actions[bot]@users.noreply.github.com'
git add -A
git reset -- \
  .github/workflows/tests.yml \
  .github/workflows/apply-h0-strict-production-profile.yml \
  apply_h0_strict_profile.py \
  publish_h0_strict_profile.sh
git diff --cached --check
git commit -m 'enforce strict production runtime policy'
git push origin HEAD:agent/h0-strict-production-profile-generated
