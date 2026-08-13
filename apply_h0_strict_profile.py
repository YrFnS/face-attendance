from pathlib import Path

ROOT = Path(__file__).resolve().parent


def read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def write(path, content):
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def replace_once(path, old, new):
    text = read(path)
    count = text.count(old)
    if count != 1:
        raise SystemExit(
            f"expected exactly one match in {path}, found {count}: {old[:120]!r}"
        )
    write(path, text.replace(old, new))


def replace_between(path, start_marker, end_marker, replacement):
    text = read(path)
    start = text.find(start_marker)
    if start < 0:
        raise SystemExit(f"start marker not found in {path}: {start_marker!r}")
    end = text.find(end_marker, start + len(start_marker))
    if end < 0:
        raise SystemExit(f"end marker not found in {path}: {end_marker!r}")
    write(path, text[:start] + replacement + text[end:])


write("runtime_policy.py", 'import time\nfrom datetime import datetime, timezone\nfrom pathlib import Path\n\nfrom embedding_gallery import GalleryError, load_gallery\nfrom model_manifest import is_placeholder\n\n\ndef _text(value):\n    return str(value or "").strip()\n\n\ndef _positive_int(value, field, default):\n    try:\n        number = int(default if value is None else value)\n    except (TypeError, ValueError) as exc:\n        raise GalleryError(f"{field} must be an integer") from exc\n    if number <= 0:\n        raise GalleryError(f"{field} must be greater than zero")\n    return number\n\n\ndef production_enabled(cfg):\n    return bool(cfg.get("production_mode", False))\n\n\ndef gallery_policy_issues(cfg):\n    if not production_enabled(cfg):\n        return ()\n\n    issues = []\n    branch = _text(cfg.get("branch_name"))\n    model = _text(cfg.get("model"))\n    model_version = _text(cfg.get("model_version"))\n\n    if is_placeholder(branch):\n        issues.append(\n            (\n                "branch_name_missing",\n                "branch_name must be a non-placeholder value in production",\n            )\n        )\n    if is_placeholder(model):\n        issues.append(\n            (\n                "model_name_missing",\n                "model must be a non-placeholder value in production",\n            )\n        )\n    if is_placeholder(model_version):\n        issues.append(\n            (\n                "model_version_missing",\n                "model_version must identify the approved runtime model in production",\n            )\n        )\n    if not bool(cfg.get("require_model_match", True)):\n        issues.append(\n            (\n                "model_match_not_required",\n                "require_model_match must be true in production",\n            )\n        )\n    if not bool(cfg.get("require_model_version_match", False)):\n        issues.append(\n            (\n                "model_version_match_not_required",\n                "require_model_version_match must be true in production",\n            )\n        )\n    if bool(cfg.get("allow_empty_embedding_gallery", False)):\n        issues.append(\n            (\n                "empty_gallery_allowed",\n                "allow_empty_embedding_gallery must be false in production",\n            )\n        )\n    if not bool(cfg.get("reject_stale_embedding_gallery", False)):\n        issues.append(\n            (\n                "stale_gallery_allowed",\n                "reject_stale_embedding_gallery must be true in production",\n            )\n        )\n    try:\n        max_age = int(cfg.get("embedding_max_age_seconds", 0))\n    except (TypeError, ValueError):\n        max_age = 0\n    if max_age <= 0:\n        issues.append(\n            (\n                "gallery_max_age_invalid",\n                "embedding_max_age_seconds must be greater than zero in production",\n            )\n        )\n    return tuple(issues)\n\n\ndef strict_profile_issues(cfg):\n    if not production_enabled(cfg):\n        return ()\n\n    issues = list(gallery_policy_issues(cfg))\n    boolean_requirements = (\n        (\n            "model_manifest_incomplete_allowed",\n            "model_manifest_require_complete",\n            True,\n            "model_manifest_require_complete must be true in production",\n        ),\n        (\n            "model_integrity_startup_disabled",\n            "model_integrity_verify_on_start",\n            True,\n            "model_integrity_verify_on_start must be true in production",\n        ),\n        (\n            "pad_face_binding_unsafe",\n            "pad_require_single_face",\n            True,\n            "pad_require_single_face must be true until PAD is bound independently to every accepted face",\n        ),\n        (\n            "insecure_central_override_enabled",\n            "allow_insecure_central_url",\n            False,\n            "allow_insecure_central_url must be false in production",\n        ),\n        (\n            "unauthenticated_sync_override_enabled",\n            "allow_unauthenticated_embedding_sync",\n            False,\n            "allow_unauthenticated_embedding_sync must be false in production",\n        ),\n        (\n            "insecure_frappe_override_enabled",\n            "allow_insecure_frappe_url",\n            False,\n            "allow_insecure_frappe_url must be false in production",\n        ),\n        (\n            "insecure_pad_override_enabled",\n            "pad_allow_insecure_url",\n            False,\n            "pad_allow_insecure_url must be false in production",\n        ),\n        (\n            "unauthenticated_pad_override_enabled",\n            "pad_allow_unauthenticated_local",\n            False,\n            "pad_allow_unauthenticated_local must be false in production",\n        ),\n    )\n    for code, key, expected, message in boolean_requirements:\n        if bool(cfg.get(key, not expected)) is not expected:\n            issues.append((code, message))\n\n    if bool(cfg.get("embedding_sync_enabled", True)):\n        if not _text(cfg.get("central_url")):\n            issues.append(\n                (\n                    "central_url_missing",\n                    "central_url is required when embedding synchronization is enabled in production",\n                )\n            )\n        if is_placeholder(cfg.get("central_api_token")):\n            issues.append(\n                (\n                    "central_api_token_missing",\n                    "central_api_token must be a non-placeholder value in production",\n                )\n            )\n    return tuple(issues)\n\n\ndef effective_gallery_options(cfg):\n    issues = gallery_policy_issues(cfg)\n    if issues:\n        raise GalleryError(\n            "strict production gallery policy is invalid: "\n            + "; ".join(message for _, message in issues)\n        )\n\n    strict = production_enabled(cfg)\n    try:\n        max_employees = _positive_int(\n            cfg.get("max_gallery_employees"), "max_gallery_employees", 10000\n        )\n        max_embeddings = _positive_int(\n            cfg.get("max_embeddings_per_employee"),\n            "max_embeddings_per_employee",\n            50,\n        )\n    except GalleryError:\n        raise\n\n    return {\n        "expected_model": _text(cfg.get("model") or "buffalo_l"),\n        "expected_model_version": _text(cfg.get("model_version")),\n        "expected_branch": _text(cfg.get("branch_name")),\n        "require_model_match": True\n        if strict\n        else bool(cfg.get("require_model_match", True)),\n        "require_model_version_match": True\n        if strict\n        else bool(cfg.get("require_model_version_match", False)),\n        "allow_empty": False\n        if strict\n        else bool(cfg.get("allow_empty_embedding_gallery", False)),\n        "max_employees": max_employees,\n        "max_embeddings_per_employee": max_embeddings,\n    }\n\n\ndef gallery_freshness_status(cfg, updated_unix, *, path=""):\n    try:\n        updated_unix = float(updated_unix)\n    except (TypeError, ValueError) as exc:\n        raise GalleryError("embedding gallery has no valid activation timestamp") from exc\n\n    try:\n        max_age = int(cfg.get("embedding_max_age_seconds", 86400))\n    except (TypeError, ValueError) as exc:\n        raise GalleryError("embedding_max_age_seconds must be an integer") from exc\n    if max_age <= 0 and production_enabled(cfg):\n        raise GalleryError(\n            "embedding_max_age_seconds must be greater than zero in production"\n        )\n    age_seconds = max(0, int(time.time() - updated_unix))\n    stale = bool(max_age > 0 and age_seconds > max_age)\n    reject_stale = production_enabled(cfg) or bool(\n        cfg.get("reject_stale_embedding_gallery", False)\n    )\n    policy_valid = not (stale and reject_stale)\n    error = ""\n    if not policy_valid:\n        error = (\n            f"embedding gallery is stale: age={age_seconds}s max={max_age}s"\n        )\n    return {\n        "path": str(path or ""),\n        "updated_unix": updated_unix,\n        "updated_at": datetime.fromtimestamp(\n            updated_unix, timezone.utc\n        ).isoformat().replace("+00:00", "Z"),\n        "age_seconds": age_seconds,\n        "max_age_seconds": max_age,\n        "stale": stale,\n        "reject_stale": reject_stale,\n        "policy_valid": policy_valid,\n        "error": error,\n    }\n\n\ndef enforce_gallery_freshness(cfg, updated_unix, *, path=""):\n    status = gallery_freshness_status(cfg, updated_unix, path=path)\n    if not status["policy_valid"]:\n        raise GalleryError(status["error"])\n    return status\n\n\ndef inspect_gallery(cfg, path):\n    path = Path(path)\n    try:\n        options = effective_gallery_options(cfg)\n        _, metadata, _ = load_gallery(path, **options)\n        stat = path.stat()\n        freshness = gallery_freshness_status(cfg, stat.st_mtime, path=path)\n    except (GalleryError, OSError, ValueError) as exc:\n        return {\n            "available": False,\n            "policy_valid": False,\n            "path": str(path),\n            "error": str(exc),\n        }\n\n    return {\n        "available": True,\n        **freshness,\n        **metadata,\n    }\n\n\ndef load_runtime_gallery(cfg, path):\n    path = Path(path)\n    options = effective_gallery_options(cfg)\n    known, metadata, payload = load_gallery(path, **options)\n    stat = path.stat()\n    status = enforce_gallery_freshness(cfg, stat.st_mtime, path=path)\n    return known, metadata, payload, status\n')
write("model_runtime.py", 'from pathlib import Path\n\nfrom model_manifest import runtime_model_binding\n\n\nclass ModelRuntimeError(RuntimeError):\n    """Raised when InsightFace is not bound to the verified model directory."""\n\n\ndef create_face_analysis(\n    factory,\n    cfg,\n    app_root,\n    *,\n    det_size,\n    verified_model_directory=None,\n):\n    binding = runtime_model_binding(cfg, app_root)\n    expected_directory = Path(binding["model_directory"]).resolve()\n\n    if verified_model_directory:\n        verified_directory = Path(verified_model_directory).expanduser().resolve()\n        if verified_directory != expected_directory:\n            raise ModelRuntimeError(\n                "verified model directory does not match configured runtime directory: "\n                f"{verified_directory} != {expected_directory}"\n            )\n\n    if not expected_directory.is_dir():\n        raise ModelRuntimeError(\n            f"configured model directory is unavailable: {expected_directory}"\n        )\n\n    app = factory(\n        name=binding["model"],\n        root=binding["insightface_root"],\n        allowed_modules=cfg.get(\n            "allowed_modules", ["detection", "recognition"]\n        ),\n        providers=["CPUExecutionProvider"],\n    )\n    actual_value = getattr(app, "model_dir", "")\n    if not actual_value:\n        raise ModelRuntimeError(\n            "InsightFace did not expose the model directory it loaded"\n        )\n    actual_directory = Path(actual_value).expanduser().resolve()\n    if actual_directory != expected_directory:\n        raise ModelRuntimeError(\n            "InsightFace loaded an unexpected model directory: "\n            f"{actual_directory} != {expected_directory}"\n        )\n\n    size = int(det_size)\n    app.prepare(ctx_id=-1, det_size=(size, size))\n    return app\n')
write("web_security.py", 'import base64\nimport binascii\nimport hashlib\nimport hmac\nimport secrets\nfrom datetime import timedelta\nfrom functools import wraps\nfrom urllib.parse import urlsplit\n\nfrom flask import abort, redirect, request, session, url_for\n\n\nPASSWORD_SCHEME = "scrypt"\nDEFAULT_SCRYPT_N = 2**14\nDEFAULT_SCRYPT_R = 8\nDEFAULT_SCRYPT_P = 1\nMIN_SCRYPT_N = 2**14\nMAX_SCRYPT_N = 2**20\nMAX_SCRYPT_R = 32\nMAX_SCRYPT_P = 16\nMIN_SALT_BYTES = 16\nMAX_SALT_BYTES = 64\nMIN_DERIVED_BYTES = 32\nMAX_DERIVED_BYTES = 64\nPLACEHOLDERS = {"", "CHANGE_ME", "REPLACE_ME", "CHANGEME"}\n\n\ndef _b64encode(value):\n    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")\n\n\ndef _b64decode(value):\n    if not isinstance(value, str) or not value:\n        raise ValueError("encoded value is empty")\n    try:\n        raw = value.encode("ascii")\n    except UnicodeEncodeError as exc:\n        raise ValueError("encoded value must be ASCII") from exc\n    padding = b"=" * (-len(raw) % 4)\n    try:\n        return base64.b64decode(raw + padding, altchars=b"-_", validate=True)\n    except (binascii.Error, ValueError) as exc:\n        raise ValueError("encoded value is not valid URL-safe base64") from exc\n\n\ndef is_placeholder(value):\n    return str(value or "").strip().upper() in PLACEHOLDERS\n\n\ndef _validate_scrypt_parameters(n, r, p):\n    try:\n        n = int(n)\n        r = int(r)\n        p = int(p)\n    except (TypeError, ValueError) as exc:\n        raise ValueError("scrypt parameters must be integers") from exc\n    if n < MIN_SCRYPT_N or n > MAX_SCRYPT_N or n & (n - 1):\n        raise ValueError(\n            f"scrypt n must be a power of two from {MIN_SCRYPT_N} through {MAX_SCRYPT_N}"\n        )\n    if r < 1 or r > MAX_SCRYPT_R:\n        raise ValueError(f"scrypt r must be from 1 through {MAX_SCRYPT_R}")\n    if p < 1 or p > MAX_SCRYPT_P:\n        raise ValueError(f"scrypt p must be from 1 through {MAX_SCRYPT_P}")\n    return n, r, p\n\n\ndef parse_password_hash(encoded):\n    parts = str(encoded or "").split("$")\n    if len(parts) != 6:\n        raise ValueError("password hash must contain six '$'-separated fields")\n    scheme, n_value, r_value, p_value, salt_value, expected_value = parts\n    if scheme != PASSWORD_SCHEME:\n        raise ValueError(f"password hash scheme must be {PASSWORD_SCHEME}")\n    n, r, p = _validate_scrypt_parameters(n_value, r_value, p_value)\n    salt = _b64decode(salt_value)\n    expected = _b64decode(expected_value)\n    if not MIN_SALT_BYTES <= len(salt) <= MAX_SALT_BYTES:\n        raise ValueError(\n            f"password hash salt must be {MIN_SALT_BYTES}-{MAX_SALT_BYTES} bytes"\n        )\n    if not MIN_DERIVED_BYTES <= len(expected) <= MAX_DERIVED_BYTES:\n        raise ValueError(\n            "password hash derived value must be "\n            f"{MIN_DERIVED_BYTES}-{MAX_DERIVED_BYTES} bytes"\n        )\n    return {\n        "n": n,\n        "r": r,\n        "p": p,\n        "salt": salt,\n        "expected": expected,\n    }\n\n\ndef password_hash_issues(encoded):\n    try:\n        parse_password_hash(encoded)\n        return ()\n    except ValueError as exc:\n        return (str(exc),)\n\n\ndef hash_password(\n    password,\n    *,\n    n=DEFAULT_SCRYPT_N,\n    r=DEFAULT_SCRYPT_R,\n    p=DEFAULT_SCRYPT_P,\n):\n    if not isinstance(password, str):\n        raise TypeError("password must be text")\n    if len(password) < 12:\n        raise ValueError("admin password must contain at least 12 characters")\n    n, r, p = _validate_scrypt_parameters(n, r, p)\n    salt = secrets.token_bytes(MIN_SALT_BYTES)\n    derived = hashlib.scrypt(\n        password.encode("utf-8"),\n        salt=salt,\n        n=n,\n        r=r,\n        p=p,\n        maxmem=128 * 1024 * 1024,\n        dklen=MIN_DERIVED_BYTES,\n    )\n    return "$".join(\n        (\n            PASSWORD_SCHEME,\n            str(n),\n            str(r),\n            str(p),\n            _b64encode(salt),\n            _b64encode(derived),\n        )\n    )\n\n\ndef verify_password(password, encoded):\n    try:\n        parsed = parse_password_hash(encoded)\n        actual = hashlib.scrypt(\n            str(password).encode("utf-8"),\n            salt=parsed["salt"],\n            n=parsed["n"],\n            r=parsed["r"],\n            p=parsed["p"],\n            maxmem=128 * 1024 * 1024,\n            dklen=len(parsed["expected"]),\n        )\n        return hmac.compare_digest(actual, parsed["expected"])\n    except (TypeError, ValueError, OverflowError, MemoryError):\n        return False\n\n\ndef auth_configuration_issues(cfg):\n    issues = []\n    username = str(cfg.get("web_admin_username") or "").strip()\n    password_hash = str(cfg.get("web_admin_password_hash") or "").strip()\n    session_secret = str(cfg.get("web_session_secret") or "").strip()\n    if not username:\n        issues.append("web_admin_username is not configured")\n    issues.extend(password_hash_issues(password_hash))\n    if len(session_secret) < 32 or is_placeholder(session_secret):\n        issues.append(\n            "web_session_secret must be a persistent non-placeholder value of at least 32 characters"\n        )\n    return tuple(issues)\n\n\ndef auth_configured(cfg):\n    return not auth_configuration_issues(cfg)\n\n\ndef configure_app(app, cfg):\n    configured = auth_configured(cfg)\n    secret = str(cfg.get("web_session_secret") or "").strip()\n    if not configured:\n        secret = secrets.token_urlsafe(48)\n    minutes = max(5, int(cfg.get("web_session_minutes", 30)))\n    app.config.update(\n        SECRET_KEY=secret,\n        AUTH_CONFIGURED=configured,\n        SESSION_COOKIE_NAME="face_attendance_admin",\n        SESSION_COOKIE_HTTPONLY=True,\n        SESSION_COOKIE_SECURE=bool(cfg.get("web_cookie_secure", True)),\n        SESSION_COOKIE_SAMESITE="Lax",\n        PERMANENT_SESSION_LIFETIME=timedelta(minutes=minutes),\n        MAX_CONTENT_LENGTH=int(\n            cfg.get("web_max_request_bytes", 64 * 1024 * 1024)\n        ),\n    )\n    return configured\n\n\ndef csrf_token():\n    value = session.get("csrf_token")\n    if not value:\n        value = secrets.token_urlsafe(32)\n        session["csrf_token"] = value\n    return value\n\n\ndef validate_csrf():\n    expected = str(session.get("csrf_token") or "")\n    supplied = str(\n        request.form.get("csrf_token")\n        or request.headers.get("X-CSRF-Token")\n        or ""\n    )\n    if (\n        not expected\n        or not supplied\n        or not hmac.compare_digest(expected, supplied)\n    ):\n        abort(400, description="invalid CSRF token")\n\n\ndef admin_user():\n    return str(session.get("admin_user") or "")\n\n\ndef login_required(view):\n    @wraps(view)\n    def wrapped(*args, **kwargs):\n        if not session.get("admin_authenticated"):\n            return redirect(\n                url_for(\n                    "login", next=request.full_path.rstrip("?")\n                )\n            )\n        return view(*args, **kwargs)\n\n    return wrapped\n\n\ndef csrf_protected(view):\n    @wraps(view)\n    def wrapped(*args, **kwargs):\n        validate_csrf()\n        return view(*args, **kwargs)\n\n    return wrapped\n\n\ndef safe_next_url(value, fallback="/"):\n    value = str(value or "").strip()\n    parsed = urlsplit(value)\n    if (\n        not value\n        or parsed.scheme\n        or parsed.netloc\n        or not value.startswith("/")\n        or value.startswith("//")\n    ):\n        return fallback\n    return value\n\n\ndef remote_address():\n    return str(request.remote_addr or "unknown")\n\n\ndef add_security_headers(response, cfg):\n    response.headers.setdefault("X-Content-Type-Options", "nosniff")\n    response.headers.setdefault("X-Frame-Options", "DENY")\n    response.headers.setdefault("Referrer-Policy", "no-referrer")\n    response.headers.setdefault(\n        "Permissions-Policy",\n        "camera=(), microphone=(), geolocation=(), payment=()",\n    )\n    response.headers.setdefault(\n        "Content-Security-Policy",\n        "default-src 'self'; style-src 'self' 'unsafe-inline'; "\n        "img-src 'self' data:; form-action 'self'; frame-ancestors 'none'; "\n        "base-uri 'none'; object-src 'none'",\n    )\n    if bool(cfg.get("web_hsts_enabled", True)):\n        response.headers.setdefault(\n            "Strict-Transport-Security",\n            "max-age=31536000; includeSubDomains",\n        )\n    if request.path.startswith("/api/") or request.path in {"/", "/login"}:\n        response.headers.setdefault("Cache-Control", "no-store")\n    return response\n')
write("production_readiness.py", 'import argparse\nimport json\nfrom dataclasses import asdict, dataclass\nfrom pathlib import Path\nfrom urllib.parse import urlparse\n\nfrom model_manifest import (\n    is_placeholder,\n    resolve_path,\n    runtime_model_binding,\n    verify_manifest,\n)\nfrom pad import configuration_issues as pad_configuration_issues\nfrom runtime_policy import inspect_gallery, strict_profile_issues\nfrom web_security import auth_configuration_issues\n\n\nFTP_UPLOAD_ONLY_PERMISSIONS = frozenset("elw")\n\n\n@dataclass(frozen=True)\nclass ReadinessIssue:\n    code: str\n    message: str\n    severity: str = "blocker"\n\n\n@dataclass(frozen=True)\nclass ReadinessReport:\n    production_mode: bool\n    ready: bool\n    issues: tuple[ReadinessIssue, ...]\n    model_integrity: dict\n    gallery: dict\n\n    @property\n    def blockers(self):\n        return tuple(\n            issue for issue in self.issues if issue.severity == "blocker"\n        )\n\n    def to_dict(self):\n        return {\n            "production_mode": self.production_mode,\n            "ready": self.ready,\n            "issues": [asdict(issue) for issue in self.issues],\n            "model_integrity": self.model_integrity,\n            "gallery": self.gallery,\n        }\n\n\nclass ProductionReadinessError(RuntimeError):\n    def __init__(self, report):\n        self.report = report\n        super().__init__(format_report(report))\n\n\ndef _text(value):\n    return str(value or "").strip()\n\n\ndef _is_local_url(value):\n    parsed = urlparse(_text(value))\n    return parsed.hostname in {"localhost", "127.0.0.1", "::1"}\n\n\ndef _https_issue(cfg, key, allow_key, code, label):\n    value = _text(cfg.get(key))\n    if not value:\n        return None\n    parsed = urlparse(value)\n    if parsed.scheme not in {"http", "https"} or not parsed.netloc:\n        return ReadinessIssue(\n            code, f"{label} must be an absolute HTTP(S) URL"\n        )\n    if (\n        parsed.scheme != "https"\n        and not _is_local_url(value)\n        and not bool(cfg.get(allow_key, False))\n    ):\n        return ReadinessIssue(\n            code, f"{label} must use HTTPS outside localhost"\n        )\n    return None\n\n\ndef _ftp_permission_issues(cfg):\n    default_permissions = _text(cfg.get("ftp_permissions") or "elw")\n    configured_users = cfg.get("ftp_users")\n    rows = []\n    issues = []\n\n    if configured_users in (None, {}):\n        rows.append(("default FTP user", default_permissions))\n    elif not isinstance(configured_users, dict):\n        return ["ftp_users must be a JSON object"]\n    else:\n        for username, item in configured_users.items():\n            label = f"FTP user {_text(username) or '<empty>'}"\n            if not isinstance(item, dict):\n                issues.append(f"{label} configuration must be an object")\n                continue\n            rows.append(\n                (\n                    label,\n                    _text(item.get("permissions") or default_permissions),\n                )\n            )\n\n    for label, permissions in rows:\n        if "w" not in permissions:\n            issues.append(\n                f"{label} must include the upload permission 'w'"\n            )\n        unsupported = sorted(\n            set(permissions) - FTP_UPLOAD_ONLY_PERMISSIONS\n        )\n        if unsupported:\n            issues.append(\n                f"{label} grants non-upload permissions: "\n                f"{''.join(unsupported)}; only e, l, and w are allowed"\n            )\n    return issues\n\n\ndef check_production_readiness(\n    cfg,\n    root,\n    *,\n    verify_model_files=True,\n    gallery_path=None,\n):\n    root = Path(root)\n    production_mode = bool(cfg.get("production_mode", False))\n    issues = []\n\n    if not production_mode:\n        issues.append(\n            ReadinessIssue(\n                "production_mode_disabled",\n                "production_mode is false; live production safeguards are advisory only",\n                severity="warning",\n            )\n        )\n\n    for code, message in strict_profile_issues(cfg):\n        issues.append(ReadinessIssue(code, message))\n\n    if not bool(cfg.get("model_license_acknowledged", False)):\n        issues.append(\n            ReadinessIssue(\n                "model_license_not_acknowledged",\n                "model_license_acknowledged must be true after the exact model license is verified",\n            )\n        )\n    license_reference = _text(cfg.get("model_license_reference"))\n    if is_placeholder(license_reference):\n        issues.append(\n            ReadinessIssue(\n                "model_license_reference_missing",\n                "model_license_reference must identify the recorded license or internal approval",\n            )\n        )\n\n    try:\n        binding = runtime_model_binding(cfg, root)\n    except ValueError as exc:\n        binding = {}\n        issues.append(\n            ReadinessIssue("model_runtime_binding_invalid", str(exc))\n        )\n\n    model_manifest_path = resolve_path(\n        root, cfg.get("model_manifest_path"), "model_manifest.json"\n    )\n    integrity = verify_manifest(\n        model_manifest_path,\n        expected_model=cfg.get("model"),\n        expected_model_version=cfg.get("model_version"),\n        expected_model_directory=binding.get("model_directory"),\n        expected_license_reference=license_reference or None,\n        require_complete=bool(\n            cfg.get("model_manifest_require_complete", True)\n        ),\n        verify_files=verify_model_files,\n    )\n    if binding:\n        integrity.setdefault(\n            "configured_model_directory", binding["model_directory"]\n        )\n        integrity.setdefault(\n            "configured_insightface_root", binding["insightface_root"]\n        )\n    for message in integrity.get("errors", []):\n        issues.append(ReadinessIssue("model_integrity_failed", message))\n    for message in integrity.get("warnings", []):\n        issues.append(\n            ReadinessIssue(\n                "model_integrity_warning",\n                message,\n                severity="warning",\n            )\n        )\n\n    gallery_path = (\n        Path(gallery_path)\n        if gallery_path is not None\n        else root / "embedding_gallery.json"\n    )\n    gallery = inspect_gallery(cfg, gallery_path)\n    if not gallery.get("available"):\n        issues.append(\n            ReadinessIssue(\n                "embedding_gallery_invalid",\n                gallery.get("error") or "embedding gallery is unavailable",\n            )\n        )\n    elif not gallery.get("policy_valid", False):\n        issues.append(\n            ReadinessIssue(\n                "embedding_gallery_policy_failed",\n                gallery.get("error")\n                or "embedding gallery does not satisfy runtime policy",\n            )\n        )\n\n    for message in pad_configuration_issues(cfg):\n        issues.append(\n            ReadinessIssue("pad_configuration_invalid", message)\n        )\n    if not bool(cfg.get("pad_required", False)):\n        issues.append(\n            ReadinessIssue(\n                "pad_not_required",\n                "pad_required must be true for production facial recognition",\n            )\n        )\n    if _text(cfg.get("pad_provider") or "disabled").lower() == "disabled":\n        issues.append(\n            ReadinessIssue(\n                "pad_provider_disabled",\n                "a validated PAD/liveness provider must be configured",\n            )\n        )\n    if not bool(cfg.get("pad_fail_closed", True)):\n        issues.append(\n            ReadinessIssue(\n                "pad_not_fail_closed",\n                "pad_fail_closed must be true in production",\n            )\n        )\n\n    for message in auth_configuration_issues(cfg):\n        issues.append(\n            ReadinessIssue("web_admin_auth_invalid", message)\n        )\n    if _text(cfg.get("web_bind_host", "127.0.0.1")) not in {\n        "127.0.0.1",\n        "::1",\n        "localhost",\n    }:\n        issues.append(\n            ReadinessIssue(\n                "web_not_loopback",\n                "web_bind_host must be loopback behind the HTTPS reverse proxy",\n            )\n        )\n    if not bool(cfg.get("web_cookie_secure", True)):\n        issues.append(\n            ReadinessIssue(\n                "web_cookie_insecure",\n                "web_cookie_secure must be true",\n            )\n        )\n    if not bool(cfg.get("web_hsts_enabled", True)):\n        issues.append(\n            ReadinessIssue(\n                "web_hsts_disabled",\n                "web_hsts_enabled must be true",\n            )\n        )\n    if not bool(cfg.get("https_reverse_proxy_acknowledged", False)):\n        issues.append(\n            ReadinessIssue(\n                "https_proxy_not_acknowledged",\n                "set https_reverse_proxy_acknowledged after HTTPS proxy deployment is verified",\n            )\n        )\n\n    central_issue = _https_issue(\n        cfg,\n        "central_url",\n        "allow_insecure_central_url",\n        "central_url_insecure",\n        "central_url",\n    )\n    if central_issue:\n        issues.append(central_issue)\n    frappe_issue = _https_issue(\n        cfg,\n        "frappe_url",\n        "allow_insecure_frappe_url",\n        "frappe_url_insecure",\n        "frappe_url",\n    )\n    if frappe_issue:\n        issues.append(frappe_issue)\n\n    ftp_tls_enabled = bool(cfg.get("ftp_tls_enabled", False))\n    network_ack = bool(\n        cfg.get("camera_network_isolated_acknowledged", False)\n    )\n    if not ftp_tls_enabled and not network_ack:\n        issues.append(\n            ReadinessIssue(\n                "camera_transport_unprotected",\n                "enable FTPS or acknowledge a verified isolated camera VLAN/VPN",\n            )\n        )\n    if ftp_tls_enabled:\n        cert = resolve_path(root, cfg.get("ftp_tls_certfile"), "")\n        key = resolve_path(root, cfg.get("ftp_tls_keyfile"), "")\n        if (\n            not _text(cfg.get("ftp_tls_certfile"))\n            or not cert.is_file()\n        ):\n            issues.append(\n                ReadinessIssue(\n                    "ftp_tls_cert_missing",\n                    f"FTPS certificate unavailable: {cert}",\n                )\n            )\n        if (\n            not _text(cfg.get("ftp_tls_keyfile"))\n            or not key.is_file()\n        ):\n            issues.append(\n                ReadinessIssue(\n                    "ftp_tls_key_missing",\n                    f"FTPS private key unavailable: {key}",\n                )\n            )\n        if not bool(cfg.get("ftp_tls_control_required", True)):\n            issues.append(\n                ReadinessIssue(\n                    "ftp_tls_control_optional",\n                    "ftp_tls_control_required must be true in production",\n                )\n            )\n        if not bool(cfg.get("ftp_tls_data_required", True)):\n            issues.append(\n                ReadinessIssue(\n                    "ftp_tls_data_optional",\n                    "ftp_tls_data_required must be true in production",\n                )\n            )\n\n    if not bool(cfg.get("ftp_staging_enabled", True)):\n        issues.append(\n            ReadinessIssue(\n                "ftp_staging_disabled",\n                "ftp_staging_enabled must be true so the watcher cannot observe partial uploads",\n            )\n        )\n    for message in _ftp_permission_issues(cfg):\n        issues.append(ReadinessIssue("ftp_permissions_unsafe", message))\n\n    camera_ids = (\n        cfg.get("camera_ids")\n        if isinstance(cfg.get("camera_ids"), dict)\n        else {}\n    )\n    in_id = _text(camera_ids.get("in"))\n    out_id = _text(camera_ids.get("out"))\n    if not in_id or not out_id:\n        issues.append(\n            ReadinessIssue(\n                "camera_ids_missing",\n                "stable and explicit camera_ids.in and camera_ids.out are required",\n            )\n        )\n    elif in_id == out_id:\n        issues.append(\n            ReadinessIssue(\n                "camera_ids_duplicate",\n                "IN and OUT cameras must not use the same camera ID",\n            )\n        )\n\n    if bool(cfg.get("embedding_export_enabled", False)) and is_placeholder(\n        cfg.get("embedding_export_token")\n    ):\n        issues.append(\n            ReadinessIssue(\n                "embedding_export_token_missing",\n                "embedding_export_enabled requires a non-placeholder token",\n            )\n        )\n\n    blockers = [\n        issue for issue in issues if issue.severity == "blocker"\n    ]\n    return ReadinessReport(\n        production_mode=production_mode,\n        ready=not blockers,\n        issues=tuple(issues),\n        model_integrity=integrity,\n        gallery=gallery,\n    )\n\n\ndef enforce_production_readiness(\n    cfg,\n    root,\n    *,\n    dry_run=False,\n    verify_model_files=True,\n    gallery_path=None,\n):\n    report = check_production_readiness(\n        cfg,\n        root,\n        verify_model_files=verify_model_files,\n        gallery_path=gallery_path,\n    )\n    if (\n        bool(cfg.get("production_mode", False))\n        and not dry_run\n        and report.blockers\n    ):\n        raise ProductionReadinessError(report)\n    return report\n\n\ndef format_report(report):\n    lines = [\n        "production_mode="\n        f"{str(report.production_mode).lower()} "\n        f"ready={str(report.ready).lower()}"\n    ]\n    for issue in report.issues:\n        lines.append(\n            f"[{issue.severity}] {issue.code}: {issue.message}"\n        )\n    return "\\n".join(lines)\n\n\ndef load_config(path):\n    try:\n        data = json.loads(Path(path).read_text(encoding="utf-8"))\n    except FileNotFoundError as exc:\n        raise SystemExit(f"missing config: {path}") from exc\n    except json.JSONDecodeError as exc:\n        raise SystemExit(f"invalid JSON in {path}: {exc}") from exc\n    if not isinstance(data, dict):\n        raise SystemExit("config must contain a JSON object")\n    return data\n\n\ndef main():\n    parser = argparse.ArgumentParser(\n        description="Check Face Attendance production readiness."\n    )\n    parser.add_argument(\n        "--config",\n        type=Path,\n        default=Path(__file__).resolve().parent / "config.json",\n    )\n    parser.add_argument("--json", action="store_true")\n    parser.add_argument(\n        "--skip-model-hash",\n        action="store_true",\n        help="Validate manifest metadata and file inventory without hashing model files.",\n    )\n    parser.add_argument(\n        "--strict",\n        action="store_true",\n        help="Exit non-zero on blockers even when production_mode is false.",\n    )\n    args = parser.parse_args()\n    cfg = load_config(args.config)\n    report = check_production_readiness(\n        cfg,\n        args.config.resolve().parent,\n        verify_model_files=not args.skip_model_hash,\n    )\n    print(\n        json.dumps(report.to_dict(), ensure_ascii=False, indent=2)\n        if args.json\n        else format_report(report)\n    )\n    if report.blockers and (\n        args.strict or bool(cfg.get("production_mode", False))\n    ):\n        raise SystemExit(1)\n\n\nif __name__ == "__main__":\n    main()\n')
write("model_manifest.py", 'import argparse\nimport hashlib\nimport json\nimport os\nimport tempfile\nfrom datetime import datetime, timezone\nfrom pathlib import Path\n\n\nSCHEMA_VERSION = 1\nPLACEHOLDERS = {"", "CHANGE_ME", "REPLACE_ME", "CHANGEME", "TODO"}\n\n\ndef utc_now():\n    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")\n\n\ndef is_placeholder(value):\n    return str(value or "").strip().upper() in PLACEHOLDERS\n\n\ndef resolve_path(root, value, default):\n    path = Path(value or default).expanduser()\n    return path if path.is_absolute() else Path(root) / path\n\n\ndef default_model_directory(cfg, root=None):\n    model = str(cfg.get("model") or "buffalo_l").strip()\n    configured = str(cfg.get("model_directory") or "").strip()\n    if configured:\n        path = Path(configured).expanduser()\n        if not path.is_absolute() and root is not None:\n            path = Path(root) / path\n        return path\n    return Path.home() / ".insightface" / "models" / model\n\n\ndef insightface_root_for_model_directory(model_directory, model):\n    model = str(model or "").strip()\n    if is_placeholder(model):\n        raise ValueError(\n            "model must be a non-placeholder value before binding InsightFace"\n        )\n    configured = Path(model_directory).expanduser()\n    if configured.is_symlink():\n        raise ValueError("model_directory must not be a symbolic link")\n    directory = configured.resolve()\n    if directory.name != model or directory.parent.name != "models":\n        raise ValueError(\n            "model_directory must use InsightFace's root/models/<model> layout; "\n            f"received {directory} for model {model!r}"\n        )\n    return directory.parent.parent\n\n\ndef runtime_model_binding(cfg, root=None):\n    model = str(cfg.get("model") or "").strip()\n    if is_placeholder(model):\n        raise ValueError("model must be a non-placeholder value")\n    model_directory = default_model_directory(cfg, root).expanduser().resolve()\n    insightface_root = insightface_root_for_model_directory(\n        model_directory, model\n    )\n    return {\n        "model": model,\n        "model_version": str(cfg.get("model_version") or "").strip(),\n        "model_directory": str(model_directory),\n        "insightface_root": str(insightface_root),\n    }\n\n\ndef sha256_file(path, chunk_size=1024 * 1024):\n    digest = hashlib.sha256()\n    size = 0\n    with Path(path).open("rb") as handle:\n        while True:\n            chunk = handle.read(chunk_size)\n            if not chunk:\n                break\n            size += len(chunk)\n            digest.update(chunk)\n    return digest.hexdigest(), size\n\n\ndef _model_files(model_directory):\n    model_directory = Path(model_directory).expanduser().resolve()\n    files = []\n    for path in sorted(model_directory.rglob("*")):\n        if (\n            not path.is_file()\n            or path.is_symlink()\n            or path.name.startswith(".")\n        ):\n            continue\n        relative = path.relative_to(model_directory).as_posix()\n        digest, size = sha256_file(path)\n        files.append(\n            {"path": relative, "sha256": digest, "size": size}\n        )\n    if not files:\n        raise ValueError(\n            f"no model files found under {model_directory}"\n        )\n    return files\n\n\ndef build_manifest(\n    *,\n    model_directory,\n    model,\n    model_version="",\n    license_reference="",\n):\n    model = str(model or "").strip()\n    if is_placeholder(model):\n        raise ValueError("a non-placeholder model name is required")\n    model_directory = Path(model_directory).expanduser().resolve()\n    insightface_root_for_model_directory(model_directory, model)\n    reference = str(license_reference or "").strip()\n    if is_placeholder(reference):\n        raise ValueError(\n            "a non-placeholder license reference is required"\n        )\n    return {\n        "schema_version": SCHEMA_VERSION,\n        "generated_at": utc_now(),\n        "model": model,\n        "model_version": str(model_version or "").strip(),\n        "license_reference": reference,\n        "model_directory": str(model_directory),\n        "files": _model_files(model_directory),\n    }\n\n\ndef write_manifest_atomic(path, manifest):\n    path = Path(path)\n    path.parent.mkdir(parents=True, exist_ok=True)\n    fd, temp_name = tempfile.mkstemp(\n        prefix=f".{path.name}.", dir=path.parent\n    )\n    temp_path = Path(temp_name)\n    try:\n        with os.fdopen(fd, "w", encoding="utf-8") as handle:\n            json.dump(\n                manifest, handle, ensure_ascii=False, indent=2\n            )\n            handle.write("\\n")\n            handle.flush()\n            os.fsync(handle.fileno())\n        os.chmod(temp_path, 0o600)\n        os.replace(temp_path, path)\n        os.chmod(path, 0o600)\n    finally:\n        try:\n            temp_path.unlink()\n        except FileNotFoundError:\n            pass\n\n\ndef verify_manifest(\n    manifest_path,\n    *,\n    expected_model=None,\n    expected_model_version=None,\n    expected_model_directory=None,\n    expected_license_reference=None,\n    require_complete=True,\n    verify_files=True,\n):\n    manifest_path = Path(manifest_path)\n    errors = []\n    warnings = []\n    try:\n        manifest = json.loads(\n            manifest_path.read_text(encoding="utf-8")\n        )\n    except FileNotFoundError:\n        return {\n            "ok": False,\n            "errors": [\n                f"model manifest not found: {manifest_path}"\n            ],\n            "warnings": [],\n            "manifest_path": str(manifest_path),\n            "hashes_verified": bool(verify_files),\n        }\n    except (OSError, json.JSONDecodeError) as exc:\n        return {\n            "ok": False,\n            "errors": [\n                f"could not read model manifest {manifest_path}: {exc}"\n            ],\n            "warnings": [],\n            "manifest_path": str(manifest_path),\n            "hashes_verified": bool(verify_files),\n        }\n\n    if not isinstance(manifest, dict):\n        return {\n            "ok": False,\n            "errors": [\n                "model manifest must be a JSON object"\n            ],\n            "warnings": [],\n            "manifest_path": str(manifest_path),\n            "hashes_verified": bool(verify_files),\n        }\n\n    if manifest.get("schema_version") != SCHEMA_VERSION:\n        errors.append(\n            "unsupported model manifest schema "\n            f"{manifest.get('schema_version')!r}; "\n            f"expected {SCHEMA_VERSION}"\n        )\n\n    model = str(manifest.get("model") or "").strip()\n    version = str(\n        manifest.get("model_version") or ""\n    ).strip()\n    reference = str(\n        manifest.get("license_reference") or ""\n    ).strip()\n    if is_placeholder(model):\n        errors.append(\n            "model manifest has no usable model name"\n        )\n    if expected_model and model != str(expected_model).strip():\n        errors.append(\n            f"model manifest names {model!r}; "\n            f"expected {expected_model!r}"\n        )\n    if (\n        expected_model_version is not None\n        and version\n        != str(expected_model_version or "").strip()\n    ):\n        errors.append(\n            f"model manifest version {version!r}; expected "\n            f"{str(expected_model_version or '').strip()!r}"\n        )\n    if is_placeholder(reference):\n        errors.append(\n            "model manifest has no usable license_reference"\n        )\n    if (\n        expected_license_reference\n        and reference != str(expected_license_reference).strip()\n    ):\n        errors.append(\n            "model manifest license_reference does not match config"\n        )\n\n    directory_value = str(\n        manifest.get("model_directory") or ""\n    ).strip()\n    insightface_root = ""\n    if not directory_value:\n        errors.append(\n            "model manifest has no model_directory"\n        )\n        model_directory = None\n    else:\n        configured_directory = Path(\n            directory_value\n        ).expanduser()\n        if configured_directory.is_symlink():\n            errors.append(\n                "model manifest directory must not be a symbolic link"\n            )\n        model_directory = configured_directory.resolve()\n        if expected_model_directory:\n            expected = Path(\n                expected_model_directory\n            ).expanduser().resolve()\n            if model_directory != expected:\n                errors.append(\n                    "model manifest directory "\n                    f"{model_directory} does not match "\n                    f"configured directory {expected}"\n                )\n        try:\n            insightface_root = str(\n                insightface_root_for_model_directory(\n                    model_directory, model\n                )\n            )\n        except ValueError as exc:\n            errors.append(str(exc))\n        if (\n            not model_directory.exists()\n            or not model_directory.is_dir()\n        ):\n            errors.append(\n                f"model directory is unavailable: {model_directory}"\n            )\n\n    entries = manifest.get("files")\n    if not isinstance(entries, list) or not entries:\n        errors.append(\n            "model manifest has no files"\n        )\n        entries = []\n\n    listed = set()\n    verified_count = 0\n    if model_directory and model_directory.is_dir():\n        for index, item in enumerate(entries):\n            if not isinstance(item, dict):\n                errors.append(\n                    f"files[{index}] must be an object"\n                )\n                continue\n            relative = (\n                str(item.get("path") or "")\n                .strip()\n                .replace("\\\\", "/")\n            )\n            if (\n                not relative\n                or relative.startswith("/")\n                or ".." in Path(relative).parts\n            ):\n                errors.append(\n                    f"files[{index}] has an unsafe path"\n                )\n                continue\n            if relative in listed:\n                errors.append(\n                    "duplicate model file in manifest: "\n                    f"{relative}"\n                )\n                continue\n            listed.add(relative)\n            expected_hash = (\n                str(item.get("sha256") or "")\n                .strip()\n                .lower()\n            )\n            try:\n                expected_size = int(item.get("size"))\n            except (TypeError, ValueError):\n                errors.append(\n                    f"invalid size for model file {relative}"\n                )\n                continue\n            if expected_size < 0:\n                errors.append(\n                    f"invalid size for model file {relative}"\n                )\n                continue\n            if (\n                len(expected_hash) != 64\n                or any(\n                    char not in "0123456789abcdef"\n                    for char in expected_hash\n                )\n            ):\n                errors.append(\n                    f"invalid SHA-256 for model file {relative}"\n                )\n                continue\n            path = (\n                model_directory / relative\n            ).resolve()\n            try:\n                path.relative_to(model_directory)\n            except ValueError:\n                errors.append(\n                    "model file escapes model directory: "\n                    f"{relative}"\n                )\n                continue\n            if not path.is_file() or path.is_symlink():\n                errors.append(\n                    f"model file missing or unsafe: {relative}"\n                )\n                continue\n            if verify_files:\n                actual_hash, actual_size = sha256_file(path)\n                if actual_size != expected_size:\n                    errors.append(\n                        "model file size mismatch for "\n                        f"{relative}: {actual_size} != "\n                        f"{expected_size}"\n                    )\n                if actual_hash != expected_hash:\n                    errors.append(\n                        "model file SHA-256 mismatch for "\n                        f"{relative}"\n                    )\n                if (\n                    actual_size == expected_size\n                    and actual_hash == expected_hash\n                ):\n                    verified_count += 1\n            elif path.stat().st_size != expected_size:\n                errors.append(\n                    "model file size mismatch for "\n                    f"{relative}: {path.stat().st_size} != "\n                    f"{expected_size}"\n                )\n\n        if require_complete:\n            actual = {\n                path.relative_to(\n                    model_directory\n                ).as_posix()\n                for path in model_directory.rglob("*")\n                if path.is_file()\n                and not path.is_symlink()\n                and not path.name.startswith(".")\n            }\n            unlisted = sorted(actual - listed)\n            missing = sorted(listed - actual)\n            if unlisted:\n                errors.append(\n                    "unlisted model files are present: "\n                    + ", ".join(unlisted[:10])\n                )\n            if missing:\n                errors.append(\n                    "listed model files are absent: "\n                    + ", ".join(missing[:10])\n                )\n\n    if entries and not any(\n        str(item.get("path", "")).lower().endswith(".onnx")\n        for item in entries\n        if isinstance(item, dict)\n    ):\n        warnings.append(\n            "model manifest contains no ONNX files"\n        )\n\n    return {\n        "ok": not errors,\n        "errors": errors,\n        "warnings": warnings,\n        "manifest_path": str(manifest_path),\n        "model": model,\n        "model_version": version,\n        "license_reference": reference,\n        "model_directory": (\n            str(model_directory)\n            if model_directory\n            else ""\n        ),\n        "insightface_root": insightface_root,\n        "file_count": len(entries),\n        "verified_file_count": verified_count,\n        "hashes_verified": bool(verify_files),\n    }\n\n\ndef load_config(path):\n    try:\n        data = json.loads(\n            Path(path).read_text(encoding="utf-8")\n        )\n    except FileNotFoundError as exc:\n        raise SystemExit(\n            f"missing config: {path}"\n        ) from exc\n    except json.JSONDecodeError as exc:\n        raise SystemExit(\n            f"invalid JSON in {path}: {exc}"\n        ) from exc\n    if not isinstance(data, dict):\n        raise SystemExit(\n            "config must contain a JSON object"\n        )\n    return data\n\n\ndef main():\n    parser = argparse.ArgumentParser(\n        description="Create or verify a pinned face-model manifest."\n    )\n    parser.add_argument(\n        "--config",\n        type=Path,\n        default=Path(__file__).resolve().parent\n        / "config.json",\n    )\n    sub = parser.add_subparsers(\n        dest="command", required=True\n    )\n\n    create = sub.add_parser("create")\n    create.add_argument("--model-dir", type=Path)\n    create.add_argument("--output", type=Path)\n    create.add_argument("--license-reference")\n\n    verify = sub.add_parser("verify")\n    verify.add_argument("--manifest", type=Path)\n    verify.add_argument(\n        "--skip-hash",\n        action="store_true",\n        help="Validate metadata, paths, file sizes, and inventory without hashing files.",\n    )\n    args = parser.parse_args()\n    cfg = load_config(args.config)\n    root = args.config.resolve().parent\n    model_dir = (\n        args.model_dir\n        if args.command == "create"\n        and args.model_dir\n        else default_model_directory(cfg, root)\n    )\n    manifest_path = (\n        args.output\n        if args.command == "create"\n        and args.output\n        else args.manifest\n        if args.command == "verify"\n        and args.manifest\n        else resolve_path(\n            root,\n            cfg.get("model_manifest_path"),\n            "model_manifest.json",\n        )\n    )\n\n    if args.command == "create":\n        reference = (\n            args.license_reference\n            or cfg.get("model_license_reference")\n        )\n        try:\n            manifest = build_manifest(\n                model_directory=model_dir,\n                model=cfg.get("model"),\n                model_version=cfg.get(\n                    "model_version", ""\n                ),\n                license_reference=reference,\n            )\n        except ValueError as exc:\n            raise SystemExit(str(exc)) from exc\n        write_manifest_atomic(\n            manifest_path, manifest\n        )\n        print(\n            f"wrote {manifest_path}: "\n            f"{len(manifest['files'])} file(s)"\n        )\n        return\n\n    result = verify_manifest(\n        manifest_path,\n        expected_model=cfg.get("model"),\n        expected_model_version=cfg.get(\n            "model_version"\n        ),\n        expected_model_directory=default_model_directory(\n            cfg, root\n        ),\n        expected_license_reference=cfg.get(\n            "model_license_reference"\n        ),\n        require_complete=bool(\n            cfg.get(\n                "model_manifest_require_complete",\n                True,\n            )\n        ),\n        verify_files=not args.skip_hash,\n    )\n    print(\n        json.dumps(\n            result, ensure_ascii=False, indent=2\n        )\n    )\n    if not result["ok"]:\n        raise SystemExit(1)\n\n\nif __name__ == "__main__":\n    main()\n')
write("test_runtime_policy.py", 'import json\nimport os\nimport tempfile\nimport time\nimport unittest\nfrom pathlib import Path\n\nfrom embedding_gallery import GalleryError, write_gallery_atomic\nfrom runtime_policy import (\n    effective_gallery_options,\n    gallery_freshness_status,\n    inspect_gallery,\n    strict_profile_issues,\n)\n\n\ndef payload(*, branch="Baghdad", model_version="v1"):\n    return {\n        "schema_version": 1,\n        "gallery_version": "runtime-test",\n        "generated_at": "2026-08-13T00:00:00Z",\n        "model": "buffalo_l",\n        "model_version": model_version,\n        "dimension": 3,\n        "normalized": True,\n        "branch": branch,\n        "employees": [\n            {\n                "employee": "HR-EMP-1",\n                "embeddings": [[1.0, 0.0, 0.0]],\n            }\n        ],\n    }\n\n\nclass RuntimePolicyTests(unittest.TestCase):\n    def setUp(self):\n        self.temp = tempfile.TemporaryDirectory()\n        self.root = Path(self.temp.name)\n        self.gallery = self.root / "embedding_gallery.json"\n        self.cfg = {\n            "production_mode": True,\n            "branch_name": "Baghdad",\n            "model": "buffalo_l",\n            "model_version": "v1",\n            "require_model_match": True,\n            "require_model_version_match": True,\n            "allow_empty_embedding_gallery": False,\n            "reject_stale_embedding_gallery": True,\n            "embedding_max_age_seconds": 3600,\n            "max_gallery_employees": 100,\n            "max_embeddings_per_employee": 5,\n            "model_manifest_require_complete": True,\n            "model_integrity_verify_on_start": True,\n            "pad_require_single_face": True,\n            "allow_insecure_central_url": False,\n            "allow_unauthenticated_embedding_sync": False,\n            "allow_insecure_frappe_url": False,\n            "pad_allow_insecure_url": False,\n            "pad_allow_unauthenticated_local": False,\n            "embedding_sync_enabled": True,\n            "central_url": "https://central.example.test",\n            "central_api_token": "secret",\n        }\n\n    def tearDown(self):\n        self.temp.cleanup()\n\n    def test_valid_strict_profile_has_no_issues(self):\n        self.assertEqual(strict_profile_issues(self.cfg), ())\n        options = effective_gallery_options(self.cfg)\n        self.assertTrue(options["require_model_match"])\n        self.assertTrue(options["require_model_version_match"])\n        self.assertFalse(options["allow_empty"])\n\n    def test_production_cannot_weaken_gallery_controls(self):\n        for key, value, code in (\n            ("require_model_match", False, "model_match_not_required"),\n            (\n                "require_model_version_match",\n                False,\n                "model_version_match_not_required",\n            ),\n            ("allow_empty_embedding_gallery", True, "empty_gallery_allowed"),\n            (\n                "reject_stale_embedding_gallery",\n                False,\n                "stale_gallery_allowed",\n            ),\n        ):\n            with self.subTest(key=key):\n                cfg = dict(self.cfg, **{key: value})\n                codes = {item[0] for item in strict_profile_issues(cfg)}\n                self.assertIn(code, codes)\n                with self.assertRaisesRegex(\n                    GalleryError, "strict production gallery policy"\n                ):\n                    effective_gallery_options(cfg)\n\n    def test_branch_and_model_version_must_match(self):\n        write_gallery_atomic(self.gallery, payload(branch="Basra"))\n        status = inspect_gallery(self.cfg, self.gallery)\n        self.assertFalse(status["available"])\n        self.assertIn("branch mismatch", status["error"])\n\n        write_gallery_atomic(\n            self.gallery, payload(model_version="v2")\n        )\n        status = inspect_gallery(self.cfg, self.gallery)\n        self.assertFalse(status["available"])\n        self.assertIn("model version mismatch", status["error"])\n\n    def test_stale_gallery_fails_closed_in_production(self):\n        write_gallery_atomic(self.gallery, payload())\n        stale = time.time() - 7200\n        os.utime(self.gallery, (stale, stale))\n        status = inspect_gallery(self.cfg, self.gallery)\n        self.assertTrue(status["available"])\n        self.assertFalse(status["policy_valid"])\n        self.assertTrue(status["stale"])\n\n    def test_nonproduction_can_report_stale_without_rejecting(self):\n        cfg = {\n            "production_mode": False,\n            "branch_name": "",\n            "model": "buffalo_l",\n            "model_version": "",\n            "require_model_match": True,\n            "require_model_version_match": False,\n            "allow_empty_embedding_gallery": False,\n            "reject_stale_embedding_gallery": False,\n            "embedding_max_age_seconds": 10,\n        }\n        status = gallery_freshness_status(\n            cfg, time.time() - 20\n        )\n        self.assertTrue(status["stale"])\n        self.assertTrue(status["policy_valid"])\n\n\nif __name__ == "__main__":\n    unittest.main()\n')
write("test_model_runtime.py", 'import tempfile\nimport unittest\nfrom pathlib import Path\n\nfrom model_runtime import ModelRuntimeError, create_face_analysis\n\n\nclass FakeApp:\n    def __init__(self, model_dir):\n        self.model_dir = str(model_dir)\n        self.prepared = None\n\n    def prepare(self, *, ctx_id, det_size):\n        self.prepared = (ctx_id, det_size)\n\n\nclass FakeFactory:\n    def __init__(self, actual_model_dir=None):\n        self.actual_model_dir = actual_model_dir\n        self.calls = []\n\n    def __call__(self, **kwargs):\n        self.calls.append(kwargs)\n        actual = (\n            self.actual_model_dir\n            or Path(kwargs["root"]) / "models" / kwargs["name"]\n        )\n        return FakeApp(actual)\n\n\nclass ModelRuntimeTests(unittest.TestCase):\n    def setUp(self):\n        self.temp = tempfile.TemporaryDirectory()\n        self.root = Path(self.temp.name)\n        self.insightface_root = self.root / "runtime"\n        self.model_dir = (\n            self.insightface_root / "models" / "buffalo_l"\n        )\n        self.model_dir.mkdir(parents=True)\n        (self.model_dir / "recognition.onnx").write_bytes(b"model")\n        self.cfg = {\n            "model": "buffalo_l",\n            "model_version": "v1",\n            "model_directory": str(self.model_dir),\n        }\n\n    def tearDown(self):\n        self.temp.cleanup()\n\n    def test_verified_directory_is_passed_as_insightface_root(self):\n        factory = FakeFactory()\n        app = create_face_analysis(\n            factory,\n            self.cfg,\n            self.root,\n            det_size=640,\n            verified_model_directory=self.model_dir,\n        )\n        self.assertEqual(\n            Path(factory.calls[0]["root"]), self.insightface_root\n        )\n        self.assertEqual(factory.calls[0]["name"], "buffalo_l")\n        self.assertEqual(app.prepared, (-1, (640, 640)))\n\n    def test_mismatched_verified_directory_is_rejected(self):\n        other = self.root / "other" / "models" / "buffalo_l"\n        other.mkdir(parents=True)\n        with self.assertRaisesRegex(\n            ModelRuntimeError, "verified model directory"\n        ):\n            create_face_analysis(\n                FakeFactory(),\n                self.cfg,\n                self.root,\n                det_size=640,\n                verified_model_directory=other,\n            )\n\n    def test_factory_loading_different_directory_is_rejected(self):\n        other = self.root / "other" / "models" / "buffalo_l"\n        other.mkdir(parents=True)\n        with self.assertRaisesRegex(\n            ModelRuntimeError, "unexpected model directory"\n        ):\n            create_face_analysis(\n                FakeFactory(actual_model_dir=other),\n                self.cfg,\n                self.root,\n                det_size=640,\n                verified_model_directory=self.model_dir,\n            )\n\n    def test_non_native_directory_layout_is_rejected(self):\n        cfg = dict(\n            self.cfg,\n            model_directory=str(self.root / "models-elsewhere"),\n        )\n        with self.assertRaisesRegex(ValueError, "root/models/<model>"):\n            create_face_analysis(\n                FakeFactory(), cfg, self.root, det_size=640\n            )\n\n\nif __name__ == "__main__":\n    unittest.main()\n')
write("test_production_readiness.py", 'import os\nimport tempfile\nimport time\nimport unittest\nfrom pathlib import Path\n\nfrom embedding_gallery import write_gallery_atomic\nfrom model_manifest import build_manifest, write_manifest_atomic\nfrom production_readiness import check_production_readiness\nfrom web_security import hash_password\n\n\ndef gallery_payload(*, branch="Baghdad", model_version="v1"):\n    return {\n        "schema_version": 1,\n        "gallery_version": "readiness-test",\n        "generated_at": "2026-08-13T00:00:00Z",\n        "model": "licensed_model",\n        "model_version": model_version,\n        "dimension": 3,\n        "normalized": True,\n        "branch": branch,\n        "employees": [\n            {\n                "employee": "HR-EMP-1",\n                "embeddings": [[1.0, 0.0, 0.0]],\n            }\n        ],\n    }\n\n\nclass ProductionReadinessTests(unittest.TestCase):\n    @classmethod\n    def setUpClass(cls):\n        cls.valid_password_hash = hash_password(\n            "correct horse battery staple"\n        )\n\n    def setUp(self):\n        self.temp = tempfile.TemporaryDirectory()\n        self.root = Path(self.temp.name)\n        self.insightface_root = self.root / "insightface"\n        self.model_dir = (\n            self.insightface_root / "models" / "licensed_model"\n        )\n        self.model_dir.mkdir(parents=True)\n        (self.model_dir / "recognition.onnx").write_bytes(b"model")\n        self.manifest = self.root / "model_manifest.json"\n        write_manifest_atomic(\n            self.manifest,\n            build_manifest(\n                model_directory=self.model_dir,\n                model="licensed_model",\n                model_version="v1",\n                license_reference="contract-123",\n            ),\n        )\n        self.gallery = self.root / "embedding_gallery.json"\n        write_gallery_atomic(self.gallery, gallery_payload())\n        self.cert = self.root / "cert.pem"\n        self.key = self.root / "key.pem"\n        self.cert.write_text("cert", encoding="utf-8")\n        self.key.write_text("key", encoding="utf-8")\n\n    def tearDown(self):\n        self.temp.cleanup()\n\n    def valid_config(self):\n        return {\n            "production_mode": True,\n            "branch_name": "Baghdad",\n            "model": "licensed_model",\n            "model_version": "v1",\n            "model_directory": str(self.model_dir),\n            "model_manifest_path": str(self.manifest),\n            "model_manifest_require_complete": True,\n            "model_integrity_verify_on_start": True,\n            "model_license_acknowledged": True,\n            "model_license_reference": "contract-123",\n            "require_model_match": True,\n            "require_model_version_match": True,\n            "allow_empty_embedding_gallery": False,\n            "reject_stale_embedding_gallery": True,\n            "embedding_max_age_seconds": 3600,\n            "embedding_sync_enabled": True,\n            "central_url": "https://central.example.test",\n            "central_api_token": "secret",\n            "allow_insecure_central_url": False,\n            "allow_unauthenticated_embedding_sync": False,\n            "pad_provider": "http",\n            "pad_required": True,\n            "pad_fail_closed": True,\n            "pad_require_single_face": True,\n            "pad_min_score": 0.8,\n            "pad_http_url": "https://pad.example.test/v1/check",\n            "pad_http_token": "secret",\n            "pad_allow_insecure_url": False,\n            "pad_allow_unauthenticated_local": False,\n            "web_admin_username": "admin",\n            "web_admin_password_hash": self.valid_password_hash,\n            "web_session_secret": "s" * 48,\n            "web_bind_host": "127.0.0.1",\n            "web_cookie_secure": True,\n            "web_hsts_enabled": True,\n            "https_reverse_proxy_acknowledged": True,\n            "frappe_url": "https://erp.example.test",\n            "allow_insecure_frappe_url": False,\n            "ftp_tls_enabled": True,\n            "ftp_tls_certfile": str(self.cert),\n            "ftp_tls_keyfile": str(self.key),\n            "ftp_tls_control_required": True,\n            "ftp_tls_data_required": True,\n            "ftp_staging_enabled": True,\n            "ftp_permissions": "elw",\n            "camera_ids": {\n                "in": "camera-in",\n                "out": "camera-out",\n            },\n        }\n\n    def report(self, cfg=None, **kwargs):\n        return check_production_readiness(\n            cfg or self.valid_config(),\n            self.root,\n            gallery_path=self.gallery,\n            **kwargs,\n        )\n\n    def test_valid_production_config_is_ready(self):\n        report = self.report()\n        self.assertTrue(report.ready, report.to_dict())\n        self.assertTrue(report.model_integrity["ok"])\n        self.assertEqual(\n            Path(report.model_integrity["insightface_root"]),\n            self.insightface_root,\n        )\n        self.assertTrue(report.gallery["policy_valid"])\n\n    def test_missing_strict_identity_is_blocked(self):\n        cfg = self.valid_config()\n        cfg.update(\n            branch_name="",\n            model_version="",\n            require_model_version_match=False,\n        )\n        codes = {issue.code for issue in self.report(cfg).blockers}\n        self.assertIn("branch_name_missing", codes)\n        self.assertIn("model_version_missing", codes)\n        self.assertIn("model_version_match_not_required", codes)\n\n    def test_malformed_admin_hash_is_blocked(self):\n        cfg = self.valid_config()\n        cfg["web_admin_password_hash"] = (\n            "scrypt$16384$8$1$salt$hash"\n        )\n        report = self.report(cfg, verify_model_files=False)\n        self.assertIn(\n            "web_admin_auth_invalid",\n            {issue.code for issue in report.blockers},\n        )\n\n    def test_wrong_branch_gallery_is_blocked(self):\n        write_gallery_atomic(\n            self.gallery, gallery_payload(branch="Basra")\n        )\n        report = self.report(verify_model_files=False)\n        self.assertIn(\n            "embedding_gallery_invalid",\n            {issue.code for issue in report.blockers},\n        )\n        self.assertIn("branch mismatch", report.gallery["error"])\n\n    def test_stale_gallery_is_blocked(self):\n        stale = time.time() - 7200\n        os.utime(self.gallery, (stale, stale))\n        report = self.report(verify_model_files=False)\n        self.assertIn(\n            "embedding_gallery_policy_failed",\n            {issue.code for issue in report.blockers},\n        )\n        self.assertTrue(report.gallery["stale"])\n\n    def test_changed_model_file_is_blocked(self):\n        (self.model_dir / "recognition.onnx").write_bytes(b"changed")\n        report = self.report()\n        self.assertIn(\n            "model_integrity_failed",\n            {issue.code for issue in report.blockers},\n        )\n\n    def test_skip_hash_still_checks_inventory_and_sizes(self):\n        (self.model_dir / "extra.onnx").write_bytes(b"extra")\n        report = self.report(verify_model_files=False)\n        self.assertFalse(report.model_integrity["ok"])\n        self.assertFalse(report.model_integrity["hashes_verified"])\n        self.assertTrue(\n            any(\n                "unlisted" in message\n                for message in report.model_integrity["errors"]\n            )\n        )\n\n    def test_missing_pad_and_license_are_blockers(self):\n        cfg = self.valid_config()\n        cfg.update(\n            model_license_acknowledged=False,\n            model_license_reference="",\n            pad_provider="disabled",\n            pad_required=False,\n        )\n        codes = {\n            issue.code\n            for issue in self.report(\n                cfg, verify_model_files=False\n            ).blockers\n        }\n        self.assertIn("model_license_not_acknowledged", codes)\n        self.assertIn("pad_not_required", codes)\n        self.assertIn("pad_provider_disabled", codes)\n\n    def test_plain_ftp_requires_isolation_ack(self):\n        cfg = self.valid_config()\n        cfg["ftp_tls_enabled"] = False\n        cfg["camera_network_isolated_acknowledged"] = False\n        report = self.report(cfg)\n        self.assertIn(\n            "camera_transport_unprotected",\n            {issue.code for issue in report.blockers},\n        )\n\n    def test_disabled_ftp_staging_is_a_blocker(self):\n        cfg = self.valid_config()\n        cfg["ftp_staging_enabled"] = False\n        report = self.report(cfg)\n        self.assertIn(\n            "ftp_staging_disabled",\n            {issue.code for issue in report.blockers},\n        )\n\n    def test_non_upload_ftp_permissions_are_a_blocker(self):\n        cfg = self.valid_config()\n        cfg["ftp_users"] = {\n            "camera_in": {"permissions": "elrw"},\n            "camera_out": {"permissions": "elw"},\n        }\n        report = self.report(cfg)\n        self.assertIn(\n            "ftp_permissions_unsafe",\n            {issue.code for issue in report.blockers},\n        )\n\n\nif __name__ == "__main__":\n    unittest.main()\n')
write("test_web_security.py", 'import unittest\n\ntry:\n    import flask  # noqa: F401\nexcept ModuleNotFoundError:\n    flask = None\n\nif flask is not None:\n    from flask import Flask\n\n    from web_security import (\n        auth_configured,\n        configure_app,\n        hash_password,\n        password_hash_issues,\n        verify_password,\n    )\n\n\n@unittest.skipIf(flask is None, "Flask dependency is not installed")\nclass WebSecurityTests(unittest.TestCase):\n    def test_password_hash_round_trip(self):\n        encoded = hash_password(\n            "correct horse battery staple"\n        )\n        self.assertFalse(password_hash_issues(encoded))\n        self.assertTrue(\n            verify_password(\n                "correct horse battery staple", encoded\n            )\n        )\n        self.assertFalse(\n            verify_password("wrong password", encoded)\n        )\n\n    def test_short_password_is_rejected(self):\n        with self.assertRaises(ValueError):\n            hash_password("too-short")\n\n    def test_prefix_only_hash_is_rejected(self):\n        malformed = "scrypt$16384$8$1$salt$hash"\n        self.assertTrue(password_hash_issues(malformed))\n        self.assertFalse(\n            auth_configured(\n                {\n                    "web_admin_username": "admin",\n                    "web_admin_password_hash": malformed,\n                    "web_session_secret": "x" * 48,\n                }\n            )\n        )\n\n    def test_unsafe_scrypt_cost_is_rejected_before_derivation(self):\n        encoded = "scrypt$1073741824$8$1$AAAAAAAAAAAAAAAAAAAAAA$" + (\n            "A" * 43\n        )\n        self.assertTrue(password_hash_issues(encoded))\n        self.assertFalse(verify_password("anything", encoded))\n\n    def test_auth_requires_persistent_secret_and_hash(self):\n        cfg = {\n            "web_admin_username": "admin",\n            "web_admin_password_hash": hash_password(\n                "correct horse battery staple"\n            ),\n            "web_session_secret": "x" * 48,\n            "web_cookie_secure": False,\n        }\n        self.assertTrue(auth_configured(cfg))\n        app = Flask(__name__)\n        self.assertTrue(configure_app(app, cfg))\n        self.assertEqual(\n            app.config["SESSION_COOKIE_SAMESITE"], "Lax"\n        )\n        self.assertTrue(\n            app.config["SESSION_COOKIE_HTTPONLY"]\n        )\n\n\nif __name__ == "__main__":\n    unittest.main()\n')
write("test_model_manifest.py", 'import json\nimport tempfile\nimport unittest\nfrom pathlib import Path\n\nfrom model_manifest import (\n    build_manifest,\n    insightface_root_for_model_directory,\n    runtime_model_binding,\n    verify_manifest,\n    write_manifest_atomic,\n)\n\n\nclass ModelManifestTests(unittest.TestCase):\n    def setUp(self):\n        self.temp = tempfile.TemporaryDirectory()\n        self.root = Path(self.temp.name)\n        self.insightface_root = self.root / "insightface"\n        self.model_dir = (\n            self.insightface_root / "models" / "licensed_model"\n        )\n        self.model_dir.mkdir(parents=True)\n        (self.model_dir / "detector.onnx").write_bytes(b"detector")\n        (self.model_dir / "recognition.onnx").write_bytes(\n            b"recognition"\n        )\n        self.path = self.root / "model_manifest.json"\n\n    def tearDown(self):\n        self.temp.cleanup()\n\n    def create(self):\n        manifest = build_manifest(\n            model_directory=self.model_dir,\n            model="licensed_model",\n            model_version="v1",\n            license_reference="contract-123",\n        )\n        write_manifest_atomic(self.path, manifest)\n        return manifest\n\n    def test_manifest_round_trip(self):\n        self.create()\n        result = verify_manifest(\n            self.path,\n            expected_model="licensed_model",\n            expected_model_version="v1",\n            expected_model_directory=self.model_dir,\n            expected_license_reference="contract-123",\n        )\n        self.assertTrue(result["ok"], result)\n        self.assertEqual(result["verified_file_count"], 2)\n        self.assertEqual(\n            Path(result["insightface_root"]), self.insightface_root\n        )\n\n    def test_runtime_binding_derives_native_root(self):\n        binding = runtime_model_binding(\n            {\n                "model": "licensed_model",\n                "model_version": "v1",\n                "model_directory": str(self.model_dir),\n            },\n            self.root,\n        )\n        self.assertEqual(\n            Path(binding["insightface_root"]), self.insightface_root\n        )\n        self.assertEqual(\n            Path(binding["model_directory"]), self.model_dir\n        )\n\n    def test_non_native_layout_is_rejected(self):\n        other = self.root / "licensed_model"\n        other.mkdir()\n        with self.assertRaisesRegex(ValueError, "root/models/<model>"):\n            insightface_root_for_model_directory(\n                other, "licensed_model"\n            )\n\n    def test_changed_model_file_is_rejected(self):\n        self.create()\n        (self.model_dir / "recognition.onnx").write_bytes(\n            b"changed"\n        )\n        result = verify_manifest(\n            self.path,\n            expected_model="licensed_model",\n            expected_model_version="v1",\n            expected_model_directory=self.model_dir,\n            expected_license_reference="contract-123",\n        )\n        self.assertFalse(result["ok"])\n        self.assertTrue(\n            any(\n                "SHA-256 mismatch" in item\n                for item in result["errors"]\n            )\n        )\n\n    def test_unlisted_file_is_rejected(self):\n        self.create()\n        (self.model_dir / "extra.bin").write_bytes(b"extra")\n        result = verify_manifest(\n            self.path,\n            expected_model="licensed_model",\n            expected_model_directory=self.model_dir,\n            expected_license_reference="contract-123",\n        )\n        self.assertFalse(result["ok"])\n        self.assertTrue(\n            any("unlisted" in item for item in result["errors"])\n        )\n\n    def test_skip_hash_still_rejects_size_mismatch(self):\n        manifest = self.create()\n        manifest["files"][0]["size"] += 1\n        write_manifest_atomic(self.path, manifest)\n        result = verify_manifest(\n            self.path,\n            expected_model="licensed_model",\n            expected_model_directory=self.model_dir,\n            expected_license_reference="contract-123",\n            verify_files=False,\n        )\n        self.assertFalse(result["ok"])\n        self.assertFalse(result["hashes_verified"])\n        self.assertTrue(\n            any(\n                "size mismatch" in item\n                for item in result["errors"]\n            )\n        )\n\n    def test_placeholder_license_reference_is_rejected(self):\n        with self.assertRaises(ValueError):\n            build_manifest(\n                model_directory=self.model_dir,\n                model="licensed_model",\n                license_reference="CHANGE_ME",\n            )\n\n\nif __name__ == "__main__":\n    unittest.main()\n')

# Extend GalleryReloader so local runtime validation enforces the same
# employee/template limits as synchronization and remembers the activation time
# of the last successfully loaded gallery.
replace_once(
    "embedding_gallery.py",
    '''        require_model_version_match=False,
        allow_empty=False,
    ):
        self.path = Path(path)
        self.expected_model = expected_model
        self.expected_model_version = expected_model_version
        self.expected_branch = expected_branch
        self.require_model_match = require_model_match
        self.require_model_version_match = require_model_version_match
        self.allow_empty = allow_empty
        self.signature = None
        self.known = []
        self.metadata = {}
''',
    '''        require_model_version_match=False,
        allow_empty=False,
        max_employees=10000,
        max_embeddings_per_employee=50,
    ):
        self.path = Path(path)
        self.expected_model = expected_model
        self.expected_model_version = expected_model_version
        self.expected_branch = expected_branch
        self.require_model_match = require_model_match
        self.require_model_version_match = require_model_version_match
        self.allow_empty = allow_empty
        self.max_employees = int(max_employees)
        self.max_embeddings_per_employee = int(max_embeddings_per_employee)
        self.signature = None
        self.updated_unix = 0.0
        self.known = []
        self.metadata = {}
''',
)
replace_once(
    "embedding_gallery.py",
    '''            require_model_version_match=self.require_model_version_match,
            allow_empty=self.allow_empty,
        )
        self.known = known
        self.metadata = metadata
        self.signature = gallery_signature(self.path)
        return self.known, self.metadata, True
''',
    '''            require_model_version_match=self.require_model_version_match,
            allow_empty=self.allow_empty,
            max_employees=self.max_employees,
            max_embeddings_per_employee=self.max_embeddings_per_employee,
        )
        self.known = known
        self.metadata = metadata
        self.signature = gallery_signature(self.path)
        try:
            self.updated_unix = self.path.stat().st_mtime
        except OSError:
            self.updated_unix = 0.0
        return self.known, self.metadata, True
''',
)

# Make the bounded synchronization client consume the shared effective gallery
# policy and evaluate it before any network request.
replace_once(
    "secure_sync.py",
    '''from embedding_gallery import (
    GalleryError,
    load_gallery,
    read_sync_status,
    validate_gallery,
    write_gallery_atomic,
    write_sync_status,
)


DEFAULT_ENDPOINT''',
    '''from embedding_gallery import (
    GalleryError,
    load_gallery,
    read_sync_status,
    validate_gallery,
    write_gallery_atomic,
    write_sync_status,
)
from runtime_policy import effective_gallery_options


DEFAULT_ENDPOINT''',
)
replace_between(
    "secure_sync.py",
    "def _gallery_options(cfg):\\n",
    "\\n\\ndef _local_metadata",
    '''def _gallery_options(cfg):
    return effective_gallery_options(cfg)
''',
)
replace_once(
    "secure_sync.py",
    '''def _local_metadata(gallery_path, cfg):
    _, metadata, _ = load_gallery(gallery_path, **_gallery_options(cfg))
    return metadata
''',
    '''def _local_metadata(gallery_path, options):
    _, metadata, _ = load_gallery(gallery_path, **options)
    return metadata
''',
)
replace_once(
    "secure_sync.py",
    '''    attempted_at = utc_now()
    requested_url = _validate_source(cfg)
    resolved_url = requested_url
    gallery_path = Path(gallery_path)
    status = read_sync_status(status_path)
    branch = _text(cfg.get("branch_name"))
''',
    '''    attempted_at = utc_now()
    options = _gallery_options(cfg)
    requested_url = _validate_source(cfg)
    resolved_url = requested_url
    gallery_path = Path(gallery_path)
    status = read_sync_status(status_path)
    branch = options["expected_branch"]
''',
)
replace_once(
    "secure_sync.py",
    "metadata = _local_metadata(gallery_path, cfg)",
    "metadata = _local_metadata(gallery_path, options)",
)
replace_once(
    "secure_sync.py",
    '''                sanitized, _, metadata = validate_gallery(
                    payload, **_gallery_options(cfg)
                )
                try:
                    current = _local_metadata(gallery_path, cfg)
''',
    '''                sanitized, _, metadata = validate_gallery(
                    payload, **options
                )
                try:
                    current = _local_metadata(gallery_path, options)
''',
)
replace_once(
    "secure_sync.py",
    '''                    write_gallery_atomic(
                        gallery_path, sanitized, **_gallery_options(cfg)
                    )
''',
    '''                    write_gallery_atomic(
                        gallery_path, sanitized, **options
                    )
''',
)

# Bind InsightFace to the configured model directory and route every gallery
# consumer through the shared policy.
replace_once(
    "face_attendance.py",
    '''from watcher_entrypoints import require_legacy_dry_run
''',
    '''from model_runtime import ModelRuntimeError, create_face_analysis
from runtime_policy import (
    effective_gallery_options,
    enforce_gallery_freshness,
    inspect_gallery,
    load_runtime_gallery,
)
from watcher_entrypoints import require_legacy_dry_run
''',
)
replace_between(
    "face_attendance.py",
    "def face_app(det_size=None):\n",
    "\n\ndef scaled_frame",
    '''def face_app(
    det_size=None,
    *,
    cfg=None,
    verified_model_directory=None,
):
    cfg = cfg or load_config()
    det_size = int(det_size or cfg.get("det_size", 640))
    try:
        return create_face_analysis(
            FaceAnalysis,
            cfg,
            ROOT,
            det_size=det_size,
            verified_model_directory=verified_model_directory,
        )
    except (ModelRuntimeError, ValueError) as exc:
        raise SystemExit(f"Face model runtime validation failed: {exc}") from exc
''',
)
replace_once(
    "face_attendance.py",
    '    app = face_app(cfg.get("build_det_size", 640))\n',
    '    app = face_app(cfg.get("build_det_size", 640), cfg=cfg)\n',
)
replace_once(
    "face_attendance.py",
    '''    _, metadata = write_gallery_atomic(
        EMBEDDINGS,
        payload,
        expected_model=cfg.get("model", "buffalo_l"),
        expected_model_version=cfg.get("model_version"),
        expected_branch=cfg.get("branch_name", ""),
        require_model_match=True,
        require_model_version_match=bool(
            cfg.get("require_model_version_match", False)
        ),
        allow_empty=False,
        max_embeddings_per_employee=int(
            cfg.get("max_embeddings_per_employee", 50)
        ),
    )
''',
    '''    _, metadata = write_gallery_atomic(
        EMBEDDINGS,
        payload,
        **effective_gallery_options(cfg),
    )
''',
)
replace_once(
    "face_attendance.py",
    "    app = face_app()\n    out_dir = FACES / employee\n",
    "    app = face_app(cfg=cfg)\n    out_dir = FACES / employee\n",
)
replace_between(
    "face_attendance.py",
    "def load_embeddings():\n",
    "\n\nclass GalleryRuntime",
    '''def load_embeddings():
    cfg = load_config()
    migrate_legacy_embeddings(cfg)
    known, _, _, _ = load_runtime_gallery(cfg, EMBEDDINGS)
    return known
''',
)
replace_between(
    "face_attendance.py",
    "class GalleryRuntime:\n",
    "\n\ndef load_cooldown_state",
    '''class GalleryRuntime:
    def __init__(self, cfg):
        self.cfg = cfg
        self.last_sync_attempt = 0.0
        self.rejected_signature = None
        self.reloader = GalleryReloader(
            EMBEDDINGS,
            **effective_gallery_options(cfg),
        )

    def sync_enabled(self):
        return bool(self.cfg.get("embedding_sync_enabled", True)) and bool(
            self.cfg.get("central_url")
        )

    def maybe_sync(self, force=False):
        if not self.sync_enabled():
            return
        interval = max(
            10, int(self.cfg.get("embedding_sync_interval_seconds", 300))
        )
        now = time.monotonic()
        if not force and now - self.last_sync_attempt < interval:
            return
        self.last_sync_attempt = now
        try:
            result = sync_gallery(self.cfg, EMBEDDINGS, SYNC_STATUS)
            action = "updated" if result["changed"] else "unchanged"
            log(
                f"embedding sync {action}: version={result['gallery_version']} "
                f"employees={result['employee_count']} "
                f"embeddings={result['embedding_count']}"
            )
        except GalleryError as exc:
            log(f"embedding sync failed; keeping current gallery: {exc}")

    def check_freshness(self):
        status = enforce_gallery_freshness(
            self.cfg,
            self.reloader.updated_unix,
            path=EMBEDDINGS,
        )
        if status.get("stale"):
            log(
                "embedding gallery is stale but permitted outside strict "
                f"production: age={status['age_seconds']}s "
                f"max={status['max_age_seconds']}s"
            )
        return status

    def start(self):
        migrate_legacy_embeddings(self.cfg)
        self.maybe_sync(force=True)
        try:
            known, metadata, _ = self.reloader.reload(force=True)
            self.check_freshness()
        except GalleryError as exc:
            raise SystemExit(
                f"No valid embedding gallery is available: {exc}. "
                "Run 'python sync_embeddings.py' or enable local enrollment and run "
                "'python face_attendance.py build'."
            ) from exc
        log(
            f"embedding gallery loaded: version={metadata.get('gallery_version')} "
            f"employees={metadata.get('employee_count')} "
            f"embeddings={metadata.get('embedding_count')}"
        )
        return known

    def refresh(self):
        self.maybe_sync()
        try:
            known, metadata, changed = self.reloader.reload()
            self.check_freshness()
            self.rejected_signature = None
            if changed:
                log(
                    f"embedding gallery reloaded: version={metadata.get('gallery_version')} "
                    f"employees={metadata.get('employee_count')} "
                    f"embeddings={metadata.get('embedding_count')}"
                )
            return known
        except GalleryError as exc:
            if self.reloader.known:
                rejected_signature = gallery_signature(EMBEDDINGS)
                if rejected_signature != self.rejected_signature:
                    log(
                        "embedding reload rejected; keeping previous gallery: "
                        f"{exc}"
                    )
                    self.rejected_signature = rejected_signature
                return self.reloader.known
            raise
''',
)
replace_between(
    "face_attendance.py",
    "def print_embedding_status():\n",
    "\n\ndef main():",
    '''def print_embedding_status():
    cfg = load_config()
    print(
        json.dumps(
            inspect_gallery(cfg, EMBEDDINGS),
            ensure_ascii=False,
            indent=2,
        )
    )
''',
)

# Pass the manifest-verified directory into the actual InsightFace loader.
replace_once(
    "watch_service.py",
    '''    report = check_production_readiness(
        cfg,
        ROOT,
        verify_model_files=bool(cfg.get("model_integrity_verify_on_start", True)),
    )
''',
    '''    report = check_production_readiness(
        cfg,
        ROOT,
        verify_model_files=bool(cfg.get("model_integrity_verify_on_start", True)),
        gallery_path=ROOT / "embedding_gallery.json",
    )
''',
)
replace_once(
    "watch_service.py",
    '''    state = state_for_config(cfg)
    app = attendance.face_app()
    gallery = attendance.GalleryRuntime(cfg)
''',
    '''    state = state_for_config(cfg)
    verified_model_directory = (
        report.model_integrity.get("model_directory")
        if report.model_integrity.get("ok")
        else None
    )
    app = attendance.face_app(
        cfg=cfg,
        verified_model_directory=verified_model_directory,
    )
    gallery = attendance.GalleryRuntime(cfg)
''',
)

# Make the web UI, /readyz, and export path use the same gallery/readiness policy.
replace_once(
    "web_admin.py",
    '''from runtime_state import RuntimeState, resolve_runtime_path
from secure_sync import sync_gallery
from web_security import (
''',
    '''from runtime_policy import effective_gallery_options
from runtime_state import RuntimeState, resolve_runtime_path
from secure_sync import sync_gallery
from web_security import (
''',
)
replace_between(
    "web_admin.py",
    "def payload(cfg):\n",
    "\n\ndef employees",
    '''def payload(cfg):
    return load_gallery(
        GALLERY,
        **effective_gallery_options(cfg),
    )[2]
''',
)
replace_between(
    "web_admin.py",
    "def readiness(cfg):\n",
    "\n\n@app.after_request",
    '''def readiness(cfg):
    return check_production_readiness(
        cfg,
        ROOT,
        verify_model_files=False,
        gallery_path=GALLERY,
    )
''',
)
replace_between(
    "web_admin.py",
    '@app.get("/readyz")\ndef ready():\n',
    '\n\n@app.route("/login"',
    '''@app.get("/readyz")
def ready():
    cfg = load_config()
    report = readiness(cfg)
    gallery = report.gallery
    reasons = []
    if not auth_configured(cfg):
        reasons.append("admin authentication is not configured")
    if not gallery.get("available") or not gallery.get(
        "policy_valid", False
    ):
        reasons.append(
            gallery.get("error") or "gallery unavailable"
        )
    if bool(cfg.get("production_mode", False)):
        reasons.extend(issue.message for issue in report.blockers)
    reasons = list(dict.fromkeys(reasons))
    return (
        jsonify(
            ok=not reasons,
            reasons=reasons,
            gallery=gallery,
            production=report.to_dict(),
        ),
        200 if not reasons else 503,
    )
''',
)
replace_between(
    "web_admin.py",
    '@app.get("/")\n@login_required\ndef index():\n',
    '\n\n@app.post("/sync")',
    '''@app.get("/")
@login_required
def index():
    cfg = load_config()
    report = readiness(cfg)
    return render_template_string(
        HOME,
        style=STYLE,
        cfg=cfg,
        readiness=report,
        gallery=report.gallery,
        sync=read_sync_status(SYNC_STATUS),
        employees=employees(cfg),
        sync_enabled=bool(
            cfg.get("embedding_sync_enabled", True)
            and cfg.get("central_url")
        ),
        enroll=bool(cfg.get("local_enrollment_enabled", False)),
        user=admin_user(),
        csrf=csrf_token(),
        msg=request.args.get("msg"),
        error=request.args.get("error") == "1",
    )
''',
)

# Safer defaults: even development starts from strict model-version matching and
# stale-gallery rejection unless explicitly changed outside production.
replace_once(
    "config.example.json",
    '  "reject_stale_embedding_gallery": false,\n',
    '  "reject_stale_embedding_gallery": true,\n',
)
replace_once(
    "config.example.json",
    '  "require_model_version_match": false,\n',
    '  "require_model_version_match": true,\n',
)

# Synchronization regression coverage: strict policy fails before networking and
# remote model-version mismatches are rejected.
replace_once(
    "test_secure_sync.py",
    'def payload(*, dimension=3, version="sync-test"):\n',
    'def payload(*, dimension=3, version="sync-test", model_version=""):\n',
)
replace_once(
    "test_secure_sync.py",
    '        "model_version": "",\n',
    '        "model_version": model_version,\n',
)
replace_once(
    "test_secure_sync.py",
    '''    def tearDown(self):
        self.temp.cleanup()

    def test_sync_writes_gallery_and_etag(self):
''',
    '''    def tearDown(self):
        self.temp.cleanup()

    def strict_config(self):
        return dict(
            self.cfg,
            production_mode=True,
            model_version="v1",
            require_model_match=True,
            require_model_version_match=True,
            allow_empty_embedding_gallery=False,
            reject_stale_embedding_gallery=True,
            embedding_max_age_seconds=3600,
        )

    def test_sync_writes_gallery_and_etag(self):
''',
)
replace_once(
    "test_secure_sync.py",
    '''    def test_redirect_limit_is_enforced(self):
        cfg = dict(self.cfg, embedding_max_redirects=1)
        first = FakeResponse(status=302, headers={"Location": "/one"})
        second = FakeResponse(status=302, headers={"Location": "/two"})
        session = FakeSession([first, second])
        with self.assertRaisesRegex(Exception, "maximum redirects"):
            sync_gallery(
                cfg,
                self.gallery,
                self.status,
                session=session,
                sleep=lambda _: None,
            )
        self.assertEqual(len(session.calls), 2)
        self.assertTrue(second.closed)


if __name__ == "__main__":
''',
    '''    def test_redirect_limit_is_enforced(self):
        cfg = dict(self.cfg, embedding_max_redirects=1)
        first = FakeResponse(status=302, headers={"Location": "/one"})
        second = FakeResponse(status=302, headers={"Location": "/two"})
        session = FakeSession([first, second])
        with self.assertRaisesRegex(Exception, "maximum redirects"):
            sync_gallery(
                cfg,
                self.gallery,
                self.status,
                session=session,
                sleep=lambda _: None,
            )
        self.assertEqual(len(session.calls), 2)
        self.assertTrue(second.closed)

    def test_strict_policy_is_checked_before_network(self):
        cfg = self.strict_config()
        cfg["require_model_version_match"] = False
        session = FakeSession([])
        with self.assertRaisesRegex(Exception, "strict production gallery policy"):
            sync_gallery(
                cfg,
                self.gallery,
                self.status,
                session=session,
                sleep=lambda _: None,
            )
        self.assertEqual(session.calls, [])

    def test_strict_remote_model_version_mismatch_is_rejected(self):
        session = FakeSession(
            [
                FakeResponse(
                    body=payload(model_version="v2"),
                    headers={"Content-Type": "application/json"},
                )
            ]
        )
        with self.assertRaisesRegex(Exception, "model version mismatch"):
            sync_gallery(
                self.strict_config(),
                self.gallery,
                self.status,
                session=session,
                sleep=lambda _: None,
            )
        self.assertFalse(self.gallery.exists())


if __name__ == "__main__":
''',
)

# Web readiness must surface the same branch/model gallery policy.
replace_once(
    "test_web_admin.py",
    '        "model_version": "",\n',
    '        "model_version": "v1",\n',
)
replace_once(
    "test_web_admin.py",
    '''            "branch_name": "Baghdad",
            "model": "buffalo_l",
            "embedding_export_enabled": True,
''',
    '''            "branch_name": "Baghdad",
            "model": "buffalo_l",
            "model_version": "v1",
            "require_model_match": True,
            "require_model_version_match": True,
            "reject_stale_embedding_gallery": True,
            "embedding_max_age_seconds": 3600,
            "embedding_export_enabled": True,
''',
)
replace_once(
    "test_web_admin.py",
    '''    def test_state_change_rejects_missing_csrf(self):
        self.login()
        self.assertEqual(self.client.post("/logout").status_code, 400)


if __name__ == "__main__":
''',
    '''    def test_state_change_rejects_missing_csrf(self):
        self.login()
        self.assertEqual(self.client.post("/logout").status_code, 400)

    def test_readyz_uses_strict_branch_policy(self):
        invalid = payload()
        invalid["branch"] = "Basra"
        write_gallery_atomic(self.gallery, invalid)
        response = self.client.get("/readyz")
        self.assertEqual(response.status_code, 503)
        self.assertTrue(
            any(
                "branch mismatch" in reason
                for reason in response.get_json()["reasons"]
            )
        )


if __name__ == "__main__":
''',
)

# Run the new policy and runtime-binding tests in CI.
replace_once(
    ".github/workflows/tests.yml",
    '''            test_secure_sync.py \\
            test_legacy_gallery_converter.py \\
            test_runtime_state.py \\
''',
    '''            test_secure_sync.py \\
            test_legacy_gallery_converter.py \\
            test_runtime_policy.py \\
            test_model_runtime.py \\
            test_runtime_state.py \\
''',
)

# Document the strict production policy and the model-directory binding that
# now applies consistently across sync, readiness, the watcher, and the UI.
replace_once(
    "README.md",
    "- `watch_service.py` — production FTP watcher with readiness, PAD, replay protection, and event state.\n",
    "- `watch_service.py` — production FTP watcher with readiness, PAD, replay protection, and event state.\n"
    "- `runtime_policy.py` — shared strict gallery/profile policy used by sync, readiness, the watcher, and the web UI.\n"
    "- `model_runtime.py` — binds InsightFace to the manifest-verified `root/models/<model>` directory.\n",
)
replace_once(
    "README.md",
    '''  "embedding_max_redirects": 3,
  "require_model_match": true,
  "local_enrollment_enabled": false,
  "model": "buffalo_l"
''',
    '''  "embedding_max_redirects": 3,
  "reject_stale_embedding_gallery": true,
  "require_model_match": true,
  "require_model_version_match": true,
  "local_enrollment_enabled": false,
  "model": "buffalo_l",
  "model_version": "APPROVED-MODEL-BUILD"
''',
)
replace_once(
    "README.md",
    '''The synchronization timer refreshes the gallery, and the production watcher loads valid changes without restarting. A failed, empty, wrong-branch, wrong-model, malformed, or dimension-incompatible gallery is rejected while the previous working gallery remains active.

## Trusted enrollment/export server
''',
    '''The synchronization timer refreshes the gallery, and the production watcher loads valid changes without restarting. A failed, empty, wrong-branch, wrong-model, malformed, or dimension-incompatible gallery is rejected while the previous working gallery remains active.

### Strict production profile

When `production_mode` is true, the application applies one effective policy everywhere. The sync client checks it before making a network request, `/readyz` and the dashboard inspect the same branch/model/version/freshness rules, and the watcher loads only a gallery accepted by that policy.

Production requires a non-placeholder `branch_name`, `model`, and `model_version`; exact model and model-version matching; a nonempty gallery; positive gallery age limits; stale-gallery rejection; complete startup model verification; per-event fail-closed PAD; and no insecure or unauthenticated service overrides. Configuration flags cannot silently weaken these controls while production mode is enabled.

The configured model directory must use InsightFace's native layout:

```text
<insightface-root>/models/<model>
```

For example:

```json
{
  "model": "buffalo_l",
  "model_version": "APPROVED-MODEL-BUILD",
  "model_directory": "/srv/face-models/models/buffalo_l"
}
```

The manifest verifier checks that exact directory, and the same derived `/srv/face-models` root is passed to `FaceAnalysis`. Startup then verifies that InsightFace actually reports the expected model directory before any camera processing begins. The runtime therefore cannot verify one directory and silently load another.

## Trusted enrollment/export server
''',
)
replace_once(
    "HANDOFF.md",
    "- Current branch/model validation coverage and limitations are tracked in the [platform plan baseline](docs/attendance-platform-plan.md#4-current-baseline).\n",
    "- In production, one shared policy requires explicit branch, model, model version, nonempty/fresh gallery state, and exact compatibility in sync, readiness, web status, and watcher loading.\n"
    "- The manifest-verified `root/models/<model>` directory is the directory passed to and confirmed by the InsightFace runtime.\n",
)
replace_once(
    "docs/security-hardening.md",
    '''Changing the recognition model requires rebuilding every employee embedding and recalibrating the recognition threshold and score margin. Never mix embeddings from different models or preprocessing pipelines.

## Web administration
''',
    '''Changing the recognition model requires rebuilding every employee embedding and recalibrating the recognition threshold and score margin. Never mix embeddings from different models or preprocessing pipelines.

`model_directory` must be the exact `<insightface-root>/models/<model>` directory. The manifest verifier validates that layout and its complete file inventory. The watcher derives the InsightFace root from it, passes that root to `FaceAnalysis`, and confirms the model directory reported by the runtime. A manifest for one directory cannot authorize a different directory loaded through InsightFace's default cache.

## Web administration
''',
)
replace_once(
    "docs/security-hardening.md",
    '''The sync client validates HTTPS, authentication, content type, response size, schema, branch, model, dimensions, and vector values. It uses conditional requests, bounded timeouts, and retry backoff. A failed sync leaves the last valid local gallery untouched.

Useful commands:
''',
    '''The sync client validates HTTPS, authentication, content type, response size, schema, branch, model, dimensions, and vector values. It uses conditional requests, bounded timeouts, and retry backoff. A failed sync leaves the last valid local gallery untouched.

In production, `runtime_policy.py` is authoritative for branch, model, model version, employee/template limits, empty-gallery behavior, and freshness. Sync validates the policy before networking; the local reloader retains the activation time of the last successfully loaded gallery; `/readyz`, the dashboard, export routes, and the watcher all consume the same effective options. Replacing a valid gallery with a malformed or newly timestamped invalid file cannot reset the age of the active in-memory gallery.

Useful commands:
''',
)
replace_once(
    "docs/production-readiness.md",
    '''  "model_license_acknowledged": true,
  "model_license_reference": "CONTRACT-OR-APPROVAL-ID",
  "model_directory": "/home/service-user/.insightface/models/buffalo_l",
  "model_manifest_path": "model_manifest.json"
''',
    '''  "model": "buffalo_l",
  "model_version": "APPROVED-MODEL-BUILD",
  "model_license_acknowledged": true,
  "model_license_reference": "CONTRACT-OR-APPROVAL-ID",
  "model_directory": "/home/service-user/.insightface/models/buffalo_l",
  "model_manifest_path": "model_manifest.json",
  "model_manifest_require_complete": true,
  "model_integrity_verify_on_start": true
''',
)
replace_once(
    "docs/production-readiness.md",
    '''The manifest records the exact model name, version, directory, file sizes, and SHA-256 hashes. The production watcher verifies it once at startup. A changed, missing, extra, or mismatched model file blocks production mode.

Creating a manifest is an integrity control, not proof that a license was obtained.
''',
    '''The manifest records the exact model name, version, directory, file sizes, and SHA-256 hashes. The model directory must be `<insightface-root>/models/<model>`. The production watcher verifies the complete inventory at startup, passes the derived root into InsightFace, and confirms that the runtime loaded the exact verified directory. A changed, missing, extra, mismatched, or differently loaded model file blocks production mode.

Creating a manifest is an integrity control, not proof that a license was obtained.
''',
)
replace_once(
    "docs/production-readiness.md",
    '''After every blocker is resolved:

```json
"production_mode": true
```
''',
    '''The readiness command, `/readyz`, synchronization, and the canonical watcher all use the same effective production profile. A malformed admin hash that merely starts with `scrypt$`, a blank branch/model version, disabled compatibility matching, an empty or stale gallery, an incomplete manifest, or an insecure override remains a blocker.

After every blocker is resolved:

```json
"production_mode": true
```
''',
)
