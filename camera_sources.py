import hashlib
import hmac
import ipaddress
import json
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


PLACEHOLDERS = {"", "CHANGE_ME", "REPLACE_ME", "CHANGEME", "TODO"}
CAMERA_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
FTP_USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
RECEIPT_SUFFIX = ".source.json"
RECEIPT_SCHEMA_VERSION = 1
MAX_RECEIPT_BYTES = 32 * 1024
FTP_UPLOAD_ONLY_PERMISSIONS = frozenset("elw")


class CameraSourceError(ValueError):
    pass


@dataclass(frozen=True)
class CameraSource:
    camera_id: str
    source_type: str
    branch: str
    policy: str
    ftp_username: str
    upload_dir: Path
    upload_route: str
    allowed_networks: tuple
    binding_id: str

    def allows_ip(self, value):
        try:
            address = normalize_ip(value)
        except CameraSourceError:
            return False
        return any(address in network for network in self.allowed_networks)


@dataclass(frozen=True)
class SourceReceipt:
    camera_id: str
    source_type: str
    branch: str
    policy: str
    ftp_username: str
    remote_ip: str
    received_at: str
    source_sha256: str
    source_size: int
    source_binding_id: str
    signature: str
    verified: bool


def _text(value, field, *, required=True, max_chars=256, pattern=None):
    if value is None:
        text = ""
    elif not isinstance(value, str):
        raise CameraSourceError(f"{field} must be a string")
    else:
        text = unicodedata.normalize("NFC", value)
    if text != text.strip():
        raise CameraSourceError(f"{field} must not contain surrounding whitespace")
    if required and not text:
        raise CameraSourceError(f"{field} is required")
    if len(text) > int(max_chars):
        raise CameraSourceError(f"{field} exceeds {int(max_chars)} characters")
    for character in text:
        if unicodedata.category(character) in {"Cc", "Cf", "Cs"}:
            raise CameraSourceError(f"{field} contains a control or formatting character")
    if text and pattern is not None and not pattern.fullmatch(text):
        raise CameraSourceError(f"{field} has an invalid format")
    return text


def _is_placeholder(value):
    return str(value or "").strip().upper() in PLACEHOLDERS


def _strict_json_object(raw, label):
    if not isinstance(raw, dict):
        raise CameraSourceError(f"{label} must be a JSON object")
    return raw


def _strict_positive_int(value, field, *, maximum=(1 << 31) - 1):
    if isinstance(value, bool) or not isinstance(value, int):
        raise CameraSourceError(f"{field} must be an integer")
    if value < 0 or value > int(maximum):
        raise CameraSourceError(f"{field} must be between 0 and {int(maximum)}")
    return value


def _resolve(root, value, field):
    text = _text(value, field, max_chars=4096)
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = Path(root) / path
    lexical = Path(os.path.abspath(path))
    cursor = lexical
    while True:
        if cursor.exists() and cursor.is_symlink():
            raise CameraSourceError(f"{field} must not use a symbolic-link path")
        if cursor == cursor.parent:
            break
        cursor = cursor.parent
    return lexical.resolve(strict=False)


def uploads_root(cfg, root):
    value = cfg.get("camera_uploads_dir")
    if value in (None, ""):
        value = Path(root) / "camera_uploads"
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path(root) / path
    return path.resolve(strict=False)


def _relative_route(target, root):
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise CameraSourceError(
            f"camera upload_dir must remain under camera_uploads_dir: {target}"
        ) from exc
    if not relative.parts:
        raise CameraSourceError(
            "each camera upload_dir must be a dedicated subdirectory of camera_uploads_dir"
        )
    if ".incoming" in relative.parts:
        raise CameraSourceError("camera upload_dir must not include .incoming")
    return relative.as_posix()


def normalize_ip(value):
    text = _text(value, "source IP", max_chars=64)
    try:
        address = ipaddress.ip_address(text)
    except ValueError as exc:
        raise CameraSourceError(f"invalid source IP address: {text}") from exc
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address


def _allowed_networks(value, field):
    if not isinstance(value, list) or not value:
        raise CameraSourceError(f"{field} must contain at least one CIDR network")
    networks = []
    seen = set()
    for index, item in enumerate(value):
        text = _text(item, f"{field}[{index}]", max_chars=64)
        try:
            network = ipaddress.ip_network(text, strict=False)
        except ValueError as exc:
            raise CameraSourceError(f"{field}[{index}] is not a valid CIDR network") from exc
        if network.prefixlen == 0:
            raise CameraSourceError(f"{field}[{index}] must not allow the entire internet")
        key = (network.version, network.network_address.compressed, network.prefixlen)
        if key in seen:
            raise CameraSourceError(f"{field} contains a duplicate network: {network}")
        seen.add(key)
        networks.append(network)
    return tuple(networks)


def _source_binding_descriptor(
    *,
    camera_id,
    source_type,
    branch,
    policy,
    ftp_username,
    upload_route,
    allowed_networks,
):
    return {
        "schema_version": 1,
        "camera_id": camera_id,
        "source_type": source_type,
        "branch": branch,
        "policy": policy,
        "ftp_username": ftp_username,
        "upload_route": upload_route,
        "allowed_networks": [str(item) for item in allowed_networks],
    }


def _canonical_json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _binding_id(descriptor):
    return hashlib.sha256(_canonical_json(descriptor)).hexdigest()


def _password(value, username):
    password = _text(value, f"FTP password for {username}", max_chars=1024)
    if _is_placeholder(password):
        raise CameraSourceError(
            f"FTP password for {username} is missing or still a placeholder"
        )
    if len(password.encode("utf-8")) < 16:
        raise CameraSourceError(
            f"FTP password for {username} must contain at least 16 UTF-8 bytes"
        )
    return password


def _permissions(value, username, default="elw"):
    permissions = _text(
        value if value not in (None, "") else default,
        f"FTP permissions for {username}",
        max_chars=16,
    )
    if "w" not in permissions:
        raise CameraSourceError(
            f"FTP permissions for {username} must include upload permission 'w'"
        )
    unsupported = sorted(set(permissions) - FTP_UPLOAD_ONLY_PERMISSIONS)
    if unsupported:
        raise CameraSourceError(
            f"FTP permissions for {username} grant unsupported permissions: "
            f"{''.join(unsupported)}"
        )
    return permissions


def load_camera_sources(cfg, root):
    if not isinstance(cfg, dict):
        raise CameraSourceError("config must be a JSON object")
    root = Path(root).resolve(strict=False)
    upload_root = uploads_root(cfg, root)
    raw_sources = cfg.get("camera_sources")
    if not isinstance(raw_sources, dict) or not raw_sources:
        raise CameraSourceError(
            "camera_sources must define at least one explicitly bound camera"
        )
    raw_users = cfg.get("ftp_users")
    if not isinstance(raw_users, dict) or not raw_users:
        raise CameraSourceError(
            "ftp_users must define one unique credential for each camera source"
        )

    configured_branch = _text(
        cfg.get("branch_name"), "branch_name", required=False, max_chars=128
    )
    default_permissions = _text(
        cfg.get("ftp_permissions") or "elw", "ftp_permissions", max_chars=16
    )
    sources = []
    usernames = set()
    routes = []
    password_owners = {}

    for raw_camera_id, raw_item in raw_sources.items():
        camera_id = _text(
            raw_camera_id, "camera source ID", max_chars=128, pattern=CAMERA_ID_RE
        )
        item = _strict_json_object(raw_item, f"camera_sources.{camera_id}")
        allowed_fields = {
            "source_type",
            "branch",
            "policy",
            "ftp_username",
            "upload_dir",
            "allowed_networks",
        }
        unknown = sorted(set(item) - allowed_fields)
        if unknown:
            raise CameraSourceError(
                f"camera_sources.{camera_id} contains unknown fields: {', '.join(unknown)}"
            )
        source_type = _text(
            item.get("source_type"),
            f"camera_sources.{camera_id}.source_type",
            max_chars=64,
        ).lower()
        if source_type != "holowits_ftp":
            raise CameraSourceError(
                f"camera_sources.{camera_id}.source_type must be holowits_ftp"
            )
        branch = _text(
            item.get("branch"),
            f"camera_sources.{camera_id}.branch",
            max_chars=128,
        )
        if _is_placeholder(branch):
            raise CameraSourceError(
                f"camera_sources.{camera_id}.branch must not be a placeholder"
            )
        if configured_branch and branch != configured_branch:
            raise CameraSourceError(
                f"camera_sources.{camera_id}.branch {branch!r} does not match branch_name {configured_branch!r}"
            )
        policy = _text(
            item.get("policy"),
            f"camera_sources.{camera_id}.policy",
            max_chars=16,
        )
        if policy not in {"IN", "OUT"}:
            raise CameraSourceError(
                f"camera_sources.{camera_id}.policy must be IN or OUT"
            )
        username = _text(
            item.get("ftp_username"),
            f"camera_sources.{camera_id}.ftp_username",
            max_chars=64,
            pattern=FTP_USERNAME_RE,
        )
        if username in usernames:
            raise CameraSourceError(
                f"FTP username {username!r} is bound to more than one camera"
            )
        usernames.add(username)

        target = _resolve(
            root,
            item.get("upload_dir"),
            f"camera_sources.{camera_id}.upload_dir",
        )
        route = _relative_route(target, upload_root)
        if target.exists() and target.is_symlink():
            raise CameraSourceError(
                f"camera_sources.{camera_id}.upload_dir must not be a symbolic link"
            )
        for other_id, other_target in routes:
            if target == other_target:
                raise CameraSourceError(
                    f"camera sources {other_id!r} and {camera_id!r} share an upload route"
                )
            if target in other_target.parents or other_target in target.parents:
                raise CameraSourceError(
                    f"camera upload routes must not overlap: {other_id!r} and {camera_id!r}"
                )
        routes.append((camera_id, target))

        networks = _allowed_networks(
            item.get("allowed_networks"),
            f"camera_sources.{camera_id}.allowed_networks",
        )

        user_item = raw_users.get(username)
        if not isinstance(user_item, dict):
            raise CameraSourceError(
                f"ftp_users.{username} must exist and be a JSON object"
            )
        user_unknown = sorted(set(user_item) - {"password", "dir", "permissions"})
        if user_unknown:
            raise CameraSourceError(
                f"ftp_users.{username} contains unknown fields: {', '.join(user_unknown)}"
            )
        password = _password(user_item.get("password"), username)
        owner = password_owners.get(password)
        if owner:
            raise CameraSourceError(
                f"FTP credentials for {owner!r} and {username!r} reuse the same password"
            )
        password_owners[password] = username
        _permissions(user_item.get("permissions"), username, default_permissions)
        if user_item.get("dir") not in (None, ""):
            legacy_target = _resolve(root, user_item.get("dir"), f"ftp_users.{username}.dir")
            if legacy_target != target:
                raise CameraSourceError(
                    f"ftp_users.{username}.dir does not match the bound camera upload_dir"
                )

        descriptor = _source_binding_descriptor(
            camera_id=camera_id,
            source_type=source_type,
            branch=branch,
            policy=policy,
            ftp_username=username,
            upload_route=route,
            allowed_networks=networks,
        )
        sources.append(
            CameraSource(
                camera_id=camera_id,
                source_type=source_type,
                branch=branch,
                policy=policy,
                ftp_username=username,
                upload_dir=target,
                upload_route=route,
                allowed_networks=networks,
                binding_id=_binding_id(descriptor),
            )
        )

    unbound_users = sorted(set(raw_users) - usernames)
    if unbound_users:
        raise CameraSourceError(
            "every FTP credential must be bound to exactly one camera source; "
            f"unbound users: {', '.join(unbound_users)}"
        )
    return tuple(sorted(sources, key=lambda item: item.camera_id))


def camera_source_configuration_issues(cfg, root):
    issues = []
    try:
        load_camera_sources(cfg, root)
    except CameraSourceError as exc:
        issues.append(str(exc))
    required = bool(cfg.get("production_mode", False)) or bool(
        cfg.get("camera_source_receipt_required", True)
    )
    try:
        receipt_secret(cfg, required=required)
    except CameraSourceError as exc:
        issues.append(str(exc))
    if bool(cfg.get("production_mode", False)) and not bool(
        cfg.get("camera_source_receipt_required", True)
    ):
        issues.append("camera_source_receipt_required must be true in production")
    return issues


def source_by_username(sources, username):
    text = _text(username, "FTP username", max_chars=64, pattern=FTP_USERNAME_RE)
    matches = [item for item in sources if item.ftp_username == text]
    if len(matches) != 1:
        raise CameraSourceError(
            f"FTP username {text!r} is not bound to exactly one camera source"
        )
    return matches[0]


def source_for_upload_path(sources, path):
    target = Path(path).resolve(strict=False)
    matches = []
    for source in sources:
        try:
            target.relative_to(source.upload_dir)
            matches.append(source)
        except ValueError:
            continue
    if len(matches) != 1:
        raise CameraSourceError(
            f"upload path is not bound to exactly one camera source: {target}"
        )
    relative = target.relative_to(matches[0].upload_dir)
    if ".incoming" in relative.parts:
        raise CameraSourceError("watcher must not process a staging .incoming path")
    return matches[0]


def ftp_user_config(cfg, source):
    raw_users = cfg.get("ftp_users")
    if not isinstance(raw_users, dict):
        raise CameraSourceError("ftp_users must be a JSON object")
    item = raw_users.get(source.ftp_username)
    if not isinstance(item, dict):
        raise CameraSourceError(
            f"ftp_users.{source.ftp_username} must be a JSON object"
        )
    return {
        "password": _password(item.get("password"), source.ftp_username),
        "permissions": _permissions(
            item.get("permissions"),
            source.ftp_username,
            _text(cfg.get("ftp_permissions") or "elw", "ftp_permissions", max_chars=16),
        ),
    }


def receipt_secret(cfg, *, required=True):
    value = cfg.get("camera_source_receipt_secret")
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise CameraSourceError("camera_source_receipt_secret must be a string")
    if value != value.strip():
        raise CameraSourceError(
            "camera_source_receipt_secret must not contain surrounding whitespace"
        )
    if not value:
        if required:
            raise CameraSourceError("camera_source_receipt_secret is required")
        return b""
    if _is_placeholder(value):
        raise CameraSourceError(
            "camera_source_receipt_secret must not be a placeholder"
        )
    encoded = value.encode("utf-8")
    if len(encoded) < 32:
        raise CameraSourceError(
            "camera_source_receipt_secret must contain at least 32 UTF-8 bytes"
        )
    if len(encoded) > 1024:
        raise CameraSourceError(
            "camera_source_receipt_secret exceeds 1024 UTF-8 bytes"
        )
    return encoded


def receipt_path(image_path):
    image = Path(image_path)
    return image.with_name(image.name + RECEIPT_SUFFIX)


def _timestamp(value, field="received_at"):
    text = _text(value, field, max_chars=64)
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise CameraSourceError(f"{field} must be a valid RFC 3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CameraSourceError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _receipt_payload(source, *, remote_ip, received_at, source_sha256, source_size):
    digest = _text(source_sha256, "source_sha256", max_chars=64, pattern=HEX64_RE)
    size = _strict_positive_int(source_size, "source_size")
    address = normalize_ip(remote_ip)
    if not source.allows_ip(address.compressed):
        raise CameraSourceError(
            f"source IP {address.compressed} is not allowed for camera {source.camera_id}"
        )
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "camera_id": source.camera_id,
        "source_type": source.source_type,
        "branch": source.branch,
        "policy": source.policy,
        "ftp_username": source.ftp_username,
        "remote_ip": address.compressed,
        "received_at": _timestamp(received_at),
        "source_sha256": digest,
        "source_size": size,
        "source_binding_id": source.binding_id,
    }


def _receipt_signature(payload, secret):
    return hmac.new(secret, _canonical_json(payload), hashlib.sha256).hexdigest()


def write_source_receipt(
    image_path,
    source,
    cfg,
    *,
    remote_ip,
    source_sha256,
    source_size,
    received_at=None,
):
    image = Path(image_path)
    if source_for_upload_path((source,), image) != source:
        raise CameraSourceError("image path does not match its camera source")
    secret = receipt_secret(cfg, required=True)
    payload = _receipt_payload(
        source,
        remote_ip=remote_ip,
        received_at=received_at
        or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        source_sha256=source_sha256,
        source_size=source_size,
    )
    payload["signature"] = _receipt_signature(payload, secret)
    destination = receipt_path(image)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary_path, 0o600)
        except OSError:
            pass
        os.replace(temporary_path, destination)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
    return destination


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CameraSourceError(f"source receipt contains duplicate key: {key}")
        result[key] = value
    return result


def _load_receipt_file(path):
    path = Path(path)
    try:
        size = path.stat().st_size
    except FileNotFoundError as exc:
        raise CameraSourceError(f"source receipt is missing: {path.name}") from exc
    if size > MAX_RECEIPT_BYTES:
        raise CameraSourceError(
            f"source receipt exceeds {MAX_RECEIPT_BYTES} bytes"
        )
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                CameraSourceError(f"source receipt contains invalid number: {value}")
            ),
        )
    except UnicodeDecodeError as exc:
        raise CameraSourceError("source receipt must be UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise CameraSourceError(f"source receipt is invalid JSON: {exc}") from exc


def verify_source_receipt(
    image_path,
    cfg,
    root,
    *,
    source_sha256,
    source_size,
    sources=None,
):
    image = Path(image_path)
    sources = tuple(sources or load_camera_sources(cfg, root))
    source = source_for_upload_path(sources, image)
    required = bool(cfg.get("production_mode", False)) or bool(
        cfg.get("camera_source_receipt_required", True)
    )
    path = receipt_path(image)
    if not path.exists() and not required:
        return source, SourceReceipt(
            camera_id=source.camera_id,
            source_type=source.source_type,
            branch=source.branch,
            policy=source.policy,
            ftp_username=source.ftp_username,
            remote_ip="",
            received_at="",
            source_sha256=source_sha256,
            source_size=int(source_size),
            source_binding_id=source.binding_id,
            signature="",
            verified=False,
        )

    payload = _load_receipt_file(path)
    if not isinstance(payload, dict):
        raise CameraSourceError("source receipt must be a JSON object")
    expected_fields = {
        "schema_version",
        "camera_id",
        "source_type",
        "branch",
        "policy",
        "ftp_username",
        "remote_ip",
        "received_at",
        "source_sha256",
        "source_size",
        "source_binding_id",
        "signature",
    }
    unknown = sorted(set(payload) - expected_fields)
    missing = sorted(expected_fields - set(payload))
    if unknown:
        raise CameraSourceError(
            f"source receipt contains unknown fields: {', '.join(unknown)}"
        )
    if missing:
        raise CameraSourceError(
            f"source receipt is missing fields: {', '.join(missing)}"
        )
    if payload.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        raise CameraSourceError(
            f"unsupported source receipt schema_version: {payload.get('schema_version')!r}"
        )

    normalized = _receipt_payload(
        source,
        remote_ip=payload.get("remote_ip"),
        received_at=payload.get("received_at"),
        source_sha256=payload.get("source_sha256"),
        source_size=payload.get("source_size"),
    )
    for key in (
        "camera_id",
        "source_type",
        "branch",
        "policy",
        "ftp_username",
        "source_binding_id",
    ):
        if payload.get(key) != normalized.get(key):
            raise CameraSourceError(
                f"source receipt {key} does not match the configured camera binding"
            )
    if normalized["source_sha256"] != source_sha256:
        raise CameraSourceError("source receipt SHA-256 does not match the upload")
    if normalized["source_size"] != int(source_size):
        raise CameraSourceError("source receipt size does not match the upload")

    signature = _text(payload.get("signature"), "source receipt signature", max_chars=64, pattern=HEX64_RE)
    secret = receipt_secret(cfg, required=True)
    expected_signature = _receipt_signature(normalized, secret)
    if not hmac.compare_digest(signature, expected_signature):
        raise CameraSourceError("source receipt signature is invalid")

    future_tolerance = cfg.get("camera_source_receipt_future_tolerance_seconds", 300)
    if isinstance(future_tolerance, bool) or not isinstance(future_tolerance, int):
        raise CameraSourceError(
            "camera_source_receipt_future_tolerance_seconds must be an integer"
        )
    if future_tolerance < 0 or future_tolerance > 3600:
        raise CameraSourceError(
            "camera_source_receipt_future_tolerance_seconds must be between 0 and 3600"
        )
    received = datetime.fromisoformat(
        normalized["received_at"].replace("Z", "+00:00")
    )
    if received.timestamp() > datetime.now(timezone.utc).timestamp() + future_tolerance:
        raise CameraSourceError("source receipt timestamp is too far in the future")

    return source, SourceReceipt(
        camera_id=source.camera_id,
        source_type=source.source_type,
        branch=source.branch,
        policy=source.policy,
        ftp_username=source.ftp_username,
        remote_ip=normalized["remote_ip"],
        received_at=normalized["received_at"],
        source_sha256=normalized["source_sha256"],
        source_size=normalized["source_size"],
        source_binding_id=source.binding_id,
        signature=signature,
        verified=True,
    )
