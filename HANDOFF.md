# Face Attendance Handoff

This app is a local face-attendance bridge between HOLOWITS camera captures and ERPNext/Frappe `Employee Checkin`.

It does not install a Frappe app. It runs beside Frappe and creates checkins through either:

- Frappe HTTP API, when `frappe_url`, `frappe_api_key`, and `frappe_api_secret` are configured.
- local `bench execute`, when running on the same machine as a Frappe bench.

## Goal

Automatically create employee IN/OUT checkins when enrolled employees pass camera-controlled entrances, while rejecting visitors and uncertain matches.

The main risk is not missing a few weak faces. The main risk is false checkins for the wrong employee, because that affects attendance/payroll. The app should prefer rejection over guessing.

## Current Architecture

```text
HOLOWITS camera
  -> FTP Target/Person snapshot
  -> ftp_receiver.py
  -> camera_uploads/in or camera_uploads/out
  -> face_attendance.py watch-folder
  -> local InsightFace model
  -> local embeddings.pkl
  -> ERPNext Employee Checkin
  -> matched face crop attached to the checkin
```

The camera does person/target detection and uploads images. The app does employee recognition and Frappe checkin creation.

We moved away from RTSP frame polling for production testing because the real camera is mounted high and wide. RTSP frames were too small and compressed for reliable face recognition. Camera Target/Person FTP captures are the better source because the camera sends event images when it detects a person.

## Main Files

- `face_attendance.py` - builds embeddings, watches RTSP or folder uploads, recognizes faces, creates checkins.
- `ftp_receiver.py` - local FTP server for camera uploads.
- `web_admin.py` - simple web UI for uploading employee face photos and rebuilding embeddings.
- `import_local_faces.py` - imports existing employee image folders from the old camera dashboard data.
- `import_faces.py` - imports employee photos from the central/dashboard source when configured.
- `config.example.json` - safe example config.
- `test_match_employee.py` - small assert check for the matching logic.

Runtime data is intentionally ignored by git:

- `config.json`
- `faces/`
- `camera_uploads/`
- `logs/`
- `embeddings.pkl`
- image/video files

Do not commit employee photos, camera captures, logs, API tokens, or passwords.

## Model Choice

Current production test model:

```json
"model": "buffalo_l"
```

This uses local InsightFace. No cloud recognition is used.

Why not `antelopev2` right now:

- It was too slow/heavy during the VPS test.
- CPU cores were pinned for a long time while building embeddings.

Why not NVIDIA DeepStream/FaceDetectNet:

- Those require NVIDIA GPU/CUDA.
- A normal low-cost 4-core mini PC without NVIDIA hardware cannot use them properly.

Why not YOLO/YuNet rewrite now:

- YOLO/YuNet/SCRFD are detectors. They find faces.
- They do not identify employees by themselves.
- InsightFace already includes SCRFD-style detection plus ArcFace-style recognition in one pipeline.
- The current bottleneck was unsafe acceptance logic, not only detection speed.

Why not Faiss now:

- Faiss speeds up vector search for very large galleries.
- With around 100 employees, simple NumPy cosine comparison is enough.
- Faiss would not fix false positives by itself.

## Matching Rules

The app uses open-set matching. This means most faces may be unknown visitors, not enrolled employees. The code must not blindly pick the nearest employee.

Current acceptance rules:

- detected face must pass `min_face_width`
- detected face must pass `min_face_height`
- detector confidence must pass `min_detection_score`
- best employee score must pass `threshold`
- best employee must beat the second-best employee by `min_score_margin`
- employee must not already be processed from the same image
- employee must not be inside `cooldown_seconds`

Important config:

```json
{
  "threshold": 0.8,
  "min_score_margin": 0.08,
  "min_face_width": 65,
  "min_face_height": 80,
  "min_detection_score": 0.5,
  "cooldown_seconds": 600
}
```

For production testing, prefer raising accuracy guards before lowering them. If real employees are rejected too often, tune from real logs, for example `threshold: 0.75` and `min_score_margin: 0.06`. Do not go back to very low thresholds like `0.55`; that caused false checkins.

## Attachments

The app now attaches the matched face crop to `Employee Checkin`, not the full camera image.

Reason: full-frame attachments made it hard to audit why a checkin was created. The crop shows the exact face the model matched.

The original camera uploads remain under `camera_uploads/` for local investigation.

## Camera Setup

HOLOWITS web UI settings:

- FTP attribute: `Target/Person`
- Protocol: `FTP`
- Server IP: local server/VPS LAN IP reachable by the camera
- Port: `2121`
- Directory: root directory
- File naming: Track ID + Time
- For IN camera use FTP user mapped to `camera_uploads/in`
- For OUT camera use FTP user mapped to `camera_uploads/out`

`FTP attribute: Target/Person` means the camera uploads target/person event captures, not generic scheduled snapshots. This is the setting we want.

Folder direction mapping is controlled by:

```json
"folder_log_types": {
  "in": "IN",
  "out": "OUT"
}
```

## Web UI

Run:

```bash
python web_admin.py
```

Open:

```text
http://SERVER-IP:8088
```

Use it to:

1. Enter the ERPNext Employee ID, for example `HR-EMP-00001`.
2. Upload face photos for that employee.
3. Rebuild embeddings.

Enrollment images must live under:

```text
faces/HR-EMP-00001/
```

Folder names must be ERPNext employee IDs, not Arabic display names.

## Commands

Create or refresh embeddings:

```bash
python face_attendance.py build
```

Receive camera FTP uploads:

```bash
python ftp_receiver.py
```

Watch FTP uploads and create checkins:

```bash
python face_attendance.py watch-folder
```

Dry-run folder watcher:

```bash
python face_attendance.py watch-folder --dry-run
```

Process existing uploaded images once:

```bash
python face_attendance.py watch-folder --scan-existing --dry-run
```

RTSP mode still exists, but production testing should use FTP Target/Person captures:

```bash
python face_attendance.py watch
```

## Current VPS Test Context

The production test has been using:

- App path: `/home/nvr2/face-attendance`
- Camera upload folders: `/home/nvr2/face-attendance/camera_uploads/in` and `/home/nvr2/face-attendance/camera_uploads/out`
- Frappe site: `https://dr-atyaf.e2next.com`
- Camera FTP server port: `2121`
- Two camera FTP users: one for IN, one for OUT

Known service state during the last safe handoff:

- FTP receiver was allowed to keep running.
- `face_attendance.py watch-folder` was stopped after false-positive checkins.
- Old Frigate/RTSP face workers were stopped for clean testing.

Before restarting live checkin creation, verify running processes:

```bash
pgrep -af "face_attendance.py watch-folder|ftp_receiver.py|frigate|rtsp_face_gate.py|ffmpeg" || true
```

Only `ftp_receiver.py` should be running if you are collecting images without creating checkins.

## What We Already Fixed

- Switched production approach from RTSP polling to camera FTP Target/Person captures.
- Added FTP receiver.
- Added IN/OUT folder mapping.
- Added web UI for employee image upload.
- Imported existing employee image folders into `faces/<employee_id>/`.
- Built local embeddings.
- Stopped relying on camera's changing internal target IDs; those IDs are tracking IDs, not employee IDs.
- Added Frappe checkin creation by API/bench.
- Added image attachment to checkins.
- Changed attachment from full source image to matched face crop.
- Added high threshold and second-best margin rejection to reduce false checkins.
- Kept recognition local; no cloud model dependency.

## Known Problems

- Wide/high camera angles still produce weak faces. The app cannot create reliable identity from a tiny or side-facing face.
- If threshold is too low, visitors can be matched to the nearest employee.
- If threshold is too high, some real employees will be rejected.
- Bad enrollment images hurt accuracy. Blurry, side-angle, cropped, or wrong-person images should be removed.
- Blind auto-learning is dangerous. It can poison an employee profile with the wrong face.

## Safe Next Steps

1. Keep FTP running and watcher stopped while collecting more samples.
2. Review rejected faces under `logs/unknown/`.
3. Clean employee enrollment folders.
4. Run controlled tests with one known employee and one visitor.
5. Tune `threshold` and `min_score_margin` from real log scores.
6. Only after stable matching, run the watcher live.

Recommended live start command:

```bash
cd /home/nvr2/face-attendance
source .venv/bin/activate
python face_attendance.py watch-folder
```

## Do Not Do

- Do not lower threshold back to `0.55`.
- Do not enable blind auto-learning from unknown or weak matches.
- Do not commit `faces/`, `camera_uploads/`, `logs/`, `embeddings.pkl`, or `config.json`.
- Do not run old Frigate/RTSP face workers at the same time when testing this app.
- Do not assume camera overlay target IDs are employee IDs.
- Do not switch to NVIDIA models unless the machine has NVIDIA GPU/CUDA.

