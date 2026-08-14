import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def write(path, content):
    (ROOT / path).write_text(content, encoding="utf-8")


def replace_once(path, old, new):
    source = read(path)
    count = source.count(old)
    if count != 1:
        raise SystemExit(f"expected one match in {path}, found {count}: {old[:120]!r}")
    write(path, source.replace(old, new, 1))


def replace_between(path, start, end, replacement):
    source = read(path)
    start_index = source.find(start)
    if start_index < 0:
        raise SystemExit(f"start marker not found in {path}: {start!r}")
    end_index = source.find(end, start_index + len(start))
    if end_index < 0:
        raise SystemExit(f"end marker not found in {path}: {end!r}")
    write(path, source[:start_index] + replacement + source[end_index:])


def patch_config_loaders():
    replace_once(
        "face_attendance.py",
        "from model_runtime import ModelRuntimeError, create_face_analysis\n",
        "from model_runtime import ModelRuntimeError, create_face_analysis\n"
        "from secret_store import ConfigLoadError, load_runtime_config\n",
    )
    replace_between(
        "face_attendance.py",
        "def load_config():\n",
        "\n\ndef face_app(",
        '''def load_config():
    try:
        return load_runtime_config(CONFIG)
    except ConfigLoadError as exc:
        raise SystemExit(str(exc)) from exc


''',
    )

    replace_once(
        "ftp_receiver.py",
        "from data_contract import safe_log_message\n",
        "from data_contract import safe_log_message\n"
        "from secret_store import ConfigLoadError, load_runtime_config\n",
    )
    replace_between(
        "ftp_receiver.py",
        "def load_config():\n",
        "\n\ndef resolve_folder(",
        '''def load_config():
    try:
        return load_runtime_config(CONFIG)
    except ConfigLoadError as exc:
        raise SystemExit(str(exc)) from exc


''',
    )


def patch_secure_sync():
    replace_once(
        "secure_sync.py",
        "from gallery_release import (\n",
        "from gallery_credentials import (\n"
        "    GalleryCredentialError,\n"
        "    outbound_gallery_credential,\n"
        ")\n"
        "from gallery_release import (\n",
    )
    replace_between(
        "secure_sync.py",
        "def _request_headers(cfg, release_state, *, conditional=True):\n",
        "\n\ndef _validate_source(",
        '''def _request_headers(credential, release_state, *, conditional=True):
    headers = {
        "Accept": "application/json",
        "User-Agent": "face-attendance-embedding-sync/4",
        "Authorization": f"Bearer {credential.token}",
        "X-Face-Attendance-Credential-ID": credential.credential_id,
    }
    etag = _text(release_state.get("etag"))
    if conditional and etag:
        headers["If-None-Match"] = etag
    return headers


''',
    )
    marker = '''    requested_url = _validate_source(cfg)
    resolved_url = requested_url
'''
    replacement = '''    try:
        credential = outbound_gallery_credential(cfg)
    except GalleryCredentialError as exc:
        raise GalleryError(f"central gallery credential is invalid: {exc}") from exc
    requested_url = _validate_source(cfg)
    resolved_url = requested_url
'''
    replace_once("secure_sync.py", marker, replacement)
    replace_once(
        "secure_sync.py",
        '''    branch = _text(cfg.get("branch_name"))
    params = {"branch": branch} if branch else None
''',
        '''    branch = _text(cfg.get("branch_name"))
    model = _text(cfg.get("model"))
    model_version = _text(cfg.get("model_version"))
    params = {
        key: value
        for key, value in {
            "branch": branch,
            "model": model,
            "model_version": model_version,
        }.items()
        if value
    } or None
''',
    )
    replace_once(
        "secure_sync.py",
        '''                    headers=_request_headers(
                        cfg,
                        previous_release,
                        conditional=gallery_path.exists(),
                    ),
''',
        '''                    headers=_request_headers(
                        credential,
                        previous_release,
                        conditional=gallery_path.exists(),
                    ),
''',
    )
    replace_once(
        "secure_sync.py",
        '''                    result["gallery_age_seconds"] = freshness["age_seconds"]
''',
        '''                    result["credential_id"] = credential.credential_id
                    result["credential_fingerprint"] = credential.fingerprint
                    result["gallery_age_seconds"] = freshness["age_seconds"]
''',
    )
    replace_once(
        "secure_sync.py",
        '''                result["gallery_age_seconds"] = freshness["age_seconds"]
''',
        '''                result["credential_id"] = credential.credential_id
                result["credential_fingerprint"] = credential.fingerprint
                result["gallery_age_seconds"] = freshness["age_seconds"]
''',
    )


def patch_production_readiness():
    replace_once(
        "production_readiness.py",
        "from camera_sources import camera_source_configuration_issues\n",
        "from auth_backends import auth_configuration_issues as auth_backend_configuration_issues\n"
        "from camera_sources import camera_source_configuration_issues\n"
        "from gallery_credentials import gallery_credential_configuration_issues\n",
    )
    replace_once(
        "production_readiness.py",
        "from runtime_policy import inspect_gallery, strict_profile_issues\n"
        "from web_security import auth_configuration_issues\n",
        "from runtime_policy import inspect_gallery, strict_profile_issues\n"
        "from secret_store import (\n"
        "    ConfigLoadError,\n"
        "    external_secret_configuration_issues,\n"
        "    load_runtime_config,\n"
        ")\n"
        "from web_security import proxy_configuration_issues\n",
    )
    replace_once(
        "production_readiness.py",
        '''    for code, message in strict_profile_issues(cfg):
        issues.append(ReadinessIssue(code, message))

''',
        '''    for code, message in strict_profile_issues(cfg):
        issues.append(ReadinessIssue(code, message))

    for message in gallery_credential_configuration_issues(cfg):
        issues.append(ReadinessIssue("gallery_credentials_invalid", message))
    for message in external_secret_configuration_issues(cfg):
        issues.append(ReadinessIssue("external_secret_delivery_invalid", message))

''',
    )
    replace_once(
        "production_readiness.py",
        '''    for message in auth_configuration_issues(cfg):
        issues.append(
            ReadinessIssue("web_admin_auth_invalid", message)
        )
''',
        '''    for message in auth_backend_configuration_issues(cfg):
        issues.append(
            ReadinessIssue("web_admin_auth_invalid", message)
        )
    for message in proxy_configuration_issues(cfg):
        issues.append(
            ReadinessIssue("trusted_proxy_configuration_invalid", message)
        )
''',
    )
    replace_once(
        "production_readiness.py",
        '''    if bool(cfg.get("embedding_export_enabled", False)) and is_placeholder(
        cfg.get("embedding_export_token")
    ):
        issues.append(
            ReadinessIssue(
                "embedding_export_token_missing",
                "embedding_export_enabled requires a non-placeholder token",
            )
        )

''',
        "",
    )
    replace_between(
        "production_readiness.py",
        "def load_config(path):\n",
        "\n\ndef main():",
        '''def load_config(path):
    try:
        return load_runtime_config(path)
    except ConfigLoadError as exc:
        raise SystemExit(str(exc)) from exc


''',
    )


def patch_web_admin():
    replace_once(
        "web_admin.py",
        "from data_contract import (\n",
        "from auth_backends import (\n"
        "    AuthBackendError,\n"
        "    auth_configured,\n"
        "    auth_mode,\n"
        "    authenticate_local,\n"
        "    begin_external_login,\n"
        "    complete_external_login,\n"
        ")\n"
        "from data_contract import (\n",
    )
    replace_once(
        "web_admin.py",
        "from embedding_gallery import GalleryError, gallery_status, load_gallery, read_sync_status\n",
        "from embedding_gallery import GalleryError, gallery_status, load_gallery, read_sync_status\n"
        "from gallery_credentials import (\n"
        "    GalleryCredentialError,\n"
        "    authenticate_export_credential,\n"
        ")\n",
    )
    replace_once(
        "web_admin.py",
        "from runtime_state import RuntimeState, resolve_runtime_path\n",
        "from runtime_state import RuntimeState, resolve_runtime_path\n"
        "from secret_store import ConfigLoadError, RuntimeConfig, load_runtime_config\n",
    )
    replace_once(
        "web_admin.py",
        '''    auth_configured,
    configure_app,
''',
        '''    configure_app,
''',
    )
    replace_once(
        "web_admin.py",
        '''    remote_address,
    safe_next_url,
    validate_csrf,
    verify_password,
''',
        '''    peer_address,
    remote_address,
    safe_next_url,
    validate_csrf,
''',
    )
    replace_once(
        "web_admin.py",
        '''SETUP = """<!doctype html><style>{{style}}</style><div class=card><h1>Admin setup required</h1>
<p class=bad>The web UI is locked.</p><pre>cd /opt/face-attendance
source .venv/bin/activate
python manage_admin.py set-password</pre></div>"""
''',
        '''SETUP = """<!doctype html><style>{{style}}</style><div class=card><h1>Admin setup required</h1>
<p class=bad>The web UI is locked.</p><pre>cd /opt/face-attendance
source .venv/bin/activate
python manage_admin.py set-password</pre></div>"""

AUTH_ERROR = """<!doctype html><style>{{style}}</style><div class=card>
<h1>Authentication unavailable</h1><p class=bad>{{message}}</p></div>"""
''',
    )
    replace_between(
        "web_admin.py",
        "def load_config():\n",
        "\n\ndef apply_security_config():",
        '''def load_config():
    try:
        return load_runtime_config(CONFIG)
    except ConfigLoadError:
        return RuntimeConfig()


''',
    )
    replace_once(
        "web_admin.py",
        '''def apply_security_config():
    return configure_app(app, load_config())
''',
        '''def apply_security_config():
    cfg = load_config()
    configure_app(app, cfg)
    return auth_configured(cfg)
''',
    )
    replace_between(
        "web_admin.py",
        "def audit(action, detail=None, actor=None):\n",
        "\n\ndef payload(cfg):",
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
    )

    login_block = '''@app.route("/login", methods=["GET", "POST"])
def login():
    cfg = load_config()
    if not auth_configured(cfg):
        return render_template_string(SETUP, style=STYLE), 503
    mode = auth_mode(cfg)
    if request.method == "GET":
        if session.get("admin_authenticated"):
            return redirect(url_for("index"))
        next_url = safe_next_url(request.args.get("next"))
        if mode == "adapter":
            state = secrets.token_urlsafe(32)
            session.clear()
            session.update(
                external_auth_state=state,
                external_auth_next=next_url,
            )
            session.permanent = True
            callback_url = str(
                cfg.get("web_auth_callback_url")
                or url_for("auth_callback", _external=True)
            )
            try:
                target = begin_external_login(
                    cfg,
                    next_url=next_url,
                    state=state,
                    callback_url=callback_url,
                )
            except AuthBackendError as exc:
                audit(
                    "external_login_begin_failed",
                    {"error": str(exc)[:500]},
                    "anonymous",
                    cfg,
                )
                return render_template_string(
                    AUTH_ERROR,
                    style=STYLE,
                    message="External authentication could not be started.",
                ), 503
            return redirect(target)
        return render_template_string(
            LOGIN,
            style=STYLE,
            csrf=csrf_token(),
            next_url=next_url,
            message="",
        )

    if mode != "local":
        abort(405)
    validate_csrf()
    user = str(request.form.get("username") or "").strip()
    password = str(request.form.get("password") or "")
    next_url = safe_next_url(request.form.get("next"))
    client_ip = remote_address(cfg)
    limiter_key = f"login:{client_ip}"
    store = state_store(cfg)
    allowed, retry = store.login_allowed(limiter_key)
    if not allowed:
        audit("login_locked", {"username": user}, "anonymous", cfg)
        return (
            render_template_string(
                LOGIN,
                style=STYLE,
                csrf=csrf_token(),
                next_url=next_url,
                message=f"Try again in {retry} seconds.",
            ),
            429,
        )
    principal = authenticate_local(cfg, user, password)
    if principal is None:
        store.record_login_failure(
            limiter_key,
            max_attempts=cfg.get("web_login_attempts", 5),
            window_seconds=cfg.get("web_login_window_seconds", 300),
            lockout_seconds=cfg.get("web_lockout_seconds", 900),
        )
        audit("login_failed", {"username": user}, "anonymous", cfg)
        return (
            render_template_string(
                LOGIN,
                style=STYLE,
                csrf=csrf_token(),
                next_url=next_url,
                message="Invalid username or password.",
            ),
            401,
        )
    store.clear_login_failures(limiter_key)
    session.clear()
    session.update(
        admin_authenticated=True,
        admin_user=principal.subject,
        auth_assurance=principal.assurance,
        auth_mfa=principal.mfa,
    )
    session.permanent = True
    csrf_token()
    audit(
        "login_succeeded",
        {"assurance": principal.assurance, "mfa": principal.mfa},
        principal.subject,
        cfg,
    )
    return redirect(next_url)


@app.get("/auth/callback")
def auth_callback():
    cfg = load_config()
    if auth_mode(cfg) != "adapter" or not auth_configured(cfg):
        abort(404)
    expected_state = str(session.get("external_auth_state") or "")
    next_url = safe_next_url(session.get("external_auth_next"))
    callback_url = str(
        cfg.get("web_auth_callback_url")
        or url_for("auth_callback", _external=True)
    )
    try:
        principal = complete_external_login(
            cfg,
            query=request.args.to_dict(flat=True),
            expected_state=expected_state,
            callback_url=callback_url,
        )
    except AuthBackendError as exc:
        session.clear()
        audit(
            "external_login_failed",
            {"error": str(exc)[:500]},
            "anonymous",
            cfg,
        )
        return render_template_string(
            AUTH_ERROR,
            style=STYLE,
            message="External authentication was rejected.",
        ), 401
    session.clear()
    session.update(
        admin_authenticated=True,
        admin_user=principal.subject,
        auth_assurance=principal.assurance,
        auth_mfa=principal.mfa,
    )
    session.permanent = True
    csrf_token()
    audit(
        "login_succeeded",
        {
            "backend": "adapter",
            "assurance": principal.assurance,
            "mfa": principal.mfa,
        },
        principal.subject,
        cfg,
    )
    return redirect(next_url)


'''
    replace_between(
        "web_admin.py",
        '@app.route("/login", methods=["GET", "POST"])\n',
        '@app.post("/logout")\n',
        login_block,
    )

    export_block = '''def _rate_limit_response(retry_after):
    response = jsonify(error="rate limit exceeded")
    response.status_code = 429
    response.headers["Retry-After"] = str(max(1, int(retry_after)))
    return response


@app.get("/api/faces/embeddings")
def export_embeddings():
    cfg = load_config()
    if not cfg.get("embedding_export_enabled"):
        abort(404)
    client_ip = remote_address(cfg)
    store = state_store(cfg)
    credential_id = str(
        request.headers.get("X-Face-Attendance-Credential-ID") or ""
    ).strip()
    try:
        credential = authenticate_export_credential(
            cfg,
            request.headers.get("Authorization", ""),
            credential_id,
            branch=str(cfg.get("branch_name") or ""),
            model=str(cfg.get("model") or ""),
            model_version=str(cfg.get("model_version") or ""),
        )
    except GalleryCredentialError as exc:
        allowed, retry, _remaining = store.consume_rate_limit(
            f"embedding-export-auth:{client_ip}",
            limit=max(1, int(cfg.get("embedding_export_auth_failures", 10))),
            window_seconds=max(
                1,
                int(cfg.get("embedding_export_auth_failure_window_seconds", 300)),
            ),
        )
        audit(
            "embedding_export_denied",
            {
                "credential_id": credential_id[:128],
                "reason": str(exc)[:200],
            },
            "anonymous",
            cfg,
        )
        if not allowed:
            audit(
                "embedding_export_rate_limited",
                {"bucket": "authentication", "retry_after": retry},
                "anonymous",
                cfg,
            )
            return _rate_limit_response(retry)
        abort(401)

    window = max(
        1,
        int(cfg.get("embedding_export_rate_limit_window_seconds", 60)),
    )
    allowed, retry, _remaining = store.consume_rate_limit(
        f"embedding-export-credential:{credential.credential_id}",
        limit=max(1, int(cfg.get("embedding_export_rate_limit_requests", 120))),
        window_seconds=window,
    )
    if allowed:
        allowed, retry, _remaining = store.consume_rate_limit(
            f"embedding-export-client:{credential.credential_id}:{client_ip}",
            limit=max(
                1,
                int(cfg.get("embedding_export_ip_rate_limit_requests", 60)),
            ),
            window_seconds=window,
        )
    if not allowed:
        audit(
            "embedding_export_rate_limited",
            {
                "credential_id": credential.credential_id,
                "credential_fingerprint": credential.fingerprint,
                "retry_after": retry,
            },
            f"credential:{credential.credential_id}",
            cfg,
        )
        return _rate_limit_response(retry)

    try:
        data = payload(cfg)
    except GalleryError as exc:
        audit(
            "embedding_export_failed",
            {
                "credential_id": credential.credential_id,
                "error": str(exc)[:500],
            },
            f"credential:{credential.credential_id}",
            cfg,
        )
        return jsonify(error=str(exc)), 503

    requested = {}
    for name in ("branch", "model", "model_version"):
        try:
            requested[name] = validate_gallery_label(
                request.args.get(name),
                name,
                required=False,
                max_chars=128,
            )
        except GalleryError as exc:
            return jsonify(error=str(exc)), 400
    actual = {
        "branch": str(data.get("branch") or ""),
        "model": str(data.get("model") or ""),
        "model_version": str(data.get("model_version") or ""),
    }
    if any(requested[name] and requested[name] != actual[name] for name in requested):
        audit(
            "embedding_export_scope_mismatch",
            {
                "credential_id": credential.credential_id,
                "requested": requested,
            },
            f"credential:{credential.credential_id}",
            cfg,
        )
        abort(404)

    checksum = str(data["checksum"])
    not_modified = bool(
        request.if_none_match and request.if_none_match.contains(checksum)
    )
    response = (
        make_response("", 304)
        if not_modified
        else make_response(jsonify(data))
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["ETag"] = f'"{checksum}"'
    response.headers[
        "X-Face-Attendance-Credential-ID"
    ] = credential.credential_id
    audit(
        "embedding_export_not_modified"
        if not_modified
        else "embedding_export_succeeded",
        {
            "credential_id": credential.credential_id,
            "credential_fingerprint": credential.fingerprint,
            "branch": actual["branch"],
            "model": actual["model"],
            "model_version": actual["model_version"],
            "checksum": checksum[:16],
            "status": 304 if not_modified else 200,
            "user_agent": str(request.headers.get("User-Agent") or "")[:200],
        },
        f"credential:{credential.credential_id}",
        cfg,
    )
    return response



'''
    replace_between(
        "web_admin.py",
        "def export_allowed(cfg):\n",
        'if __name__ == "__main__":\n',
        export_block,
    )


def patch_config_example():
    path = ROOT / "config.example.json"
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg["frappe_api_secret"] = "systemd://frappe_api_secret"
    cfg["camera_source_receipt_secret"] = "systemd://camera_source_receipt_secret"
    cfg["ftp_users"]["camera_in"]["password"] = "systemd://ftp_camera_in_password"
    cfg["ftp_users"]["camera_out"]["password"] = "systemd://ftp_camera_out_password"
    cfg["web_auth_mode"] = "local"
    cfg["web_auth_adapter"] = ""
    cfg["web_auth_allowed_redirect_hosts"] = []
    cfg["web_auth_callback_url"] = ""
    cfg["web_mfa_required"] = False
    cfg["web_admin_password_hash"] = "systemd://web_admin_password_hash"
    cfg["web_session_secret"] = "systemd://web_session_secret"
    cfg["web_trust_proxy_headers"] = True
    cfg["web_trusted_proxy_networks"] = ["127.0.0.1/32", "::1/128"]
    cfg["web_forwarded_for_header"] = "X-Forwarded-For"
    cfg["web_max_forwarded_hops"] = 8
    cfg["production_external_secrets_required"] = True
    cfg.pop("central_api_token", None)
    cfg["central_api_credential_id"] = "baghdad-node-2026-q3"
    cfg["central_api_credentials"] = {
        "baghdad-node-2026-q3": {
            "token": "systemd://central_gallery_token_2026_q3",
            "scopes": ["gallery:read"],
            "branches": ["CHANGE_ME"],
            "models": ["buffalo_l"],
            "model_versions": ["CHANGE_ME"],
            "not_before": "",
            "expires_at": "",
            "enabled": True,
        }
    }
    cfg.pop("embedding_export_token", None)
    cfg["embedding_export_credentials"] = {
        "baghdad-node-2026-q3": {
            "token": "systemd://gallery_export_token_2026_q3",
            "scopes": ["gallery:read"],
            "branches": ["CHANGE_ME"],
            "models": ["buffalo_l"],
            "model_versions": ["CHANGE_ME"],
            "not_before": "",
            "expires_at": "",
            "enabled": True,
        }
    }
    cfg["embedding_export_rate_limit_requests"] = 120
    cfg["embedding_export_rate_limit_window_seconds"] = 60
    cfg["embedding_export_ip_rate_limit_requests"] = 60
    cfg["embedding_export_auth_failures"] = 10
    cfg["embedding_export_auth_failure_window_seconds"] = 300
    cfg["pad_http_token"] = "systemd://pad_http_token"
    path.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def patch_tests():
    replace_once(
        "test_web_security.py",
        "import unittest\n\n",
        '''import unittest

from test_h0_credential_auth import (
    ExternalAuthAdapterH11Tests,
    GalleryCredentialH11Tests,
    RuntimeRateLimitH11Tests,
    SecretStoreH11Tests,
    SystemdCredentialExampleH11Tests,
    TrustedProxyH11Tests,
    WebAdminH11Tests,
)

''',
    )

    replace_once(
        "test_secure_sync.py",
        '            "central_api_token": "secret",\n',
        '            "central_api_token": "s" * 32,\n',
    )
    replace_once(
        "test_secure_sync.py",
        '''            "production_mode": True,
            "model_version": "approved-v1",
''',
        '''            "production_mode": True,
            "central_api_credential_id": "node-2026",
            "central_api_credentials": {
                "node-2026": {
                    "token": "t" * 48,
                    "scopes": ["gallery:read"],
                    "branches": ["Baghdad"],
                    "models": ["buffalo_l"],
                    "model_versions": ["approved-v1"],
                    "enabled": True,
                }
            },
            "model_version": "approved-v1",
''',
    )
    replace_once(
        "test_secure_sync.py",
        '            result["source_url"], "https://central.example.test/v2/embeddings"\n        )\n        self.assertTrue(first.closed)\n',
        '            result["source_url"], "https://central.example.test/v2/embeddings"\n        )\n        self.assertEqual(result["credential_id"], "legacy-central-token")\n        self.assertTrue(first.closed)\n',
    )
    replace_once(
        "test_secure_sync.py",
        '''            session.calls[1][1]["headers"]["Authorization"], "Bearer secret"
''',
        '''            session.calls[1][1]["headers"]["Authorization"],
            "Bearer " + "s" * 32,
''',
    )
    insert_marker = '''    def test_strict_policy_is_checked_before_network(self):
'''
    insert = '''    def test_structured_credential_header_and_scope_are_enforced(self):
        session = FakeSession(
            [
                FakeResponse(
                    body=self.signed(1),
                    headers={"Content-Type": "application/json"},
                )
            ]
        )
        result = sync_gallery(
            self.release_cfg,
            self.gallery,
            self.status,
            session=session,
            sleep=lambda _: None,
        )
        headers = session.calls[0][1]["headers"]
        self.assertEqual(headers["Authorization"], "Bearer " + "t" * 48)
        self.assertEqual(
            headers["X-Face-Attendance-Credential-ID"], "node-2026"
        )
        self.assertEqual(result["credential_id"], "node-2026")

        wrong_scope = self.release_cfg.copy()
        wrong_scope["central_api_credentials"] = {
            "node-2026": {
                **self.release_cfg["central_api_credentials"]["node-2026"],
                "branches": ["Basra"],
            }
        }
        blocked = FakeSession([])
        with self.assertRaisesRegex(Exception, "not scoped"):
            sync_gallery(
                wrong_scope,
                self.gallery,
                self.status,
                session=blocked,
                sleep=lambda _: None,
            )
        self.assertEqual(blocked.calls, [])

'''
    replace_once("test_secure_sync.py", insert_marker, insert + insert_marker)

    replace_once(
        "test_production_readiness.py",
        "from web_security import hash_password\n",
        "from secret_store import RuntimeConfig\nfrom web_security import hash_password\n",
    )
    replace_once(
        "test_production_readiness.py",
        '''    def valid_config(self):
        return {
''',
        '''    def valid_config(self):
        cfg = {
''',
    )
    replace_once(
        "test_production_readiness.py",
        '''            "central_url": "https://central.example.test",
            "central_api_token": "secret",
''',
        '''            "central_url": "https://central.example.test",
            "central_api_credential_id": "readiness-node-2026",
            "central_api_credentials": {
                "readiness-node-2026": {
                    "token": "c" * 48,
                    "scopes": ["gallery:read"],
                    "branches": ["Baghdad"],
                    "models": ["licensed_model"],
                    "model_versions": ["v1"],
                    "enabled": True,
                }
            },
            "production_external_secrets_required": True,
''',
    )
    replace_once(
        "test_production_readiness.py",
        '            "pad_http_token": "secret",\n',
        '            "pad_http_token": "p" * 48,\n',
    )
    replace_once(
        "test_production_readiness.py",
        '''            "web_admin_username": "admin",
''',
        '''            "web_auth_mode": "local",
            "web_mfa_required": False,
            "web_admin_username": "admin",
''',
    )
    replace_once(
        "test_production_readiness.py",
        '''            "https_reverse_proxy_acknowledged": True,
''',
        '''            "https_reverse_proxy_acknowledged": True,
            "web_trust_proxy_headers": True,
            "web_trusted_proxy_networks": ["127.0.0.1/32", "::1/128"],
            "web_forwarded_for_header": "X-Forwarded-For",
            "web_max_forwarded_hops": 8,
''',
    )
    replace_once(
        "test_production_readiness.py",
        '''            },
        }

    def activate_gallery(
''',
        '''            },
        }
        return RuntimeConfig(
            cfg,
            secret_sources={
                "central_api_credentials.readiness-node-2026.token": "systemd://central_gallery_token",
                "pad_http_token": "systemd://pad_http_token",
                "web_admin_password_hash": "systemd://web_admin_password_hash",
                "web_session_secret": "systemd://web_session_secret",
                "camera_source_receipt_secret": "systemd://camera_source_receipt_secret",
                "ftp_users.camera_in.password": "systemd://ftp_camera_in_password",
                "ftp_users.camera_out.password": "systemd://ftp_camera_out_password",
            },
        )

    def activate_gallery(
''',
    )
    readiness_marker = '''    def test_plain_ftp_requires_isolation_ack(self):
'''
    readiness_tests = '''    def test_inline_secret_and_untrusted_proxy_are_blocked(self):
        cfg = self.valid_config()
        cfg.secret_sources.pop("web_session_secret")
        cfg["web_trust_proxy_headers"] = False
        report = self.report(cfg, verify_model_files=False)
        codes = {issue.code for issue in report.blockers}
        self.assertIn("external_secret_delivery_invalid", codes)
        self.assertIn("trusted_proxy_configuration_invalid", codes)

    def test_gallery_credential_scope_mismatch_is_blocked(self):
        cfg = self.valid_config()
        cfg["central_api_credentials"] = {
            "readiness-node-2026": {
                **cfg["central_api_credentials"]["readiness-node-2026"],
                "branches": ["Basra"],
            }
        }
        report = self.report(cfg, verify_model_files=False)
        self.assertIn(
            "gallery_credentials_invalid",
            {issue.code for issue in report.blockers},
        )

'''
    replace_once(
        "test_production_readiness.py",
        readiness_marker,
        readiness_tests + readiness_marker,
    )


def patch_plan():
    replace_once(
        "docs/attendance-platform-plan.md",
        "- [ ] `H0-11` Add scoped/rotatable gallery credentials",
        "- [x] `H0-11` Add scoped/rotatable gallery credentials",
    )


def main():
    patch_config_loaders()
    patch_secure_sync()
    patch_production_readiness()
    patch_web_admin()
    patch_config_example()
    patch_tests()
    patch_plan()


if __name__ == "__main__":
    main()
