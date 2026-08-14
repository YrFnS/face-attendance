import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

try:
    import flask  # noqa: F401
except ModuleNotFoundError:
    flask = None

from auth_backends import (
    auth_configuration_issues,
    begin_external_login,
    complete_external_login,
)
from gallery_credentials import (
    GalleryCredentialError,
    authenticate_export_credential,
    outbound_gallery_credential,
)
from runtime_state import RuntimeState
from secret_store import (
    RuntimeConfig,
    SecretStoreError,
    external_secret_configuration_issues,
    resolve_config_secrets,
    secret_source_map,
)

if flask is not None:
    from flask import Flask

    import web_admin
    from embedding_gallery import write_gallery_atomic
    from web_security import hash_password, remote_address


def gallery_payload():
    return {
        "schema_version": 1,
        "gallery_version": "h0-11-test",
        "generated_at": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "model": "buffalo_l",
        "model_version": "v1",
        "dimension": 3,
        "normalized": True,
        "branch": "Baghdad",
        "employees": [
            {"employee": "HR-EMP-1", "embeddings": [[1.0, 0.0, 0.0]]}
        ],
    }


def credential_config(token="t" * 48):
    return {
        "production_mode": True,
        "branch_name": "Baghdad",
        "model": "buffalo_l",
        "model_version": "v1",
        "central_api_credential_id": "baghdad-node-2026-q3",
        "central_api_credentials": {
            "baghdad-node-2026-q3": {
                "token": token,
                "scopes": ["gallery:read"],
                "branches": ["Baghdad"],
                "models": ["buffalo_l"],
                "model_versions": ["v1"],
                "enabled": True,
            }
        },
        "embedding_export_credentials": {
            "baghdad-node-2026-q3": {
                "token": token,
                "scopes": ["gallery:read"],
                "branches": ["Baghdad"],
                "models": ["buffalo_l"],
                "model_versions": ["v1"],
                "enabled": True,
            }
        },
    }


class SecretStoreH11Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        os.chmod(self.root, 0o700)

    def tearDown(self):
        self.temp.cleanup()

    def write_secret(self, name, value):
        path = self.root / name
        path.write_text(value + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
        return path

    def test_systemd_and_environment_references_are_resolved_with_provenance(self):
        self.write_secret("central_token", "c" * 48)
        cfg = resolve_config_secrets(
            {
                "central_api_credentials": {
                    "node": {"token": "systemd://central_token"}
                },
                "web_session_secret": "env://WEB_SESSION_SECRET",
            },
            environ={
                "CREDENTIALS_DIRECTORY": str(self.root),
                "WEB_SESSION_SECRET": "s" * 48,
            },
        )
        self.assertEqual(
            cfg["central_api_credentials"]["node"]["token"],
            "c" * 48,
        )
        self.assertEqual(cfg["web_session_secret"], "s" * 48)
        sources = secret_source_map(cfg)
        self.assertEqual(
            sources["central_api_credentials.node.token"],
            "systemd://central_token",
        )
        self.assertEqual(
            sources["web_session_secret"],
            "env://WEB_SESSION_SECRET",
        )

    @unittest.skipIf(os.name == "nt", "POSIX symlink and permission test")
    def test_secret_file_symlink_is_rejected(self):
        target = self.write_secret("target", "x" * 48)
        link = self.root / "link"
        link.symlink_to(target)
        with self.assertRaisesRegex(SecretStoreError, "symbolic link"):
            resolve_config_secrets(
                {"web_session_secret": f"file://{link}"},
            )

    def test_production_reports_inline_high_value_secret(self):
        cfg = RuntimeConfig(
            {
                "production_mode": True,
                "production_external_secrets_required": True,
                "web_session_secret": "s" * 48,
            },
            secret_sources={},
        )
        issues = external_secret_configuration_issues(cfg)
        self.assertTrue(any("web_session_secret" in issue for issue in issues))
        cfg.secret_sources["web_session_secret"] = "systemd://web_session_secret"
        self.assertFalse(external_secret_configuration_issues(cfg))


class GalleryCredentialH11Tests(unittest.TestCase):
    def test_outbound_credential_is_scoped_and_identified(self):
        credential = outbound_gallery_credential(credential_config())
        self.assertEqual(credential.credential_id, "baghdad-node-2026-q3")
        self.assertEqual(credential.fingerprint, credential.fingerprint.lower())

    def test_wrong_branch_and_expired_credentials_fail_closed(self):
        cfg = credential_config()
        cfg["branch_name"] = "Basra"
        with self.assertRaisesRegex(GalleryCredentialError, "not scoped"):
            outbound_gallery_credential(cfg)

        cfg = credential_config()
        cfg["central_api_credentials"]["baghdad-node-2026-q3"][
            "expires_at"
        ] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        with self.assertRaisesRegex(GalleryCredentialError, "validity window"):
            outbound_gallery_credential(cfg)

    def test_export_authentication_requires_id_token_and_scope(self):
        cfg = credential_config()
        credential = authenticate_export_credential(
            cfg,
            "Bearer " + "t" * 48,
            "baghdad-node-2026-q3",
            branch="Baghdad",
            model="buffalo_l",
            model_version="v1",
        )
        self.assertEqual(credential.credential_id, "baghdad-node-2026-q3")
        with self.assertRaisesRegex(GalleryCredentialError, "invalid"):
            authenticate_export_credential(
                cfg,
                "Bearer wrong",
                "baghdad-node-2026-q3",
                branch="Baghdad",
                model="buffalo_l",
                model_version="v1",
            )


class RuntimeRateLimitH11Tests(unittest.TestCase):
    def test_persistent_rate_limit_blocks_and_resets(self):
        with tempfile.TemporaryDirectory() as directory:
            state = RuntimeState(Path(directory) / "runtime.sqlite3")
            self.assertEqual(
                state.consume_rate_limit(
                    "export:node",
                    limit=2,
                    window_seconds=60,
                    now=100,
                ),
                (True, 0, 1),
            )
            self.assertEqual(
                state.consume_rate_limit(
                    "export:node",
                    limit=2,
                    window_seconds=60,
                    now=101,
                ),
                (True, 0, 0),
            )
            allowed, retry, remaining = state.consume_rate_limit(
                "export:node",
                limit=2,
                window_seconds=60,
                now=102,
            )
            self.assertFalse(allowed)
            self.assertGreater(retry, 0)
            self.assertEqual(remaining, 0)
            self.assertTrue(
                state.consume_rate_limit(
                    "export:node",
                    limit=2,
                    window_seconds=60,
                    now=161,
                )[0]
            )


@unittest.skipIf(flask is None, "Flask dependency is not installed")
class TrustedProxyH11Tests(unittest.TestCase):
    def test_forwarded_chain_is_used_only_from_trusted_peer(self):
        app = Flask(__name__)
        cfg = {
            "web_trust_proxy_headers": True,
            "web_trusted_proxy_networks": [
                "127.0.0.1/32",
                "10.0.0.0/8",
            ],
            "web_forwarded_for_header": "X-Forwarded-For",
            "web_max_forwarded_hops": 8,
        }
        with app.test_request_context(
            "/",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
            headers={"X-Forwarded-For": "203.0.113.9, 10.1.2.3"},
        ):
            self.assertEqual(remote_address(cfg), "203.0.113.9")
        with app.test_request_context(
            "/",
            environ_base={"REMOTE_ADDR": "198.51.100.4"},
            headers={"X-Forwarded-For": "203.0.113.9"},
        ):
            self.assertEqual(remote_address(cfg), "198.51.100.4")


class ExternalAuthAdapterH11Tests(unittest.TestCase):
    def setUp(self):
        module = types.ModuleType("h0_fake_auth_adapter")

        class Adapter:
            supports_mfa = True

            def begin_login(self, *, next_url, state, callback_url):
                return f"https://login.example.test/authorize?state={state}"

            def complete_login(self, *, query, expected_state, callback_url):
                return {
                    "subject": "admin@example.test",
                    "display_name": "Admin",
                    "mfa": True,
                    "assurance": "oidc-mfa",
                }

        module.create_adapter = lambda cfg: Adapter()
        sys.modules[module.__name__] = module
        self.module_name = module.__name__
        self.cfg = {
            "web_auth_mode": "adapter",
            "web_auth_adapter": f"{self.module_name}:create_adapter",
            "web_auth_allowed_redirect_hosts": ["login.example.test"],
            "web_mfa_required": True,
            "web_session_secret": "s" * 48,
        }

    def tearDown(self):
        sys.modules.pop(self.module_name, None)

    def test_adapter_contract_enforces_state_redirect_host_and_mfa(self):
        self.assertFalse(auth_configuration_issues(self.cfg))
        url = begin_external_login(
            self.cfg,
            next_url="/",
            state="state-value",
            callback_url="https://attendance.example.test/auth/callback",
        )
        self.assertTrue(url.startswith("https://login.example.test/"))
        principal = complete_external_login(
            self.cfg,
            query={"state": "state-value", "code": "ok"},
            expected_state="state-value",
            callback_url="https://attendance.example.test/auth/callback",
        )
        self.assertEqual(principal.subject, "admin@example.test")
        self.assertTrue(principal.mfa)
        with self.assertRaisesRegex(Exception, "state validation"):
            complete_external_login(
                self.cfg,
                query={"state": "wrong"},
                expected_state="state-value",
                callback_url="https://attendance.example.test/auth/callback",
            )


@unittest.skipIf(flask is None, "Flask dependency is not installed")
class WebAdminH11Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config = root / "config.json"
        self.gallery = root / "embedding_gallery.json"
        self.status = root / "embedding_sync_status.json"
        self.faces = root / "faces"
        self.runtime = root / "runtime_state.sqlite3"
        self.token = "e" * 48
        self.config.write_text(
            json.dumps(
                {
                    "branch_name": "Baghdad",
                    "model": "buffalo_l",
                    "model_version": "v1",
                    "require_model_match": True,
                    "require_model_version_match": True,
                    "reject_stale_embedding_gallery": True,
                    "embedding_max_age_seconds": 3600,
                    "embedding_export_enabled": True,
                    "embedding_export_credentials": {
                        "node-2026": {
                            "token": self.token,
                            "scopes": ["gallery:read"],
                            "branches": ["Baghdad"],
                            "models": ["buffalo_l"],
                            "model_versions": ["v1"],
                            "enabled": True,
                        }
                    },
                    "embedding_export_rate_limit_requests": 1,
                    "embedding_export_rate_limit_window_seconds": 60,
                    "embedding_export_ip_rate_limit_requests": 2,
                    "embedding_export_auth_failures": 1,
                    "embedding_export_auth_failure_window_seconds": 60,
                    "local_enrollment_enabled": False,
                    "web_auth_mode": "local",
                    "web_mfa_required": False,
                    "web_admin_username": "admin",
                    "web_admin_password_hash": hash_password(
                        "correct horse battery staple"
                    ),
                    "web_session_secret": "s" * 48,
                    "web_cookie_secure": False,
                    "web_hsts_enabled": False,
                    "web_trust_proxy_headers": True,
                    "web_trusted_proxy_networks": ["127.0.0.1/32"],
                    "web_forwarded_for_header": "X-Forwarded-For",
                    "web_max_forwarded_hops": 8,
                    "runtime_state_db": str(self.runtime),
                    "model_license_acknowledged": True,
                }
            ),
            encoding="utf-8",
        )
        write_gallery_atomic(
            self.gallery,
            gallery_payload(),
            expected_model="buffalo_l",
            expected_branch="Baghdad",
        )
        self.patches = [
            patch.object(web_admin, "CONFIG", self.config),
            patch.object(web_admin, "GALLERY", self.gallery),
            patch.object(web_admin, "SYNC_STATUS", self.status),
            patch.object(web_admin, "FACES", self.faces),
        ]
        for item in self.patches:
            item.start()
        web_admin.apply_security_config()
        web_admin.app.config.update(TESTING=True)
        self.client = web_admin.app.test_client()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def export_headers(self):
        return {
            "Authorization": f"Bearer {self.token}",
            "X-Face-Attendance-Credential-ID": "node-2026",
            "X-Forwarded-For": "203.0.113.8",
        }

    def test_export_is_audited_and_rate_limited_per_credential(self):
        first = self.client.get(
            "/api/faces/embeddings?branch=Baghdad&model=buffalo_l&model_version=v1",
            headers=self.export_headers(),
        )
        self.assertEqual(first.status_code, 200)
        second = self.client.get(
            "/api/faces/embeddings?branch=Baghdad&model=buffalo_l&model_version=v1",
            headers=self.export_headers(),
        )
        self.assertEqual(second.status_code, 429)
        self.assertIn("Retry-After", second.headers)
        actions = [
            item["action"]
            for item in RuntimeState(self.runtime).recent_audit(limit=10)
        ]
        self.assertIn("embedding_export_succeeded", actions)
        self.assertIn("embedding_export_rate_limited", actions)

    def test_invalid_export_credential_is_audited_without_token(self):
        response = self.client.get(
            "/api/faces/embeddings?branch=Baghdad",
            headers={
                "Authorization": "Bearer wrong",
                "X-Face-Attendance-Credential-ID": "node-2026",
                "X-Forwarded-For": "203.0.113.9",
            },
        )
        self.assertEqual(response.status_code, 401)
        row = RuntimeState(self.runtime).recent_audit(limit=1)[0]
        self.assertEqual(row["action"], "embedding_export_denied")
        self.assertNotIn("wrong", json.dumps(row))

    def test_external_adapter_login_flow_sets_mfa_session(self):
        module = types.ModuleType("h0_web_adapter")

        class Adapter:
            supports_mfa = True

            def begin_login(self, *, next_url, state, callback_url):
                return f"https://login.example.test/start?state={state}"

            def complete_login(self, *, query, expected_state, callback_url):
                return {
                    "subject": "admin@example.test",
                    "mfa": True,
                    "assurance": "mfa",
                }

        module.create_adapter = lambda cfg: Adapter()
        sys.modules[module.__name__] = module
        try:
            cfg = json.loads(self.config.read_text(encoding="utf-8"))
            cfg.update(
                web_auth_mode="adapter",
                web_auth_adapter=f"{module.__name__}:create_adapter",
                web_auth_allowed_redirect_hosts=["login.example.test"],
                web_mfa_required=True,
            )
            self.config.write_text(json.dumps(cfg), encoding="utf-8")
            web_admin.apply_security_config()
            response = self.client.get("/login?next=/")
            self.assertEqual(response.status_code, 302)
            self.assertTrue(
                response.headers["Location"].startswith(
                    "https://login.example.test/"
                )
            )
            with self.client.session_transaction() as session:
                state = session["external_auth_state"]
            callback = self.client.get(
                f"/auth/callback?state={state}&code=ok"
            )
            self.assertEqual(callback.status_code, 302)
            with self.client.session_transaction() as session:
                self.assertTrue(session["admin_authenticated"])
                self.assertTrue(session["auth_mfa"])
                self.assertEqual(session["admin_user"], "admin@example.test")
        finally:
            sys.modules.pop(module.__name__, None)


class SystemdCredentialExampleH11Tests(unittest.TestCase):
    def test_systemd_drop_in_documents_loadcredential(self):
        text = Path("deploy/systemd/credentials.example.conf").read_text(
            encoding="utf-8"
        )
        self.assertIn("LoadCredential=web_session_secret:", text)
        self.assertIn("LoadCredential=central_gallery_token_2026_q3:", text)
        self.assertIn("CREDENTIALS_DIRECTORY", text)
