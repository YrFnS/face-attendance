# Face Attendance

Standalone local face-recognition bridge for HOLOWITS camera FTP captures and ERPNext `Employee Checkin`.

It does not install anything inside Frappe. It runs beside a Frappe bench, creates `Employee Checkin` records through `bench`, and attaches the camera image to the checkin.

## What Runs

- `ftp_receiver.py` receives camera FTP uploads.
- `face_attendance.py watch-folder` watches uploaded images, recognizes employees, creates checkins, and attaches the image.
- `web_admin.py` lets a user upload employee face images and rebuild embeddings.

The model is local InsightFace `antelopev2`; no cloud recognition is used.

## Frappe Requirements

- Frappe/ERPNext bench exists on the same Linux server.
- Employees already exist in ERPNext.
- Folder names in `faces/` must match Employee IDs, for example `HR-EMP-00001`.
- The Linux user running this app can run:

```bash
cd /home/test/frappe-bench
bench --site test console
```

No custom Frappe app is required.

## Linux VPS Install

```bash
git clone https://github.com/E2NEXT/face-attendance.git
cd face-attendance
bash install_linux.sh
```

The installer copies the app to `/opt/face-attendance`, creates a venv, installs requirements, and enables three services:

```bash
face-attendance-ftp
face-attendance-watch
face-attendance-web
```

Edit config:

```bash
sudo nano /opt/face-attendance/config.json
sudo systemctl restart face-attendance-*
```

Minimum Linux config changes:

```json
{
  "bench_dir": "/home/test/frappe-bench",
  "site": "test",
  "camera_uploads_dir": "/opt/face-attendance/camera_uploads",
  "ftp_username": "camera",
  "ftp_password": "CHANGE_ME",
  "ftp_port": 2121
}
```

## Camera FTP Settings

In the camera web UI, configure FTP upload:

- FTP attribute: `Target/Person`
- Protocol: `FTP`
- Server IP: VPS IP or local server IP
- Port: `2121`
- User name: same as `ftp_username`
- Password: same as `ftp_password`
- Directory: root directory

If the camera is inside a local LAN and the app is on a public VPS, the camera must be able to reach the VPS FTP port. Use VPN or port forwarding; otherwise run this app on a local mini-PC instead.

## Web UI

Open:

```text
http://SERVER-IP:8088
```

Use it to:

1. Enter Employee ID, for example `HR-EMP-00001`.
2. Upload one or more face images.
3. Click `Rebuild Embeddings`.

Images are stored under:

```text
/opt/face-attendance/faces/HR-EMP-00001/
```

Use clear face images from the same camera angle and lighting when possible. More good images improves matching; bad/blurry images hurt it.

## Service Commands

```bash
sudo systemctl status face-attendance-ftp
sudo systemctl status face-attendance-watch
sudo systemctl status face-attendance-web

sudo systemctl restart face-attendance-ftp
sudo systemctl restart face-attendance-watch
sudo systemctl restart face-attendance-web
```

Logs:

```bash
journalctl -u face-attendance-watch -f
journalctl -u face-attendance-ftp -f
journalctl -u face-attendance-web -f
tail -f /opt/face-attendance/logs/watch.log
```

## Manual Run

```bash
cd /opt/face-attendance
. .venv/bin/activate

python face_attendance.py build
python ftp_receiver.py
python face_attendance.py watch-folder
python web_admin.py
```

## Local Files

Ignored by git:

- `config.json`
- `faces/`
- `camera_uploads/`
- `logs/`
- `embeddings.pkl`
- `.venv/`
- image/video files

Do not commit employee photos, camera captures, logs, or real passwords.
