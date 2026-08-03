import base64
import hashlib
import hmac
import secrets
from datetime import timedelta
from functools import wraps
from urllib.parse import urlsplit

from flask import abort, redirect, request, session, url_for


PASSWORD_SCHEME = "scrypt"
DEFAULT_SCRYPT_N = 2**14
DEFAULT_SCRYPT_R = 8
DEFAULT_SCRYPT_P = 1
PLACEHOLDERS = {"", "CHANGE_ME", "REPLACE_ME", "CHANGEME"}


def _b64encode(value):
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value):
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def is_placeholder(value):
    return str(value or "").strip().upper() in PLACEHOLDERS


def hash_password(password, *, n=DEFAULT_SCRYPT_N, r=DEFAULT_SCRYPT_R, p=DEFAULT_SCRYPT_P):
    if not isinstance(password, str):
        raise TypeError("password must be text")
    if len(password) < 12:
        raise ValueError("admin password must contain at least 12 characters")
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=int(n),
        r=int(r),
        p=int(p),
        maxmem=128 * 1024 * 1024,
        dklen=32,
    )
    return "$".join(
        (
            PASSWORD_SCHEME,
            str(int(n)),
            str(int(r)),
            str(int(p)),
            _b64encode(salt),
            _b64encode(derived),
        )
    )


def verify_password(password, encoded):
    try:
        scheme, n, r, p, salt_value, expected_value = str(encoded).split("$", 5)
        if scheme != PASSWORD_SCHEME:
            return False
        expected = _b64decode(expected_value)
        actual = hashlib.scrypt(
            str(password).encode("utf-8"),
            salt=_b64decode(salt_value),
            n=int(n),
            r=int(r),
            p=int(p),
            maxmem=128 * 1024 * 1024,
            dklen=len(expected),
        )
        return hmac.compare_digest(actual, expected)
    except (TypeError, ValueError, OverflowError):
        return False


def auth_configured(cfg):
    username = str(cfg.get("web_admin_username") or "").strip()
    password_hash = str(cfg.get("web_admin_password_hash") or "").strip()
    session_secret = str(cfg.get("web_session_secret") or "").strip()
    return bool(
        username
        and password_hash.startswith(f"{PASSWORD_SCHEME}$")
        and len(session_secret) >= 32
        and not is_placeholder(session_secret)
    )


def configure_app(app, cfg):
    configured = auth_configured(cfg)
    secret = str(cfg.get("web_session_secret") or "").strip()
    if not configured:
        # This only lets the setup and health pages render. Sessions are intentionally
        # invalidated on every restart until a real persistent secret is configured.
        secret = secrets.token_urlsafe(48)
    minutes = max(5, int(cfg.get("web_session_minutes", 30)))
    app.config.update(
        SECRET_KEY=secret,
        AUTH_CONFIGURED=configured,
        SESSION_COOKIE_NAME="face_attendance_admin",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=bool(cfg.get("web_cookie_secure", True)),
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(minutes=minutes),
        MAX_CONTENT_LENGTH=int(cfg.get("web_max_request_bytes", 64 * 1024 * 1024)),
    )
    return configured


def csrf_token():
    value = session.get("csrf_token")
    if not value:
        value = secrets.token_urlsafe(32)
        session["csrf_token"] = value
    return value


def validate_csrf():
    expected = str(session.get("csrf_token") or "")
    supplied = str(request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or "")
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        abort(400, description="invalid CSRF token")


def admin_user():
    return str(session.get("admin_user") or "")


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin_authenticated"):
            return redirect(url_for("login", next=request.full_path.rstrip("?")))
        return view(*args, **kwargs)

    return wrapped


def csrf_protected(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        validate_csrf()
        return view(*args, **kwargs)

    return wrapped


def safe_next_url(value, fallback="/"):
    value = str(value or "").strip()
    parsed = urlsplit(value)
    if not value or parsed.scheme or parsed.netloc or not value.startswith("/") or value.startswith("//"):
        return fallback
    return value


def remote_address():
    return str(request.remote_addr or "unknown")


def add_security_headers(response, cfg):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()"
    )
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'",
    )
    if bool(cfg.get("web_hsts_enabled", True)):
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    if request.path.startswith("/api/") or request.path in {"/", "/login"}:
        response.headers.setdefault("Cache-Control", "no-store")
    return response
