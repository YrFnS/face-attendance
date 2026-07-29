#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/face-attendance}"

sudo mkdir -p "$APP_DIR"
sudo rsync -a \
  --exclude .git \
  --exclude .venv \
  --exclude config.json \
  --exclude camera_uploads \
  --exclude logs \
  --exclude faces \
  --exclude embedding_gallery.json \
  --exclude embedding_sync_status.json \
  --exclude embeddings.pkl \
  ./ "$APP_DIR/"
cd "$APP_DIR"

python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

[ -f config.json ] || cp config.example.json config.json
mkdir -p faces camera_uploads logs

sudo cp deploy/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable face-attendance-ftp face-attendance-watch face-attendance-web

# FTP collection and the admin UI are safe to start before enrollment is ready.
sudo systemctl restart face-attendance-ftp face-attendance-web

# Do not start live checkin creation on a fresh installation without a gallery.
if [ -s embedding_gallery.json ] || [ -s embeddings.pkl ]; then
  sudo systemctl restart face-attendance-watch
else
  sudo systemctl stop face-attendance-watch 2>/dev/null || true
  echo "Watcher left stopped: configure and sync a valid embedding gallery first."
fi

echo "Edit $APP_DIR/config.json"
echo "Sync embeddings: cd $APP_DIR && . .venv/bin/activate && python sync_embeddings.py"
echo "After dry-run validation: sudo systemctl start face-attendance-watch"
echo "Web UI: http://SERVER-IP:8088"
