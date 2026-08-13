# Face Attendance Handoff

## Current goal

Create ERPNext `Employee Checkin` records from HOLOWITS Target/Person FTP captures while preferring rejection over a false employee match.

The current design separates biometric enrollment from attendance processing:

```text
Trusted enrollment server
  -> employee photos
  -> InsightFace buffalo_l embeddings
  -> authenticated embedding API

Attendance server
  -> validated embedding sync
  -> local camera query embedding
  -> open-set employee matching
  -> Employee Checkin
```

Employee reference photos no longer need to be copied to attendance servers.

## Main safety properties

- The central gallery is JSON, never a downloaded pickle.
- Every vector is checked for finite values, non-zero length, and exact dimension.
- In production, one shared policy requires explicit branch, model, model version, nonempty/fresh gallery state, and exact compatibility in sync, readiness, web status, and watcher loading.
- The manifest-verified `root/models/<model>` directory is the directory passed to and confirmed by the InsightFace runtime.
- Empty or malformed galleries are rejected.
- Updates are written atomically.
- The watcher reloads a valid changed gallery without a service restart.
- A failed sync or reload leaves the previous working gallery active.
- Multiple embeddings per employee are preserved.
- Recognition still requires threshold and second-best margin checks.

## Main files

See the authoritative [README file inventory](README.md#main-files).

## Runtime gallery files

```text
embedding_gallery.json
embedding_sync_status.json
```

Both are ignored by Git and written with restrictive file permissions where supported. `embeddings.pkl` is legacy local state only. Service startup never deserializes it; migration requires the explicit provenance-checked offline converter documented in the README.

## Roles

### Attendance server

Use:

```json
{
  "central_url": "https://faces.example.com",
  "central_api_token": "LONG_RANDOM_TOKEN",
  "branch_name": "بغداد - الحارثية",
  "embedding_sync_enabled": true,
  "local_enrollment_enabled": false,
  "model": "buffalo_l"
}
```

Synchronization and production watcher behavior are documented in [Security and Production Hardening](docs/security-hardening.md#separate-synchronization-and-recognition).

### Trusted enrollment server

Use:

```json
{
  "branch_name": "بغداد - الحارثية",
  "local_enrollment_enabled": true,
  "embedding_export_enabled": true,
  "embedding_export_token": "LONG_RANDOM_TOKEN",
  "model": "buffalo_l"
}
```

Upload images through the web UI, rebuild embeddings, and expose `/api/faces/embeddings` over HTTPS/VPN. A separate central dashboard may implement the same API contract instead.

## Commands

See the authoritative [README command reference](README.md#commands).

## Matching rules

A detected face must pass:

- `min_face_width` and `min_face_height`;
- `min_detection_score`;
- `threshold`;
- `min_score_margin` over the second-best employee;
- duplicate protection within the same image;
- `cooldown_seconds`.

Starting values:

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

Do not lower the threshold to force weak samples through. Collect controlled known-employee and visitor scores first.

## Image retention

Camera images are still required briefly because the camera supplies images, not embeddings. Retention is configurable:

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

A readable FTP source is deleted only after processing returns normally. Processing failures preserve the file. Accepted/rejected audit crops are cleaned hourly after the retention period.

## Existing VPS context

The previous production test used:

- app path: `/home/nvr2/face-attendance`;
- upload folders: `camera_uploads/in` and `camera_uploads/out`;
- Frappe site: `https://dr-atyaf.e2next.com`;
- FTP port: `2121`;
- separate IN and OUT FTP users.

Before enabling live checkin creation:

```bash
pgrep -af "watch_service.py|face_attendance.py (watch-folder|watch)|ftp_receiver.py|frigate|rtsp_face_gate.py|ffmpeg" || true
```

Run only the intended FTP receiver and the canonical `watch_service.py` watcher. Stop any legacy, Frigate, or RTSP recognition worker before live delivery.

## Safe deployment sequence

1. Deploy code and keep live watcher stopped.
2. Configure the central URL, token, branch, and model.
3. Run `python sync_embeddings.py`.
4. Confirm `python sync_embeddings.py --status` shows the correct branch, model, dimension, employee count, and embedding count.
5. Run the controlled production-watcher dry run from the [README command reference](README.md#commands) with a known employee and visitor.
6. Review threshold and margin logs.
7. Start the watcher only after false-positive behavior is acceptable.
8. Remove attendance-server enrollment photos after the central gallery is proven and a rollback decision is made.

## Do not do

- Do not distribute a pickle file from the central server.
- Do not expose embeddings without authentication and transport protection.
- Do not accept a gallery for another branch or model.
- Do not enable blind auto-learning from attendance captures.
- Do not treat the camera's changing target ID as an employee ID.
- Do not commit photos, embeddings, camera captures, logs, tokens, or passwords.
