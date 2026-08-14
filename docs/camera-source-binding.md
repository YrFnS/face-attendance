# Camera credential, route, and network binding

The canonical FTP path now treats camera identity as configured security data. It no longer guesses `IN`, `OUT`, or a camera ID from folder names.

## Required source registry

Each camera has one entry under `camera_sources`:

```json
{
  "branch_name": "Baghdad",
  "camera_uploads_dir": "/opt/face-attendance/camera_uploads",
  "camera_sources": {
    "entrance-in": {
      "source_type": "holowits_ftp",
      "branch": "Baghdad",
      "policy": "IN",
      "ftp_username": "camera_in",
      "upload_dir": "camera_uploads/in",
      "allowed_networks": ["192.168.68.121/32"]
    },
    "entrance-out": {
      "source_type": "holowits_ftp",
      "branch": "Baghdad",
      "policy": "OUT",
      "ftp_username": "camera_out",
      "upload_dir": "camera_uploads/out",
      "allowed_networks": ["192.168.68.122/32"]
    }
  },
  "ftp_users": {
    "camera_in": {
      "password": "UNIQUE-RANDOM-PASSWORD-FOR-IN",
      "permissions": "elw"
    },
    "camera_out": {
      "password": "UNIQUE-RANDOM-PASSWORD-FOR-OUT",
      "permissions": "elw"
    }
  }
}
```

The application refuses configurations with:

- an unbound FTP account;
- one username or password reused by multiple cameras;
- duplicate, nested, or out-of-root upload routes;
- a source branch different from `branch_name`;
- a policy other than `IN` or `OUT`;
- an empty network allowlist or an unrestricted `0.0.0.0/0` or `::/0` network;
- non-upload FTP permissions.

`presence` remains unsupported until the separate presence-session design is implemented.

## Network enforcement

The FTP receiver checks the authenticated username and remote IP together. A valid password from an IP outside that camera's configured CIDR is disconnected and cannot leave a completed upload.

Use the narrowest practical CIDR. A camera with a stable address should normally use a `/32` IPv4 or `/128` IPv6 entry. If DHCP is unavoidable, reserve the address or bind a small dedicated camera subnet rather than a broad office network.

## Verified upload receipt

After a complete image reaches its bound route, the FTP receiver calculates its SHA-256 and writes a companion receipt:

```text
event.jpg
event.jpg.source.json
```

The receipt records:

- camera ID, source type, branch, and `IN`/`OUT` policy;
- authenticated FTP username;
- normalized remote IP;
- server receipt time;
- image SHA-256 and byte size;
- the configured source-binding ID;
- an HMAC-SHA-256 signature.

Configure a unique node secret with at least 32 UTF-8 bytes:

```json
{
  "camera_source_receipt_required": true,
  "camera_source_receipt_secret": "REPLACE-WITH-A-LONG-RANDOM-NODE-SECRET",
  "camera_source_receipt_future_tolerance_seconds": 300
}
```

The watcher verifies the route, receipt signature, image hash, size, camera binding, username, source IP, branch, and policy before face detection, PAD, recognition, or ERPNext delivery. Missing or altered receipts fail closed in production.

The receipt moves with the image into quarantine and is deleted with the source image after successful processing. Do not copy an image without its receipt into a production route.

## Migration order

1. Stop the FTP receiver and watcher.
2. Assign a unique FTP username and a unique random password to every camera.
3. Create one `camera_sources` entry per camera.
4. Set the exact branch, `IN`/`OUT` policy, upload route, and allowed camera network.
5. Set a new `camera_source_receipt_secret`; do not reuse an FTP, ERPNext, gallery, or web secret.
6. Remove or quarantine old images that do not have a verified source receipt.
7. Run `python production_readiness.py --strict`.
8. Start the FTP receiver before the watcher and perform one controlled upload from each camera.
9. Confirm that uploads from an unapproved IP and a wrong credential are rejected.

## Security boundary

Unique credentials, a dedicated route, an IP allowlist, FTPS or an isolated network, and a signed local receipt provide strong source attribution for the supported deployment. They do not prove hardware identity and do not stop a compromised camera or stolen credential from submitting a newly encoded replay.

Where the camera and transport support stronger authentication, prefer a device-bound client certificate, a per-device SSH key, or an authenticated site-to-site VPN/IPsec identity. Any such transport must still enter the same source registry, receipt, event ledger, PAD, recognition, and delivery path. Credential rotation and external secret storage remain part of H0-11.
