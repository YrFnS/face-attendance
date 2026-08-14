import hashlib
import hmac
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone


CREDENTIAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
PLACEHOLDERS = {"", "CHANGE_ME", "REPLACE_ME", "CHANGEME", "TODO"}
ALLOWED_SCOPES = frozenset({"gallery:read"})
MAX_CREDENTIALS = 64
MAX_SCOPE_VALUES = 64


class GalleryCredentialError(ValueError):
    pass


def _now():
    return datetime.now(timezone.utc)


def _text(value, field, *, required=False, max_chars=256):
    if value is None:
        text = ""
    elif not isinstance(value, str):
        raise GalleryCredentialError(f"{field} must be a string")
    else:
        text = unicodedata.normalize("NFC", value)
    if text != text.strip():
        raise GalleryCredentialError(f"{field} must not contain surrounding whitespace")
    if required and not text:
        raise GalleryCredentialError(f"{field} is required")
    if len(text) > int(max_chars):
        raise GalleryCredentialError(f"{field} exceeds {int(max_chars)} characters")
    for character in text:
        if unicodedata.category(character) in {"Cc", "Cf", "Cs"}:
            raise GalleryCredentialError(
                f"{field} contains a control or formatting character"
            )
    return text


def _is_placeholder(value):
    return str(value or "").strip().upper() in PLACEHOLDERS


def _timestamp(value, field):
    text = _text(value, field, required=False, max_chars=64)
    if not text:
        return None
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise GalleryCredentialError(f"{field} must be an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise GalleryCredentialError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _scope_list(value, field, *, required):
    if value in (None, ""):
        value = []
    if not isinstance(value, list):
        raise GalleryCredentialError(f"{field} must be a JSON array")
    if len(value) > MAX_SCOPE_VALUES:
        raise GalleryCredentialError(f"{field} exceeds {MAX_SCOPE_VALUES} values")
    result = []
    seen = set()
    for index, item in enumerate(value):
        text = _text(
            item,
            f"{field}[{index}]",
            required=True,
            max_chars=128,
        )
        if _is_placeholder(text):
            raise GalleryCredentialError(f"{field}[{index}] must not be a placeholder")
        if text in seen:
            raise GalleryCredentialError(f"{field} contains duplicate value {text!r}")
        seen.add(text)
        result.append(text)
    if required and not result:
        raise GalleryCredentialError(f"{field} must contain at least one value")
    return tuple(result)


@dataclass(frozen=True)
class GalleryCredential:
    credential_id: str
    token: str
    scopes: tuple[str, ...]
    branches: tuple[str, ...]
    models: tuple[str, ...]
    model_versions: tuple[str, ...]
    not_before: datetime | None = None
    expires_at: datetime | None = None
    enabled: bool = True
    legacy: bool = False

    @property
    def fingerprint(self):
        return hashlib.sha256(self.token.encode("utf-8")).hexdigest()[:16]

    def active(self, now=None):
        now = now or _now()
        if not self.enabled:
            return False
        if self.not_before and now < self.not_before:
            return False
        if self.expires_at and now >= self.expires_at:
            return False
        return True

    def allows(self, *, branch, model, model_version):
        return (
            "gallery:read" in self.scopes
            and (not self.branches or branch in self.branches)
            and (not self.models or model in self.models)
            and (not self.model_versions or model_version in self.model_versions)
        )


def _parse_credential(credential_id, item, *, production, field):
    credential_id = _text(
        credential_id,
        f"{field} credential ID",
        required=True,
        max_chars=128,
    )
    if not CREDENTIAL_ID_RE.fullmatch(credential_id):
        raise GalleryCredentialError(
            f"{field} credential ID {credential_id!r} has an invalid format"
        )
    if not isinstance(item, dict):
        raise GalleryCredentialError(f"{field}.{credential_id} must be an object")
    allowed = {
        "token",
        "scopes",
        "branches",
        "models",
        "model_versions",
        "not_before",
        "expires_at",
        "enabled",
    }
    unknown = sorted(set(item) - allowed)
    if unknown:
        raise GalleryCredentialError(
            f"{field}.{credential_id} contains unknown fields: {', '.join(unknown)}"
        )
    token = _text(
        item.get("token"),
        f"{field}.{credential_id}.token",
        required=True,
        max_chars=1024,
    )
    minimum = 32 if production else 16
    if _is_placeholder(token) or len(token.encode("utf-8")) < minimum:
        raise GalleryCredentialError(
            f"{field}.{credential_id}.token must contain at least {minimum} UTF-8 bytes"
        )
    scopes = _scope_list(
        item.get("scopes"),
        f"{field}.{credential_id}.scopes",
        required=True,
    )
    unsupported = sorted(set(scopes) - ALLOWED_SCOPES)
    if unsupported:
        raise GalleryCredentialError(
            f"{field}.{credential_id}.scopes contains unsupported scopes: "
            + ", ".join(unsupported)
        )
    branches = _scope_list(
        item.get("branches"),
        f"{field}.{credential_id}.branches",
        required=production,
    )
    models = _scope_list(
        item.get("models"),
        f"{field}.{credential_id}.models",
        required=production,
    )
    model_versions = _scope_list(
        item.get("model_versions"),
        f"{field}.{credential_id}.model_versions",
        required=production,
    )
    enabled = item.get("enabled", True)
    if not isinstance(enabled, bool):
        raise GalleryCredentialError(f"{field}.{credential_id}.enabled must be a boolean")
    not_before = _timestamp(
        item.get("not_before"),
        f"{field}.{credential_id}.not_before",
    )
    expires_at = _timestamp(
        item.get("expires_at"),
        f"{field}.{credential_id}.expires_at",
    )
    if not_before and expires_at and expires_at <= not_before:
        raise GalleryCredentialError(
            f"{field}.{credential_id}.expires_at must be later than not_before"
        )
    return GalleryCredential(
        credential_id=credential_id,
        token=token,
        scopes=scopes,
        branches=branches,
        models=models,
        model_versions=model_versions,
        not_before=not_before,
        expires_at=expires_at,
        enabled=enabled,
    )


def _credential_set(cfg, field):
    value = cfg.get(field)
    if value in (None, {}):
        return {}
    if not isinstance(value, dict):
        raise GalleryCredentialError(f"{field} must be a JSON object")
    if len(value) > MAX_CREDENTIALS:
        raise GalleryCredentialError(f"{field} exceeds {MAX_CREDENTIALS} credentials")
    production = bool(cfg.get("production_mode", False))
    parsed = {}
    fingerprints = {}
    for credential_id, item in value.items():
        credential = _parse_credential(
            credential_id,
            item,
            production=production,
            field=field,
        )
        fingerprint = hashlib.sha256(credential.token.encode("utf-8")).digest()
        if fingerprint in fingerprints:
            raise GalleryCredentialError(
                f"{field} reuses one token for {fingerprints[fingerprint]!r} "
                f"and {credential.credential_id!r}"
            )
        fingerprints[fingerprint] = credential.credential_id
        parsed[credential.credential_id] = credential
    return parsed


def _context(cfg):
    return {
        "branch": _text(cfg.get("branch_name"), "branch_name", required=False, max_chars=128),
        "model": _text(cfg.get("model"), "model", required=False, max_chars=128),
        "model_version": _text(
            cfg.get("model_version"),
            "model_version",
            required=False,
            max_chars=128,
        ),
    }


def _legacy_credential(cfg, field, *, credential_id):
    token = _text(cfg.get(field), field, required=False, max_chars=1024)
    if _is_placeholder(token):
        raise GalleryCredentialError(
            f"{field} must be a non-placeholder value"
        )
    if bool(cfg.get("production_mode", False)):
        raise GalleryCredentialError(
            "legacy single-token gallery credentials are not allowed in production"
        )
    if len(token.encode("utf-8")) < 16:
        raise GalleryCredentialError(f"{field} must contain at least 16 UTF-8 bytes")
    return GalleryCredential(
        credential_id=credential_id,
        token=token,
        scopes=("gallery:read",),
        branches=(),
        models=(),
        model_versions=(),
        legacy=True,
    )


def outbound_gallery_credential(cfg, *, now=None):
    credentials = _credential_set(cfg, "central_api_credentials")
    if credentials:
        selected = _text(
            cfg.get("central_api_credential_id"),
            "central_api_credential_id",
            required=True,
            max_chars=128,
        )
        credential = credentials.get(selected)
        if credential is None:
            raise GalleryCredentialError(
                "central_api_credential_id does not identify a configured credential"
            )
    else:
        credential = _legacy_credential(
            cfg,
            "central_api_token",
            credential_id="legacy-central-token",
        )
    context = _context(cfg)
    if not credential.active(now):
        raise GalleryCredentialError(
            f"central gallery credential {credential.credential_id!r} is disabled or outside its validity window"
        )
    if not credential.allows(**context):
        raise GalleryCredentialError(
            f"central gallery credential {credential.credential_id!r} is not scoped for "
            f"branch={context['branch']!r}, model={context['model']!r}, "
            f"model_version={context['model_version']!r}"
        )
    return credential


def _bearer_token(value):
    value = _text(value, "Authorization header", required=False, max_chars=1200)
    if not value.startswith("Bearer "):
        raise GalleryCredentialError("invalid gallery credential")
    token = value[len("Bearer ") :]
    if not token or token != token.strip():
        raise GalleryCredentialError("invalid gallery credential")
    return token


def authenticate_export_credential(
    cfg,
    authorization,
    credential_id,
    *,
    branch,
    model,
    model_version,
    now=None,
):
    credentials = _credential_set(cfg, "embedding_export_credentials")
    supplied_token = _bearer_token(authorization)
    if credentials:
        credential_id = _text(
            credential_id,
            "X-Face-Attendance-Credential-ID",
            required=True,
            max_chars=128,
        )
        if not CREDENTIAL_ID_RE.fullmatch(credential_id):
            raise GalleryCredentialError("invalid gallery credential")
        credential = credentials.get(credential_id)
        if credential is None or not hmac.compare_digest(
            supplied_token,
            credential.token,
        ):
            raise GalleryCredentialError("invalid gallery credential")
    else:
        credential = _legacy_credential(
            cfg,
            "embedding_export_token",
            credential_id="legacy-export-token",
        )
        if not hmac.compare_digest(supplied_token, credential.token):
            raise GalleryCredentialError("invalid gallery credential")
    if not credential.active(now):
        raise GalleryCredentialError("gallery credential is disabled or expired")
    if not credential.allows(
        branch=str(branch or ""),
        model=str(model or ""),
        model_version=str(model_version or ""),
    ):
        raise GalleryCredentialError("gallery credential is outside its configured scope")
    return credential


def gallery_credential_configuration_issues(cfg, *, now=None):
    issues = []
    if bool(cfg.get("embedding_sync_enabled", True)) and str(
        cfg.get("central_url") or ""
    ).strip():
        try:
            outbound_gallery_credential(cfg, now=now)
        except GalleryCredentialError as exc:
            issues.append(str(exc))

    if bool(cfg.get("embedding_export_enabled", False)):
        try:
            credentials = _credential_set(cfg, "embedding_export_credentials")
            if not credentials:
                _legacy_credential(
                    cfg,
                    "embedding_export_token",
                    credential_id="legacy-export-token",
                )
            else:
                context = _context(cfg)
                active = [
                    credential
                    for credential in credentials.values()
                    if credential.active(now) and credential.allows(**context)
                ]
                if not active:
                    raise GalleryCredentialError(
                        "embedding_export_credentials has no active credential for the configured branch/model/version"
                    )
        except GalleryCredentialError as exc:
            issues.append(str(exc))
    return tuple(issues)
