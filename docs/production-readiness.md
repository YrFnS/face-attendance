# Production Readiness

The production watcher can now fail closed on the remaining controls that can be verified in software. Enable the gate only after the deployment has a licensed and pinned recognition model, a validated presentation-attack-detection service, and protected network paths.

## 1. Record and pin the recognition model

The application cannot grant a license for third-party model weights. Obtain the appropriate rights for the exact configured model, record the contract or internal approval reference, and update:

```json
{
  "model_license_acknowledged": true,
  "model_license_reference": "CONTRACT-OR-APPROVAL-ID",
  "model_directory": "/home/service-user/.insightface/models/buffalo_l",
  "model_manifest_path": "model_manifest.json"
}
```

Create a manifest only after the approved model files are installed under the service account:

```bash
cd /opt/face-attendance
source .venv/bin/activate
python model_manifest.py create --license-reference "CONTRACT-OR-APPROVAL-ID"
python model_manifest.py verify
```

The manifest records the exact model name, version, directory, file sizes, and SHA-256 hashes. The production watcher verifies it once at startup. A changed, missing, extra, or mismatched model file blocks production mode.

Creating a manifest is an integrity control, not proof that a license was obtained.

## 2. Configure PAD/liveness

NIST's current digital identity guidance requires presentation-attack detection for facial recognition. This project does not bundle an unvalidated anti-spoofing model. Instead, it provides a strict adapter for a locally operated or trusted HTTPS PAD service.

Configure:

```json
{
  "pad_provider": "http",
  "pad_required": true,
  "pad_fail_closed": true,
  "pad_require_single_face": true,
  "pad_min_score": 0.8,
  "pad_http_url": "https://pad.internal.example/v1/check",
  "pad_http_token": "LONG-RANDOM-PER-SERVER-TOKEN"
}
```

The watcher sends a JPEG face crop as multipart form field `image` and a JSON string as form field `context`. The service must return JSON:

```json
{
  "live": true,
  "score": 0.94,
  "model": "approved-pad-model-v3",
  "evidence_id": "PAD-2026-000123",
  "reason": ""
}
```

Accepted score values are from `0` through `1`. A network error, malformed response, explicit attack decision, or score below `pad_min_score` rejects the camera event when fail-closed mode is enabled.

The PAD service and threshold need their own controlled evaluation against printed photos, phone/tablet replays, masks relevant to the workplace, camera positions, lighting, and the actual employee population. The adapter alone does not make an untested PAD model trustworthy.

## 3. Protect the web interface

The web service binds to `127.0.0.1:8088`. Use one of the templates:

```text
deploy/caddy/Caddyfile.example
deploy/nginx/face-attendance.conf.example
```

For Caddy, set `FACE_ATTENDANCE_DOMAIN` and `ACME_EMAIL`, copy the template into the Caddy configuration, validate it, and reload Caddy. Caddy can obtain and renew HTTPS certificates automatically for a public DNS name that resolves to the server.

After confirming HTTPS, authenticated login, secure cookies, and `/readyz`, set:

```json
"https_reverse_proxy_acknowledged": true
```

Never expose port `8088` publicly.

## 4. Protect camera transport

Prefer explicit FTPS when the camera supports it:

```json
{
  "ftp_tls_enabled": true,
  "ftp_tls_certfile": "/etc/face-attendance/ftp-fullchain.pem",
  "ftp_tls_keyfile": "/etc/face-attendance/ftp-privkey.pem",
  "ftp_tls_control_required": true,
  "ftp_tls_data_required": true
}
```

The certificate and private key must be readable by the face-attendance service account. Use unique camera credentials and limit the control and passive ports to camera addresses.

When a camera cannot use FTPS, put the camera and server on a verified isolated VLAN or VPN, firewall the FTP control/passive ports to camera addresses only, and set:

```json
"camera_network_isolated_acknowledged": true
```

`deploy/firewall/ufw-rules.example.sh` is a reviewed starting point. It deliberately does not reset or enable UFW.

## 5. Run the readiness check

Before enabling live production mode:

```bash
cd /opt/face-attendance
source .venv/bin/activate
python production_readiness.py --strict
```

For a fast configuration-only check that skips hashing large model files:

```bash
python production_readiness.py --strict --skip-model-hash
```

After every blocker is resolved:

```json
"production_mode": true
```

Then restart and inspect logs:

```bash
sudo systemctl restart face-attendance-ftp face-attendance-web face-attendance-watch
sudo systemctl start face-attendance-sync.service
journalctl -u face-attendance-watch -n 200 --no-pager
```

The systemd service and bundled Windows launchers must execute `watch_service.py`. The legacy watcher paths refuse non-dry-run execution. In production mode, a canonical live watcher start is refused when license acknowledgement, model integrity, PAD, admin authentication, HTTPS acknowledgement, protected camera transport, camera IDs, or required HTTPS service URLs are invalid. Dry-run mode remains available for controlled setup and diagnostics.

## 6. Controlled acceptance tests

At minimum, verify:

- known employees at IN and OUT cameras;
- unknown visitors;
- a duplicate camera image after restart;
- an old and a future-dated upload;
- central embedding service outage;
- PAD service outage;
- explicit PAD attack response;
- score just below and above the selected PAD threshold;
- printed-photo and screen-replay attacks;
- ERPNext outage and recovery;
- HTTPS login, CSRF rejection, and session expiration;
- FTPS or isolated-network camera upload;
- model-file modification causing production startup rejection.

Keep the pull request in draft until the model license and PAD evaluation are documented for the actual deployment.
