# Face Attendance

Standalone face-recognition bridge for HOLOWITS camera FTP captures and ERPNext `Employee Checkin`.

Employee enrollment photos no longer need to be copied to every attendance server. A trusted enrollment server builds normalized InsightFace embeddings and exposes an authenticated embedding-gallery API. Attendance servers synchronize that gallery, create a temporary embedding from each camera capture, match locally, and create ERPNext checkins.

## Architecture

```text
Trusted enrollment server
  employee photos
    -> InsightFace buffalo_l
    -> embedding_gallery.json
    -> authenticated /api/faces/embeddings

Attendance server
  authenticated embedding sync
    -> atomic validation and activation
    -> live gallery reload without restart

HOLOWITS camera
  -> FTP Target/Person capture
  -> temporary local image
  -> InsightFace query embedding
  -> cosine similarity + threshold + second-best margin
  -> ERPNext Employee Checkin
  -> optional audit crop
  -> optional source-image deletion
```

The camera still sends an image because it cannot send an InsightFace embedding. The important privacy change is that **employee reference images are not distributed to attendance servers**. Camera source images and audit crops are independently configurable.

## Main files

- `face_attendance.py` — recognition/check-in helpers and legacy diagnostic commands that refuse live processing.
- `watch_service.py` — production FTP watcher with readiness, PAD, replay protection, and event state.
- `embedding_gallery.py` — validates, normalizes, stores, and reloads galleries.
- `secure_sync.py` — authoritative bounded gallery-sync client with authenticated, same-origin redirect validation.
- `sync_embeddings.py` — manual or continuous gallery synchronization.
- `legacy_gallery_converter.py` — explicit offline converter for a trusted legacy `embeddings.pkl`; never used during service startup.
- `web_admin.py` — gallery status, manual sync, optional central enrollment UI, and optional export API.
- `ftp_receiver.py` — receives camera FTP uploads.
- `import_faces.py` — compatibility wrapper; now syncs embeddings instead of downloading images.
- `docs/embedding-api.md` — API contract for an existing central dashboard.
- `docs/attendance-platform-plan.md` — phased plan for durable delivery, camera policy, operator workflows, enrollment, biometric assurance, and production validation.

## Linux install

```bash
git clone https://github.com/YrFnS/face-attendance.git
cd face-attendance
bash install_linux.sh
```

The installer copies the app to `/opt/face-attendance`, creates a virtual environment, installs requirements, and enables:

```text
face-attendance-ftp
face-attendance-watch
face-attendance-web
face-attendance-sync.timer
```

On a fresh installation, FTP and the web UI start, but the live watcher stays stopped until a valid `embedding_gallery.json` exists. A legacy `embeddings.pkl` never enables the watcher. This prevents accidental checkin creation before enrollment is verified.

Edit the runtime config and restart services:

```bash
sudo nano /opt/face-attendance/config.json
sudo systemctl restart face-attendance-*
```

## Attendance-server configuration

The attendance server receives embeddings and should normally have local enrollment disabled:

```json
{
  "central_url": "https://faces.example.com",
  "central_api_token": "REPLACE_WITH_A_LONG_RANDOM_TOKEN",
  "branch_name": "بغداد - الحارثية",
  "embedding_gallery_path": "/api/faces/embeddings",
  "embedding_sync_enabled": true,
  "embedding_sync_interval_seconds": 300,
  "embedding_max_age_seconds": 86400,
  "embedding_max_redirects": 3,
  "require_model_match": true,
  "local_enrollment_enabled": false,
  "model": "buffalo_l"
}
```

Use HTTPS. Plain HTTP to a non-local host is rejected unless `allow_insecure_central_url` is explicitly enabled for a trusted VPN or isolated LAN. Redirects are followed manually and only when they stay on the same origin; HTTPS downgrades and cross-origin redirects are rejected before the bearer token can be sent to the redirected destination.

Run the first sync before enabling live checkins:

```bash
cd /opt/face-attendance
. .venv/bin/activate
python sync_embeddings.py
python sync_embeddings.py --status
python production_readiness.py --strict
python watch_service.py --once --dry-run --allow-stale
```

The synchronization timer refreshes the gallery, and the production watcher loads valid changes without restarting. A failed, empty, wrong-branch, wrong-model, malformed, or dimension-incompatible gallery is rejected while the previous working gallery remains active.

## Trusted enrollment/export server

One installation of this repository can act as the trusted single-branch enrollment server:

```json
{
  "branch_name": "بغداد - الحارثية",
  "model": "buffalo_l",
  "local_enrollment_enabled": true,
  "embedding_export_enabled": true,
  "embedding_export_token": "THE_SAME_SECRET_USED_BY_BRANCH_CLIENTS"
}
```

Open the web UI, upload clear images under the ERPNext Employee ID, and rebuild embeddings:

```text
http://SERVER-IP:8088
```

The authenticated export endpoint is:

```text
GET /api/faces/embeddings?branch=بغداد%20-%20الحارثية
Authorization: Bearer <token>
```

For a separate central dashboard, implement the same contract described in `docs/embedding-api.md`.

## Migration from image synchronization and legacy pickle files

Older deployments used:

```bash
python import_faces.py
python face_attendance.py build
```

`import_faces.py` now invokes the bounded embedding synchronization client and no longer downloads central employee images.

Service startup **never deserializes `embeddings.pkl`**. If a legacy pickle exists while `embedding_gallery.json` does not, startup stops with an actionable error. The preferred migration is to rebuild or synchronize a fresh JSON gallery. Use the converter only when the local pickle is genuinely required and its provenance can be verified from a trusted inventory, backup record, or release record.

Pickle deserialization can execute code. Stop attendance services, isolate the host from untrusted networks, compare the file with the trusted SHA-256 record, and then run the explicit one-shot converter:

```bash
cd /opt/face-attendance
. .venv/bin/activate
sudo systemctl stop face-attendance-watch face-attendance-web face-attendance-sync.timer

sha256sum embeddings.pkl
# Compare the result with the trusted pre-recorded digest. Do not treat a digest
# calculated from an untrusted file as proof that the file itself is trustworthy.

python legacy_gallery_converter.py \
  --expected-sha256 <TRUSTED_64_CHARACTER_SHA256> \
  --acknowledge-pickle-code-execution-risk

python sync_embeddings.py --status
python production_readiness.py --strict
```

The converter refuses to overwrite an existing JSON gallery, creates a mode-`0600` backup before deserialization, validates and atomically writes the JSON gallery, and moves the original pickle into `legacy_quarantine/` by default. Keep the backup and quarantine only for the approved rollback window, then dispose of them under the biometric-retention policy. When no trusted provenance record exists, do not deserialize the pickle; rebuild or synchronize the gallery instead.

After a successful central sync and controlled tests, the attendance server's `faces/` directory can be removed. Keep it only on the trusted enrollment server or while retaining a temporary rollback path.

## Camera and audit-image controls

Recommended privacy-oriented settings:

```json
{
  "save_latest_face": false,
  "save_rejected_crops": true,
  "save_checkin_crops": true,
  "attach_checkin_crop": true,
  "delete_camera_uploads_after_processing": true,
  "audit_retention_days": 7
}
```

Behavior:

- `delete_camera_uploads_after_processing` deletes a readable FTP capture only after processing completes. A processing exception preserves the source image for investigation.
- `save_rejected_crops` retains rejected audit crops under `logs/unknown/`.
- `save_checkin_crops` retains accepted audit crops under `logs/checkins/`.
- `attach_checkin_crop` attaches the accepted crop to ERPNext. It can work with `save_checkin_crops: false`; a temporary file is deleted after upload.
- `audit_retention_days` removes old accepted/rejected crops hourly. Set `0` to disable cleanup.

## Matching safeguards

Recognition remains open-set. A visitor must be rejected rather than forced to the nearest employee. The accepted face must pass:

- minimum face width and height;
- detector confidence;
- cosine similarity threshold;
- second-best employee margin;
- per-image duplicate protection;
- employee cooldown.

Recommended starting values remain:

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

Do not lower the threshold aggressively to make rejected samples pass. Tune only from controlled employee-versus-visitor tests.

## Camera FTP settings

In the HOLOWITS camera UI:

- FTP attribute: `Target/Person`
- Protocol: `FTP`
- Port: `2121`
- File naming: Track ID + Time
- IN camera user maps to `camera_uploads/in`
- OUT camera user maps to `camera_uploads/out`

Folder mapping is controlled by:

```json
{
  "folder_log_types": {
    "in": "IN",
    "out": "OUT"
  }
}
```

## Commands

```bash
# Sync and inspect the employee embedding gallery
python sync_embeddings.py
python sync_embeddings.py --status
python face_attendance.py status

# Explicit offline migration of a trusted legacy pickle only
python legacy_gallery_converter.py \
  --expected-sha256 <TRUSTED_64_CHARACTER_SHA256> \
  --acknowledge-pickle-code-execution-risk

# Central/local fallback enrollment only
python face_attendance.py build
python face_attendance.py enroll HR-EMP-00001 --photos 5

# Production camera processing
python watch_service.py
python watch_service.py --dry-run
python watch_service.py --once --dry-run --allow-stale
```

`watch_service.py` is the only supported live watcher. The legacy `face_attendance.py watch` and `watch-folder` commands fail closed unless `--dry-run` is present, because they do not provide the production readiness, PAD, replay, and event-ledger controls.

Service status and logs:

```bash
sudo systemctl status face-attendance-ftp
sudo systemctl status face-attendance-watch
sudo systemctl status face-attendance-web
journalctl -u face-attendance-watch -f
journalctl -u face-attendance-web -f
tail -f /opt/face-attendance/logs/watch.log
```

## Runtime files

The following contain local state or biometric data and are ignored by Git:

- `config.json`
- `embedding_gallery.json`
- `embedding_sync_status.json`
- legacy `embeddings.pkl`
- `legacy_backups/`
- `legacy_quarantine/`
- `faces/`
- `camera_uploads/`
- `logs/`
- `cooldown_state.json`

Do not commit employee photos, embeddings, camera captures, legacy pickle backups, logs, API tokens, or passwords.

### Inspect and resolve attendance events

After migrating `runtime_state.sqlite3` to the current schema, use the dedicated event CLI:

```bash
python event_admin.py list --database runtime_state.sqlite3
python event_admin.py explain EVENT_ID --database runtime_state.sqlite3
```

Audited reprocess, quarantine-resolution, and dismissal commands require an actor, a human reason, and explicit confirmation. They never retry or cancel ERPNext delivery. See `docs/event-operations.md` for the complete safety and recovery workflow.
