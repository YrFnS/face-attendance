# Face Attendance

Local face-attendance bridge for HOLOWITS FTP target captures and ERPNext Employee Checkin.

## Local Setup

1. Copy `config.example.json` to `config.json`.
2. Put employee enrollment images under `faces/<employee-id>/`.
3. Run `python face_attendance.py build`.
4. Run `python ftp_receiver.py`.
5. Run `python face_attendance.py watch-folder`.

Generated images, logs, embeddings, and local secrets are ignored by git.
