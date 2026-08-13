#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/face-attendance}"
SERVICE_USER="${FACE_ATTENDANCE_USER:-${SUDO_USER:-$(id -un)}}"

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  echo "Unknown FACE_ATTENDANCE_USER: $SERVICE_USER" >&2
  exit 1
fi
SERVICE_GROUP="${FACE_ATTENDANCE_GROUP:-$(id -gn "$SERVICE_USER")}"

sudo mkdir -p "$APP_DIR"
sudo chown "$SERVICE_USER:$SERVICE_GROUP" "$APP_DIR"
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
  --exclude model_manifest.json \
  --exclude runtime_state.sqlite3 \
  --exclude runtime_state.sqlite3-wal \
  --exclude runtime_state.sqlite3-shm \
  ./ "$APP_DIR/"
sudo chown -R "$SERVICE_USER:$SERVICE_GROUP" "$APP_DIR"

sudo -u "$SERVICE_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/python" -m pip install --upgrade pip
sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/python" -m pip install -r "$APP_DIR/requirements.txt"

if [ ! -f "$APP_DIR/config.json" ]; then
  sudo -u "$SERVICE_USER" cp "$APP_DIR/config.example.json" "$APP_DIR/config.json"
fi
sudo chmod 600 "$APP_DIR/config.json"
sudo -u "$SERVICE_USER" mkdir -p "$APP_DIR/faces" "$APP_DIR/camera_uploads" "$APP_DIR/logs"
sudo chmod 700 "$APP_DIR/faces" "$APP_DIR/camera_uploads" "$APP_DIR/logs"

sudo cp "$APP_DIR"/deploy/systemd/*.service /etc/systemd/system/
sudo cp "$APP_DIR"/deploy/systemd/*.timer /etc/systemd/system/

write_service_override() {
  local unit="$1"
  local command="$2"
  local dropin="/etc/systemd/system/${unit}.d"
  sudo mkdir -p "$dropin"
  {
    printf '[Service]\n'
    printf 'User=%s\n' "$SERVICE_USER"
    printf 'Group=%s\n' "$SERVICE_GROUP"
    printf 'WorkingDirectory="%s"\n' "$APP_DIR"
    printf 'ExecStart=\n'
    printf 'ExecStart=%s\n' "$command"
  } | sudo tee "$dropin/00-face-attendance-install.conf" >/dev/null
}

write_service_override \
  face-attendance-ftp.service \
  "\"$APP_DIR/.venv/bin/python\" -u \"$APP_DIR/ftp_receiver.py\""
write_service_override \
  face-attendance-watch.service \
  "\"$APP_DIR/.venv/bin/python\" -u \"$APP_DIR/watch_service.py\""
write_service_override \
  face-attendance-web.service \
  "\"$APP_DIR/.venv/bin/gunicorn\" --config \"$APP_DIR/gunicorn.conf.py\" web_admin:app"
write_service_override \
  face-attendance-sync.service \
  "\"$APP_DIR/.venv/bin/python\" -u \"$APP_DIR/sync_embeddings.py\" --scheduled"

sudo systemctl daemon-reload
sudo systemctl enable face-attendance-ftp face-attendance-watch face-attendance-web
sudo systemctl enable --now face-attendance-sync.timer

# FTP collection and the locked admin UI are safe to start before enrollment is ready.
sudo systemctl restart face-attendance-ftp face-attendance-web

# Do not start live check-in creation on a fresh installation without a gallery.
if [ -s "$APP_DIR/embedding_gallery.json" ]; then
  sudo systemctl restart face-attendance-watch
else
  sudo systemctl stop face-attendance-watch 2>/dev/null || true
  echo "Watcher left stopped: configure and sync a valid embedding_gallery.json first; a legacy pickle does not enable live processing."
fi

if [ "$SERVICE_USER" = "root" ]; then
  echo "WARNING: services are configured to run as root. Re-run with FACE_ATTENDANCE_USER set to the bench/service owner."
fi

echo
echo "Installed for service account: $SERVICE_USER:$SERVICE_GROUP"
echo "Next steps:"
echo "1. Edit $APP_DIR/config.json and replace all placeholder secrets."
echo "2. Configure the admin login: sudo -u $SERVICE_USER $APP_DIR/.venv/bin/python $APP_DIR/manage_admin.py set-password"
echo "3. Deploy HTTPS using deploy/caddy or deploy/nginx and restrict camera ports using deploy/firewall."
echo "4. Verify model licensing, then create the pinned manifest: sudo -u $SERVICE_USER $APP_DIR/.venv/bin/python $APP_DIR/model_manifest.py create"
echo "5. Configure and validate the PAD/liveness service."
echo "6. Sync embeddings: sudo -u $SERVICE_USER $APP_DIR/.venv/bin/python $APP_DIR/sync_embeddings.py"
echo "7. Run readiness checks: sudo -u $SERVICE_USER $APP_DIR/.venv/bin/python $APP_DIR/production_readiness.py --strict"
echo "8. Validate old samples safely: sudo -u $SERVICE_USER $APP_DIR/.venv/bin/python $APP_DIR/watch_service.py --once --dry-run --allow-stale"
echo "9. After controlled validation, set production_mode=true and start face-attendance-watch."
