import json
import os
import re
import stat
import tempfile
from pathlib import Path


SECRET_REFERENCE_PREFIXES = ("env://", "systemd://", "file://")
SECRET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
DEFAULT_MAX_SECRET_BYTES = 64 * 1024
PLACEHOLDERS = {"", "CHANGE_ME", "REPLACE_ME", "CHANGEME", "TODO"}


class ConfigLoadError(ValueError):
    pass


class SecretStoreError(ValueError):
    pass


class RuntimeConfig(dict):
    """A resolved config mapping with non-serializable secret-source evidence."""

    def __init__(self, *args, secret_sources=None, source_path=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.secret_sources = dict(secret_sources or {})
        self.source_path = Path(source_path).resolve() if source_path else None

    def copy(self):
        return RuntimeConfig(
            self,
            secret_sources=self.secret_sources,
            source_path=self.source_path,
        )


def _text(value):
    return str(value or "")


def _strip_one_line_ending(value):
    if value.endswith("\r\n"):
        return value[:-2]
    if value.endswith("\n"):
        return value[:-1]
    return value


def _validate_secret_text(value, *, field, max_bytes):
    if "\x00" in value:
        raise SecretStoreError(f"{field} contains a NUL byte")
    size = len(value.encode("utf-8"))
    if size > int(max_bytes):
        raise SecretStoreError(f"{field} exceeds {int(max_bytes)} bytes")
    if value == "":
        raise SecretStoreError(f"{field} resolved to an empty secret")
    return value


def _read_secret_file(path, *, field, max_bytes):
    path = Path(path)
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise SecretStoreError(f"{field} secret file does not exist: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise SecretStoreError(f"{field} secret file must not be a symbolic link")
    if not stat.S_ISREG(metadata.st_mode):
        raise SecretStoreError(f"{field} secret file must be a regular file")
    if metadata.st_size > int(max_bytes):
        raise SecretStoreError(f"{field} secret file exceeds {int(max_bytes)} bytes")
    if os.name == "posix":
        if metadata.st_uid not in {0, os.getuid()}:
            raise SecretStoreError(f"{field} secret file has an unexpected owner")
        if metadata.st_mode & 0o077:
            raise SecretStoreError(
                f"{field} secret file permissions must not grant group or other access"
            )
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SecretStoreError(f"could not read {field} secret file: {path}") from exc
    if len(raw) > int(max_bytes):
        raise SecretStoreError(f"{field} secret file exceeds {int(max_bytes)} bytes")
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SecretStoreError(f"{field} secret file must contain UTF-8 text") from exc
    return _validate_secret_text(
        _strip_one_line_ending(value),
        field=field,
        max_bytes=max_bytes,
    )


def is_secret_reference(value):
    return isinstance(value, str) and value.startswith(SECRET_REFERENCE_PREFIXES)


def resolve_secret_reference(
    value,
    *,
    field="secret",
    environ=None,
    max_bytes=DEFAULT_MAX_SECRET_BYTES,
):
    if not is_secret_reference(value):
        return value, None
    environ = os.environ if environ is None else environ
    reference = str(value)
    if reference.startswith("env://"):
        name = reference[len("env://") :]
        if not SECRET_NAME_RE.fullmatch(name):
            raise SecretStoreError(f"{field} has an invalid environment secret name")
        if name not in environ:
            raise SecretStoreError(f"{field} environment secret is unavailable: {name}")
        resolved = _validate_secret_text(
            _text(environ[name]),
            field=field,
            max_bytes=max_bytes,
        )
        return resolved, reference

    if reference.startswith("systemd://"):
        name = reference[len("systemd://") :]
        if not SECRET_NAME_RE.fullmatch(name):
            raise SecretStoreError(f"{field} has an invalid systemd credential name")
        directory = _text(
            environ.get("FACE_ATTENDANCE_CREDENTIALS_DIRECTORY")
            or environ.get("CREDENTIALS_DIRECTORY")
        ).strip()
        if not directory:
            raise SecretStoreError(
                f"{field} requires CREDENTIALS_DIRECTORY or "
                "FACE_ATTENDANCE_CREDENTIALS_DIRECTORY"
            )
        resolved = _read_secret_file(
            Path(directory) / name,
            field=field,
            max_bytes=max_bytes,
        )
        return resolved, reference

    path_text = reference[len("file://") :]
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        raise SecretStoreError(f"{field} file secret path must be absolute")
    resolved = _read_secret_file(path, field=field, max_bytes=max_bytes)
    return resolved, reference


def resolve_config_secrets(document, *, environ=None):
    if not isinstance(document, dict):
        raise ConfigLoadError("config must contain a JSON object")
    sources = {}

    def visit(value, path):
        field = ".".join(path) or "config"
        if isinstance(value, dict):
            return {
                str(key): visit(item, path + (str(key),))
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [visit(item, path + (str(index),)) for index, item in enumerate(value)]
        resolved, source = resolve_secret_reference(
            value,
            field=field,
            environ=environ,
        )
        if source:
            sources[field] = source
        return resolved

    resolved = visit(document, ())
    return RuntimeConfig(resolved, secret_sources=sources)


def load_runtime_config(path, *, environ=None):
    path = Path(path)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigLoadError(f"missing config: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigLoadError(f"invalid JSON in {path}: {exc}") from exc
    except OSError as exc:
        raise ConfigLoadError(f"could not read config: {path}") from exc
    try:
        config = resolve_config_secrets(document, environ=environ)
    except SecretStoreError as exc:
        raise ConfigLoadError(f"invalid secret configuration in {path}: {exc}") from exc
    config.source_path = path.resolve()
    return config


def load_config_document(path):
    path = Path(path)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigLoadError(f"missing config: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigLoadError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ConfigLoadError(f"config must contain a JSON object: {path}")
    return document


def write_secret_file_atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(path.parent, 0o700)
    text = _validate_secret_text(
        str(value),
        field=path.name or "secret",
        max_bytes=DEFAULT_MAX_SECRET_BYTES,
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return path


def secret_source_map(cfg):
    return dict(getattr(cfg, "secret_sources", {}) or {})


def _configured_secret_values(cfg):
    paths = []
    for key in (
        "central_api_token",
        "embedding_export_token",
        "web_admin_password_hash",
        "web_session_secret",
        "pad_http_token",
        "frappe_api_secret",
        "camera_source_receipt_secret",
    ):
        value = cfg.get(key)
        if isinstance(value, str) and value.strip().upper() not in PLACEHOLDERS:
            paths.append((key, value))

    for group in ("central_api_credentials", "embedding_export_credentials"):
        items = cfg.get(group)
        if isinstance(items, dict):
            for credential_id, item in items.items():
                if not isinstance(item, dict):
                    continue
                value = item.get("token")
                if isinstance(value, str) and value.strip().upper() not in PLACEHOLDERS:
                    paths.append((f"{group}.{credential_id}.token", value))

    users = cfg.get("ftp_users")
    if isinstance(users, dict):
        for username, item in users.items():
            if not isinstance(item, dict):
                continue
            value = item.get("password")
            if isinstance(value, str) and value.strip().upper() not in PLACEHOLDERS:
                paths.append((f"ftp_users.{username}.password", value))
    return paths


def external_secret_configuration_issues(cfg):
    if not bool(cfg.get("production_mode", False)):
        return ()
    if not bool(cfg.get("production_external_secrets_required", True)):
        return (
            "production_external_secrets_required must remain true in production",
        )
    sources = secret_source_map(cfg)
    issues = []
    for path, _value in _configured_secret_values(cfg):
        if path not in sources:
            issues.append(
                f"{path} must be delivered through env://, systemd://, or file:// in production"
            )
    return tuple(issues)
