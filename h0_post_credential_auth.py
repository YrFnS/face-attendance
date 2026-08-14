from pathlib import Path


def replace_exact(path, old, new, *, count=1):
    target = Path(path)
    source = target.read_text(encoding="utf-8")
    actual = source.count(old)
    if actual != count:
        raise SystemExit(
            f"expected {count} match(es) in {path}, found {actual}: {old[:120]!r}"
        )
    target.write_text(source.replace(old, new), encoding="utf-8")


replace_exact(
    "secret_store.py",
    '''    config = resolve_config_secrets(document, environ=environ)
    config.source_path = path.resolve()
    return config
''',
    '''    try:
        config = resolve_config_secrets(document, environ=environ)
    except SecretStoreError as exc:
        raise ConfigLoadError(f"invalid secret configuration in {path}: {exc}") from exc
    config.source_path = path.resolve()
    return config
''',
)

replace_exact(
    "manage_admin.py",
    '''from secret_store import (
''',
    '''from auth_backends import auth_configured
from secret_store import (
''',
)
replace_exact(
    "manage_admin.py",
    '''from web_security import auth_configured, hash_password
''',
    '''from web_security import hash_password
''',
)

replace_exact(
    "web_admin.py",
    '''def audit(action, detail=None, actor=None, cfg=None):
    cfg = cfg or load_config()
    detail = dict(detail or {})
    peer = peer_address()
    client = remote_address(cfg)
    if peer != client:
        detail.setdefault("proxy_peer", peer)
    try:
        state_store(cfg).audit(
            actor=actor or admin_user() or "anonymous",
            action=action,
            remote_addr=client,
            detail=detail,
        )
    except Exception:
        app.logger.exception("audit write failed")
''',
    '''def audit(action, detail=None, actor=None, cfg=None, *, required=False):
    cfg = cfg or load_config()
    detail = dict(detail or {})
    peer = peer_address()
    client = remote_address(cfg)
    if peer != client:
        detail.setdefault("proxy_peer", peer)
    required = required or str(action).startswith("embedding_export_")
    try:
        state_store(cfg).audit(
            actor=actor or admin_user() or "anonymous",
            action=action,
            remote_addr=client,
            detail=detail,
        )
    except Exception:
        app.logger.exception("audit write failed")
        if required:
            raise
''',
)

replace_exact(
    "test_h0_credential_auth.py",
    '''from secret_store import (
    RuntimeConfig,
''',
    '''from secret_store import (
    ConfigLoadError,
    RuntimeConfig,
''',
)
replace_exact(
    "test_h0_credential_auth.py",
    '''    external_secret_configuration_issues,
    resolve_config_secrets,
''',
    '''    external_secret_configuration_issues,
    load_runtime_config,
    resolve_config_secrets,
''',
)
replace_exact(
    "test_h0_credential_auth.py",
    '''    def test_production_reports_inline_high_value_secret(self):
''',
    '''    def test_missing_secret_reference_becomes_a_clean_config_error(self):
        config = self.root / "config.json"
        config.write_text(
            json.dumps({"web_session_secret": "systemd://missing"}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ConfigLoadError, "invalid secret configuration"):
            load_runtime_config(
                config,
                environ={"CREDENTIALS_DIRECTORY": str(self.root)},
            )

    def test_production_reports_inline_high_value_secret(self):
''',
)

replace_exact(
    "docs/credential-auth-hardening.md",
    '''sudo install -d -m 0700 /etc/face-attendance/credentials
sudo -u face-attendance /opt/face-attendance/.venv/bin/python \\
''',
    '''sudo install -d -o face-attendance -g face-attendance -m 0700 \\
  /etc/face-attendance/credentials
sudo -u face-attendance /opt/face-attendance/.venv/bin/python \\
''',
)
replace_exact(
    "docs/credential-auth-hardening.md",
    '''Use `deploy/systemd/credentials.example.conf` as a drop-in template. The service-specific drop-in should contain only the credentials that service needs.
''',
    '''Use `deploy/systemd/credentials.example.conf` as a drop-in template. The current shared `config.json` is resolved eagerly because watcher startup and `/readyz` evaluate the complete production profile. Every service that loads that shared file must therefore receive every referenced credential. A future split-config deployment may narrow credentials per service, but removing a referenced credential from only one unit will make that unit fail closed at configuration load.
''',
)
replace_exact(
    "docs/credential-auth-hardening.md",
    '''No generic adapter is enabled by default. A missing, unloadable, non-MFA-capable, or malformed adapter fails production readiness and login closed.
''',
    '''Set `web_auth_callback_url` explicitly to the public HTTPS callback when the reverse proxy does not preserve the external host and scheme. No generic adapter is enabled by default. A missing, unloadable, non-MFA-capable, or malformed adapter fails production readiness and login closed.
''',
)
