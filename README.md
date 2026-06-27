# Face Attendance

Local face-attendance bridge for HOLOWITS FTP target captures and ERPNext Employee Checkin.

## Local Setup

1. Copy `config.example.json` to `config.json`.
2. Put employee enrollment images under `faces/<employee-id>/`.
3. Run `python face_attendance.py build`.
4. Run `python ftp_receiver.py`.
5. Run `python face_attendance.py watch-folder`.
6. Open the upload UI with `python web_admin.py`, then visit `http://SERVER-IP:8088`.

Generated images, logs, embeddings, and local secrets are ignored by git.

## Linux service install

```bash
git clone https://github.com/E2NEXT/face-attendance.git
cd face-attendance
bash install_linux.sh
```

Edit `/opt/face-attendance/config.json`, upload employee images from the web UI, then click **Rebuild Embeddings**.
