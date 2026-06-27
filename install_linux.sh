#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/face-attendance}"

sudo mkdir -p "$APP_DIR"
sudo rsync -a --exclude .git --exclude .venv --exclude camera_uploads --exclude logs --exclude faces ./ "$APP_DIR/"
cd "$APP_DIR"

python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

[ -f config.json ] || cp config.example.json config.json
mkdir -p faces camera_uploads logs

sudo cp deploy/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now face-attendance-ftp face-attendance-watch face-attendance-web

echo "Edit $APP_DIR/config.json, then run: sudo systemctl restart face-attendance-*"
echo "Web UI: http://SERVER-IP:8088"
