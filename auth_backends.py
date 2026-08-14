import importlib
import re
import secrets
import unicodedata
from dataclasses import dataclass
from urllib.parse import urlparse

from web_security import (
    is_placeholder,
    password_hash_issues,
    verify_password,
)


ADAPTER_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_.]{0,190}:[A-Za-z_][A-Za-z0-9_]{0,63}$"
)
HOST_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")


class AuthBackendError(ValueError):
    pass


@dataclass(frozen=True)
class AuthPrincipal:
    subject: str
    display_name: str = ""
    mfa: bool = False
    assurance: str = ""


def _text(value, field, *, required=False, max_chars=256):
    if value is None:
        text = ""
    elif not isinstance(value, str):
        raise AuthBackendError(f"{field} must be a string")
    else:
        text = unicodedata.normalize("NFC", value)
    if text != text.strip():
        raise AuthBackendError(f"{field} must not contain surrounding whitespace")
    if required and not text:
        raise AuthBackendError(f"{field} is required")
    if len(text) > int(max_chars):
        raise AuthBackendError(f"{field} exceeds {int(max_chars)} characters")
    for character in text:
        if unicodedata.category(character) in {"Cc", "Cf", "Cs"}:
            raise AuthBackendError(
                f"{field} contains a control or formatting character"
            )
    return text


def auth_mode(cfg):
    mode = _text(
        cfg.get("web_auth_mode") or "local",
        "web_auth_mode",
        required=True,
        max_chars=32,
    ).lower()
    if mode not in {"local", "adapter"}:
        raise AuthBackendError("web_auth_mode must be local or adapter")
    return mode


def _session_secret_issues(cfg):
    secret = str(cfg.get("web_session_secret") or "").strip()
    if len(secret) < 32 or is_placeholder(secret):
        return (
            "web_session_secret must be a persistent non-placeholder value of at least 32 characters",
        )
    return ()


def _allowed_redirect_hosts(cfg):
    value = cfg.get("web_auth_allowed_redirect_hosts", [])
    if not isinstance(value, list):
        raise AuthBackendError("web_auth_allowed_redirect_hosts must be a JSON array")
    if len(value) > 32:
        raise AuthBackendError("web_auth_allowed_redirect_hosts exceeds 32 entries")
    hosts = []
    seen = set()
    for index, item in enumerate(value):
        host = _text(
            item,
            f"web_auth_allowed_redirect_hosts[{index}]",
            required=True,
            max_chars=253,
        ).lower()
        if not HOST_RE.fullmatch(host) or host.startswith(".") or host.endswith("."):
            raise AuthBackendError(
                f"web_auth_allowed_redirect_hosts[{index}] has an invalid hostname"
            )
        if host in seen:
            raise AuthBackendError(
                f"web_auth_allowed_redirect_hosts contains duplicate host {host!r}"
            )
        seen.add(host)
        hosts.append(host)
    return tuple(hosts)


def load_external_adapter(cfg):
    spec = _text(
        cfg.get("web_auth_adapter"),
        "web_auth_adapter",
        required=True,
        max_chars=256,
    )
    if not ADAPTER_RE.fullmatch(spec):
        raise AuthBackendError(
            "web_auth_adapter must use the form package.module:factory"
        )
    module_name, factory_name = spec.split(":", 1)
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        raise AuthBackendError(f"could not import web auth adapter {module_name!r}") from exc
    factory = getattr(module, factory_name, None)
    if not callable(factory):
        raise AuthBackendError(f"web auth adapter factory {spec!r} is not callable")
    try:
        adapter = factory(cfg)
    except Exception as exc:
        raise AuthBackendError(f"web auth adapter factory {spec!r} failed") from exc
    if not callable(getattr(adapter, "begin_login", None)):
        raise AuthBackendError("web auth adapter must implement begin_login")
    if not callable(getattr(adapter, "complete_login", None)):
        raise AuthBackendError("web auth adapter must implement complete_login")
    return adapter


def auth_configuration_issues(cfg):
    issues = list(_session_secret_issues(cfg))
    try:
        mode = auth_mode(cfg)
    except AuthBackendError as exc:
        return tuple(issues + [str(exc)])

    if mode == "local":
        username = str(cfg.get("web_admin_username") or "").strip()
        if not username:
            issues.append("web_admin_username is not configured")
        issues.extend(password_hash_issues(cfg.get("web_admin_password_hash")))
        if bool(cfg.get("web_mfa_required", False)):
            issues.append(
                "web_mfa_required cannot be satisfied by the local password backend; configure web_auth_mode=adapter"
            )
        return tuple(issues)

    try:
        hosts = _allowed_redirect_hosts(cfg)
        if not hosts:
            raise AuthBackendError(
                "web_auth_allowed_redirect_hosts must contain the approved identity-provider host"
            )
        adapter = load_external_adapter(cfg)
        adapter_issues = getattr(adapter, "configuration_issues", None)
        if callable(adapter_issues):
            for item in adapter_issues() or ():
                issues.append(str(item))
        if bool(cfg.get("web_mfa_required", False)) and not bool(
            getattr(adapter, "supports_mfa", False)
        ):
            issues.append(
                "the configured web auth adapter does not declare supports_mfa=true"
            )
    except AuthBackendError as exc:
        issues.append(str(exc))
    return tuple(issues)


def auth_configured(cfg):
    return not auth_configuration_issues(cfg)


def authenticate_local(cfg, username, password):
    if auth_mode(cfg) != "local":
        raise AuthBackendError("local password authentication is disabled")
    expected = str(cfg.get("web_admin_username") or "")
    supplied = str(username or "")
    valid = secrets.compare_digest(supplied, expected) and verify_password(
        password,
        cfg.get("web_admin_password_hash"),
    )
    if not valid:
        return None
    return AuthPrincipal(
        subject=expected,
        display_name=expected,
        mfa=False,
        assurance="password",
    )


def _validate_redirect_url(cfg, value):
    url = _text(value, "external login redirect URL", required=True, max_chars=4096)
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise AuthBackendError("external login redirect URL must be absolute HTTP(S)")
    if parsed.username or parsed.password:
        raise AuthBackendError("external login redirect URL must not contain credentials")
    host = (parsed.hostname or "").lower()
    if host not in _allowed_redirect_hosts(cfg):
        raise AuthBackendError("external login redirect host is not allowlisted")
    local = host in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and (bool(cfg.get("production_mode", False)) or not local):
        raise AuthBackendError("external login redirect must use HTTPS")
    return url


def begin_external_login(cfg, *, next_url, state, callback_url):
    if auth_mode(cfg) != "adapter":
        raise AuthBackendError("external authentication is not configured")
    adapter = load_external_adapter(cfg)
    try:
        redirect_url = adapter.begin_login(
            next_url=str(next_url),
            state=str(state),
            callback_url=str(callback_url),
        )
    except Exception as exc:
        raise AuthBackendError("external auth adapter could not begin login") from exc
    return _validate_redirect_url(cfg, redirect_url)


def _principal(value):
    if isinstance(value, AuthPrincipal):
        principal = value
    elif isinstance(value, dict):
        principal = AuthPrincipal(
            subject=value.get("subject", ""),
            display_name=value.get("display_name", ""),
            mfa=value.get("mfa", False),
            assurance=value.get("assurance", ""),
        )
    else:
        raise AuthBackendError("external auth adapter returned an invalid principal")
    subject = _text(
        principal.subject,
        "external principal subject",
        required=True,
        max_chars=128,
    )
    display_name = _text(
        principal.display_name,
        "external principal display_name",
        required=False,
        max_chars=256,
    )
    assurance = _text(
        principal.assurance,
        "external principal assurance",
        required=False,
        max_chars=128,
    )
    if not isinstance(principal.mfa, bool):
        raise AuthBackendError("external principal mfa must be a boolean")
    return AuthPrincipal(
        subject=subject,
        display_name=display_name,
        mfa=principal.mfa,
        assurance=assurance,
    )


def complete_external_login(
    cfg,
    *,
    query,
    expected_state,
    callback_url,
):
    if auth_mode(cfg) != "adapter":
        raise AuthBackendError("external authentication is not configured")
    supplied_state = str((query or {}).get("state") or "")
    if not expected_state or not supplied_state or not secrets.compare_digest(
        str(expected_state),
        supplied_state,
    ):
        raise AuthBackendError("external login state validation failed")
    adapter = load_external_adapter(cfg)
    try:
        value = adapter.complete_login(
            query=dict(query or {}),
            expected_state=str(expected_state),
            callback_url=str(callback_url),
        )
    except Exception as exc:
        raise AuthBackendError("external auth adapter could not complete login") from exc
    principal = _principal(value)
    if bool(cfg.get("web_mfa_required", False)) and not principal.mfa:
        raise AuthBackendError("external login did not satisfy the required MFA policy")
    return principal
