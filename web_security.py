import base64
import binascii
import hashlib
import hmac
import ipaddress
import secrets
from datetime import timedelta
from functools import wraps
from urllib.parse import urlsplit

from flask import abort, redirect, request, session, url_for


PASSWORD_SCHEME = "scrypt"
DEFAULT_SCRYPT_N = 2**14
DEFAULT_SCRYPT_R = 8
DEFAULT_SCRYPT_P = 1
MIN_SCRYPT_N = 2**14
MAX_SCRYPT_N = 2**20
MAX_SCRYPT_R = 32
MAX_SCRYPT_P = 16
MIN_SALT_BYTES = 16
MAX_SALT_BYTES = 64
MIN_DERIVED_BYTES = 32
MAX_DERIVED_BYTES = 64
PLACEHOLDERS = {"", "CHANGE_ME", "REPLACE_ME", "CHANGEME"}


def _b64encode(value):
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value):
    if not isinstance(value, str) or not value:
        raise ValueError("encoded value is empty")
    try:
        raw = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("encoded value must be ASCII") from exc
    padding = b"=" * (-len(raw) % 4)
    try:
        return base64.b64decode(raw + padding, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("encoded value is not valid URL-safe base64") from exc


def is_placeholder(value):
    return str(value or "").strip().upper() in PLACEHOLDERS


def _validate_scrypt_parameters(n, r, p):
    try:
        n = int(n)
        r = int(r)
        p = int(p)
    except (TypeError, ValueError) as exc:
        raise ValueError("scrypt parameters must be integers") from exc
    if n < MIN_SCRYPT_N or n > MAX_SCRYPT_N or n & (n - 1):
        raise ValueError(
            f"scrypt n must be a power of two from {MIN_SCRYPT_N} through {MAX_SCRYPT_N}"
        )
    if r < 1 or r > MAX_SCRYPT_R:
        raise ValueError(f"scrypt r must be from 1 through {MAX_SCRYPT_R}")
    if p < 1 or p > MAX_SCRYPT_P:
        raise ValueError(f"scrypt p must be from 1 through {MAX_SCRYPT_P}")
    return n, r, p


def parse_password_hash(encoded):
    parts = str(encoded or "").split("$")
    if len(parts) != 6:
        raise ValueError("password hash must contain six '$'-separated fields")
    scheme, n_value, r_value, p_value, salt_value, expected_value = parts
    if scheme != PASSWORD_SCHEME:
        raise ValueError(f"password hash scheme must be {PASSWORD_SCHEME}")
    n, r, p = _validate_scrypt_parameters(n_value, r_value, p_value)
    salt = _b64decode(salt_value)
    expected = _b64decode(expected_value)
    if not MIN_SALT_BYTES <= len(salt) <= MAX_SALT_BYTES:
        raise ValueError(
            f"password hash salt must be {MIN_SALT_BYTES}-{MAX_SALT_BYTES} bytes"
        )
    if not MIN_DERIVED_BYTES <= len(expected) <= MAX_DERIVED_BYTES:
        raise ValueError(
            "password hash derived value must be "
            f"{MIN_DERIVED_BYTES}-{MAX_DERIVED_BYTES} bytes"
        )
    return {
        "n": n,
        "r": r,
        "p": p,
        "salt": salt,
        "expected": expected,
    }


def password_hash_issues(encoded):
    try:
        parse_password_hash(encoded)
        return ()
    except ValueError as exc:
        return (str(exc),)


def hash_password(
    password,
    *,
    n=DEFAULT_SCRYPT_N,
    r=DEFAULT_SCRYPT_R,
    p=DEFAULT_SCRYPT_P,
):
    if not isinstance(password, str):
        raise TypeError("password must be text")
    if len(password) < 12:
        raise ValueError("admin password must contain at least 12 characters")
    n, r, p = _validate_scrypt_parameters(n, r, p)
    salt = secrets.token_bytes(MIN_SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        maxmem=128 * 1024 * 1024,
        dklen=MIN_DERIVED_BYTES,
    )
    return "$".join(
        (
            PASSWORD_SCHEME,
            str(n),
            str(r),
            str(p),
            _b64encode(salt),
            _b64encode(derived),
        )
    )


def verify_password(password, encoded):
    try:
        parsed = parse_password_hash(encoded)
        actual = hashlib.scrypt(
            str(password).encode("utf-8"),
            salt=parsed["salt"],
            n=parsed["n"],
            r=parsed["r"],
            p=parsed["p"],
            maxmem=128 * 1024 * 1024,
            dklen=len(parsed["expected"]),
        )
        return hmac.compare_digest(actual, parsed["expected"])
    except (TypeError, ValueError, OverflowError, MemoryError):
        return False


def session_secret_issues(cfg):
    session_secret = str(cfg.get("web_session_secret") or "").strip()
    if len(session_secret) < 32 or is_placeholder(session_secret):
        return (
            "web_session_secret must be a persistent non-placeholder value of at least 32 characters",
        )
    return ()


def auth_configuration_issues(cfg):
    issues = []
    username = str(cfg.get("web_admin_username") or "").strip()
    password_hash = str(cfg.get("web_admin_password_hash") or "").strip()
    if not username:
        issues.append("web_admin_username is not configured")
    issues.extend(password_hash_issues(password_hash))
    issues.extend(session_secret_issues(cfg))
    return tuple(issues)


def auth_configured(cfg):
    return not auth_configuration_issues(cfg)


def configure_app(app, cfg):
    mode = str(cfg.get("web_auth_mode") or "local").strip().lower()
    configured = (
        not session_secret_issues(cfg)
        if mode == "adapter"
        else auth_configured(cfg)
    )
    secret = str(cfg.get("web_session_secret") or "").strip()
    if not configured:
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
        MAX_CONTENT_LENGTH=int(
            cfg.get("web_max_request_bytes", 64 * 1024 * 1024)
        ),
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
    supplied = str(
        request.form.get("csrf_token")
        or request.headers.get("X-CSRF-Token")
        or ""
    )
    if (
        not expected
        or not supplied
        or not hmac.compare_digest(expected, supplied)
    ):
        abort(400, description="invalid CSRF token")


def admin_user():
    return str(session.get("admin_user") or "")


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin_authenticated"):
            return redirect(
                url_for(
                    "login", next=request.full_path.rstrip("?")
                )
            )
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
    if (
        not value
        or parsed.scheme
        or parsed.netloc
        or not value.startswith("/")
        or value.startswith("//")
    ):
        return fallback
    return value


def _normalize_ip(value):
    value = str(value or "").strip()
    if not value:
        return None
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address


def _trusted_proxy_networks(cfg):
    value = cfg.get("web_trusted_proxy_networks", [])
    if not isinstance(value, list):
        raise ValueError("web_trusted_proxy_networks must be a JSON array")
    if len(value) > 32:
        raise ValueError("web_trusted_proxy_networks exceeds 32 entries")
    networks = []
    for index, item in enumerate(value):
        try:
            network = ipaddress.ip_network(str(item), strict=True)
        except ValueError as exc:
            raise ValueError(
                f"web_trusted_proxy_networks[{index}] is not a canonical CIDR"
            ) from exc
        if network.prefixlen == 0:
            raise ValueError(
                f"web_trusted_proxy_networks[{index}] must not trust the entire internet"
            )
        networks.append(network)
    return tuple(networks)


def proxy_configuration_issues(cfg):
    issues = []
    enabled = cfg.get("web_trust_proxy_headers", False)
    if not isinstance(enabled, bool):
        return ("web_trust_proxy_headers must be a boolean",)
    if (
        bool(cfg.get("production_mode", False))
        and bool(cfg.get("https_reverse_proxy_acknowledged", False))
        and not enabled
    ):
        issues.append(
            "web_trust_proxy_headers must be true behind the production reverse proxy so throttling uses the verified client address"
        )
    if not enabled:
        return tuple(issues)
    try:
        networks = _trusted_proxy_networks(cfg)
        if not networks:
            issues.append("web_trusted_proxy_networks must contain the reverse proxy CIDR")
    except ValueError as exc:
        issues.append(str(exc))
    header = str(
        cfg.get("web_forwarded_for_header") or "X-Forwarded-For"
    ).strip()
    if header.lower() != "x-forwarded-for":
        issues.append("web_forwarded_for_header must be X-Forwarded-For")
    hops = cfg.get("web_max_forwarded_hops", 8)
    if isinstance(hops, bool) or not isinstance(hops, int) or not 1 <= hops <= 32:
        issues.append("web_max_forwarded_hops must be an integer from 1 through 32")
    return tuple(issues)


def peer_address():
    address = _normalize_ip(request.remote_addr)
    return address.compressed if address is not None else "unknown"


def remote_address(cfg=None):
    peer = _normalize_ip(request.remote_addr)
    if peer is None:
        return "unknown"
    if not cfg or not bool(cfg.get("web_trust_proxy_headers", False)):
        return peer.compressed
    if proxy_configuration_issues(cfg):
        return peer.compressed
    networks = _trusted_proxy_networks(cfg)
    if not any(peer in network for network in networks):
        return peer.compressed
    header_name = str(
        cfg.get("web_forwarded_for_header") or "X-Forwarded-For"
    ).strip()
    header = str(request.headers.get(header_name) or "")
    if not header or len(header) > 2048:
        return peer.compressed
    parts = [part.strip() for part in header.split(",")]
    max_hops = int(cfg.get("web_max_forwarded_hops", 8))
    if not parts or len(parts) > max_hops:
        return peer.compressed
    forwarded = []
    for part in parts:
        address = _normalize_ip(part)
        if address is None:
            return peer.compressed
        forwarded.append(address)

    candidate = peer
    for address in reversed(forwarded):
        if not any(candidate in network for network in networks):
            break
        candidate = address
    return candidate.compressed


def add_security_headers(response, cfg):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=(), payment=()",
    )
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; form-action 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; object-src 'none'",
    )
    if bool(cfg.get("web_hsts_enabled", True)):
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )
    if request.path.startswith("/api/") or request.path in {"/", "/login"}:
        response.headers.setdefault("Cache-Control", "no-store")
    return response