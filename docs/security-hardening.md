# Security and Production Hardening

This document describes the production controls added on top of the embedding-gallery architecture.

## Important model licensing requirement

The application code does not grant a production or commercial license for recognition-model files. Before live commercial use, verify the terms for the exact model and weights configured in `model` / `model_version`, record that decision, and set:

```json
"model_license_acknowledged": true
```

Changing the recognition model requires rebuilding every employee embedding and recalibrating the recognition threshold and score margin. Never mix embeddings from different models or preprocessing pipelines.

## Web administration

The admin UI is locked until a password hash and persistent session secret exist. Configure it on the server:

```bash
cd /opt/face-attendance
source .venv/bin/activate
python manage_admin.py set-password
sudo systemctl restart face-attendance-web
```

The password is stored as a salted `scrypt` hash. The command also creates a high-entropy session secret and changes `config.json` to mode `0600`.

The service runs under Gunicorn and binds to `127.0.0.1:8088` by default. Put Caddy, Nginx, or another trusted reverse proxy in front of it and terminate HTTPS there. Do not expose the Gunicorn port directly. Keep `web_cookie_secure` and `web_hsts_enabled` enabled when HTTPS is in place.

All browser mutations require an authenticated session and CSRF token. Login attempts are persisted and rate-limited in `runtime_state.sqlite3`. Administrative login, logout, enrollment, build, and synchronization actions are recorded in the same database.

Unauthenticated operational endpoints:

- `GET /healthz` — the web process is alive.
- `GET /readyz` — authentication is configured and a permitted gallery is available.

The embedding export endpoint remains bearer-token protected and supports `ETag` / `If-None-Match`.

## Separate synchronization and recognition

Production recognition never needs to wait for a central-server request.

- `face-attendance-sync.timer` periodically runs `sync_embeddings.py --scheduled`.
- A validated gallery is written atomically.
- `watch_service.py` hot-reloads the local file.
- `embedding_sync_inline_enabled` defaults to `false`.

The sync client validates HTTPS, authentication, content type, response size, schema, branch, model, dimensions, and vector values. It uses conditional requests, bounded timeouts, and retry backoff. A failed sync leaves the last valid local gallery untouched.

Useful commands:

```bash
systemctl status face-attendance-sync.timer
systemctl start face-attendance-sync.service
journalctl -u face-attendance-sync.service -n 100
python sync_embeddings.py --status
```

## Camera event replay protection

The production watcher is `watch_service.py`, not the legacy direct folder command. It computes a SHA-256 digest for each completed camera upload and claims an event in SQLite before recognition. The same binary image from the same camera cannot be processed again, including after a restart.

The fail-safe behavior is intentional: an event left in `processing` after an abnormal crash remains blocked rather than risking a duplicate attendance record. Review the state before manual recovery.

The watcher also enforces:

- completed FTP staging before files enter the watched directory;
- camera-specific identity and IN/OUT direction;
- maximum file size and decoded pixel count;
- maximum event age and future-clock tolerance;
- quarantine for malformed, stale, or failed uploads;
- retention cleanup for event state and audit images.

Controlled historical testing must be explicit:

```bash
python watch_service.py --once --dry-run --allow-stale
```

The replay layer reduces static-file replay and duplicate delivery. It is not presentation-attack detection. Before high-trust production use, integrate and validate a real PAD/liveness signal from the camera or a tested model.

## FTP isolation

The FTP receiver uses per-user staging directories. Files remain under `.incoming` during transfer and are atomically moved into the watched directory only after pyftpdlib reports a complete upload. Incomplete files are removed.

Default user permissions are upload-oriented (`elw`) rather than full read/delete/rename permissions. Connections and per-IP connections are bounded.

FTP is still unencrypted unless the camera and deployment are configured for a protected transport. Keep it inside an isolated camera VLAN, private LAN, or VPN and firewall the command/passive ports to camera addresses only. Use unique credentials per camera.

## Deployment checklist

1. Replace every placeholder password and token in `config.json`.
2. Run `python manage_admin.py set-password`.
3. Confirm `config.json`, the gallery, SQLite state, and biometric directories are not world-readable.
4. Configure HTTPS reverse proxy access to `127.0.0.1:8088`.
5. Restrict FTP and passive ports to camera network addresses.
6. Give each camera a stable `camera_ids` value and separate FTP credentials.
7. Configure the central HTTPS URL and per-server token.
8. Run a manual embedding sync and inspect status.
9. Run known-employee, unknown-visitor, duplicate-image, stale-image, and restart tests in dry-run mode.
10. Verify model licensing and record the decision.
11. Add validated PAD/liveness before treating the system as resistant to printed-photo or screen attacks.
12. Start the watcher only after controlled acceptance testing.
