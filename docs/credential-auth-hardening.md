# Credential rotation, export controls, and administrative authentication

H0-11 removes the remaining single-token and proxy-attribution assumptions from the live path. Production operators should complete this migration before enabling gallery export or relying on the web administration service.

## External secret references

Any config string may explicitly reference a secret instead of storing it inline:

```json
{
  "web_session_secret": "systemd://web_session_secret",
  "pad_http_token": "env://FACE_ATTENDANCE_PAD_TOKEN",
  "frappe_api_secret": "file:///run/secrets/frappe_api_secret"
}
```

Supported sources are:

- `systemd://NAME`: read `NAME` from `CREDENTIALS_DIRECTORY` or `FACE_ATTENDANCE_CREDENTIALS_DIRECTORY`;
- `env://NAME`: read an explicitly named environment variable;
- `file:///absolute/path`: read a regular, non-symlinked, owner-only UTF-8 file.

Secret files are bounded to 64 KiB, must not be symbolic links, and on POSIX must not grant group or other access. The runtime records only the reference source, never the resolved value. `production_external_secrets_required` keeps inline high-value credentials from passing production readiness.

Use `deploy/systemd/credentials.example.conf` as a drop-in template. The current shared `config.json` is resolved eagerly because watcher startup and `/readyz` evaluate the complete production profile. Every service that loads that shared file must therefore receive every referenced credential. A future split-config deployment may narrow credentials per service, but removing a referenced credential from only one unit will make that unit fail closed at configuration load.

The admin helper can create credential files directly:

```bash
sudo install -d -o face-attendance -g face-attendance -m 0700 \
  /etc/face-attendance/credentials
sudo -u face-attendance /opt/face-attendance/.venv/bin/python \
  /opt/face-attendance/manage_admin.py set-password \
  --credential-directory /etc/face-attendance/credentials
```

## Scoped and rotatable gallery credentials

The attendance node selects one outbound credential by ID:

```json
{
  "central_api_credential_id": "baghdad-node-2026-q3",
  "central_api_credentials": {
    "baghdad-node-2026-q3": {
      "token": "systemd://central_gallery_token_2026_q3",
      "scopes": ["gallery:read"],
      "branches": ["Baghdad"],
      "models": ["buffalo_l"],
      "model_versions": ["approved-2026-08"],
      "not_before": "2026-08-01T00:00:00Z",
      "expires_at": "2026-11-01T00:00:00Z",
      "enabled": true
    }
  }
}
```

The export server accepts a separate set under `embedding_export_credentials`. Every request includes both:

```text
Authorization: Bearer <token>
X-Face-Attendance-Credential-ID: baghdad-node-2026-q3
```

Production credentials require explicit branch, model, and model-version scopes. Disabled, not-yet-valid, expired, unknown, reused, or out-of-scope credentials fail before gallery data is returned. Legacy `central_api_token` and `embedding_export_token` remain non-production compatibility fields only.

### Rotation procedure

1. Add the new export credential while the old one remains active.
2. Deliver the new token through a new external secret reference.
3. Update each attendance node's `central_api_credential_id` and credential entry.
4. Confirm a successful sync and audit record with the new credential ID.
5. Disable the old export credential, observe one full synchronization interval, then remove it.
6. Keep credential IDs unique; never reuse an ID for different token material or scope.

## Export auditing and throttling

Gallery export now writes an audit row for successful, not-modified, denied, and rate-limited requests. Audit details contain the credential ID, token fingerprint, verified client address, branch/model/version, response status, and checksum prefix. Tokens are never logged.

Limits are persistent in `runtime_state.sqlite3`:

```json
{
  "embedding_export_rate_limit_requests": 120,
  "embedding_export_rate_limit_window_seconds": 60,
  "embedding_export_ip_rate_limit_requests": 60,
  "embedding_export_auth_failures": 10,
  "embedding_export_auth_failure_window_seconds": 300
}
```

The server applies both a per-credential limit and a credential-plus-client limit. Invalid credentials use a separate client-address bucket. Rate-limited responses include `Retry-After`.

## Trusted proxy client addresses

Login and export throttling trust `X-Forwarded-For` only when the immediate peer belongs to an explicitly configured proxy CIDR:

```json
{
  "web_trust_proxy_headers": true,
  "web_trusted_proxy_networks": ["127.0.0.1/32", "::1/128"],
  "web_forwarded_for_header": "X-Forwarded-For",
  "web_max_forwarded_hops": 8
}
```

The chain is evaluated from the trusted peer toward the client. A header from an untrusted peer, an invalid address, an oversized chain, or a non-allowlisted proxy is ignored. Never trust `0.0.0.0/0` or `::/0`.

## Organizational SSO and MFA adapter

The built-in local password backend intentionally does not claim MFA support. Organizations can provide an adapter using:

```json
{
  "web_auth_mode": "adapter",
  "web_auth_adapter": "company_face_auth:create_adapter",
  "web_auth_allowed_redirect_hosts": ["login.example.com"],
  "web_mfa_required": true
}
```

The factory receives the resolved runtime config and returns an object implementing:

```python
begin_login(next_url: str, state: str, callback_url: str) -> str
complete_login(query: dict, expected_state: str, callback_url: str) -> dict
```

The completion result must contain a stable `subject` and may include `display_name`, `assurance`, and boolean `mfa`. The core validates state, redirect-host allowlisting, principal fields, and the MFA requirement. The adapter remains responsible for protocol-specific OIDC/SAML token validation, nonce handling, issuer/audience checks, key rotation, group-to-admin authorization, and logout behavior.

Set `web_auth_callback_url` explicitly to the public HTTPS callback when the reverse proxy does not preserve the external host and scheme. No generic adapter is enabled by default. A missing, unloadable, non-MFA-capable, or malformed adapter fails production readiness and login closed.
