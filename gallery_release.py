"""Authenticated, monotonic embedding-gallery release handling."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from embedding_gallery import GalleryError, validate_gallery, write_gallery_atomic


ALGORITHM = "ed25519"
PLACEHOLDERS = {"", "CHANGE_ME", "REPLACE_ME", "CHANGEME", "TODO"}
_B64_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _text(value):
    return str(value or "").strip()


def _placeholder(value):
    return _text(value).upper() in PLACEHOLDERS


def _b64encode(value):
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value, *, field, expected_length=None):
    value = _text(value)
    if not value or not _B64_RE.fullmatch(value):
        raise GalleryError(f"{field} must be unpadded URL-safe base64")
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, TypeError) as exc:
        raise GalleryError(f"{field} is not valid base64") from exc
    if expected_length is not None and len(decoded) != int(expected_length):
        raise GalleryError(
            f"{field} must decode to {int(expected_length)} bytes"
        )
    return decoded


def parse_generated_at(value):
    value = _text(value)
    if not value:
        raise GalleryError("generated_at is required")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise GalleryError("generated_at must be a valid RFC 3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GalleryError("generated_at must include a timezone")
    return parsed.astimezone(timezone.utc)


def canonical_release_bytes(payload):
    if not isinstance(payload, dict):
        raise GalleryError("release payload must be a JSON object")
    unsigned = json.loads(json.dumps(payload, ensure_ascii=False))
    unsigned.pop("checksum", None)
    release = unsigned.get("release")
    if not isinstance(release, dict):
        raise GalleryError("signed gallery release metadata is missing")
    release.pop("signature", None)
    encoded = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return encoded.encode("utf-8")


def release_required(cfg):
    return bool(cfg.get("production_mode", False)) or bool(
        cfg.get("embedding_release_required", False)
    )


def configured_source_url(cfg):
    central = _text(cfg.get("central_url"))
    if not central:
        return ""
    endpoint = _text(cfg.get("embedding_gallery_path")) or "/api/faces/embeddings"
    return urljoin(central.rstrip("/") + "/", endpoint.lstrip("/"))


def _normalized_source_url(value):
    parsed = urlparse(_text(value))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise GalleryError("release source URL must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password:
        raise GalleryError("release source URL must not contain credentials")
    host = (parsed.hostname or "").lower()
    default_port = 443 if parsed.scheme == "https" else 80
    port = parsed.port
    authority = host if port in (None, default_port) else f"{host}:{port}"
    return urlunparse(
        (
            parsed.scheme.lower(),
            authority,
            parsed.path or "/",
            "",
            parsed.query,
            "",
        )
    )


def release_scope(source_url, cfg):
    descriptor = {
        "source_url": _normalized_source_url(source_url),
        "branch": _text(cfg.get("branch_name")),
        "model": _text(cfg.get("model")),
        "model_version": _text(cfg.get("model_version")),
    }
    encoded = json.dumps(
        descriptor,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), descriptor


def scope_state(status, scope_id):
    scopes = status.get("release_scopes") if isinstance(status, dict) else None
    if not isinstance(scopes, dict):
        return {}
    state = scopes.get(scope_id)
    return dict(state) if isinstance(state, dict) else {}


def scoped_etag(status, scope_id):
    return _text(scope_state(status, scope_id).get("etag"))


def _trusted_key(cfg, publisher, key_id):
    keys = cfg.get("embedding_release_trusted_keys")
    if not isinstance(keys, dict) or not keys:
        raise GalleryError("embedding_release_trusted_keys must configure a trusted key")
    entry = keys.get(key_id)
    if isinstance(entry, str):
        public_value = entry
        configured_publisher = _text(cfg.get("embedding_release_publisher"))
    elif isinstance(entry, dict):
        public_value = entry.get("public_key")
        configured_publisher = _text(entry.get("publisher"))
    else:
        raise GalleryError(f"release key_id {key_id!r} is not trusted")

    expected_publisher = _text(cfg.get("embedding_release_publisher"))
    if expected_publisher and publisher != expected_publisher:
        raise GalleryError(
            f"release publisher {publisher!r} does not match {expected_publisher!r}"
        )
    if configured_publisher and publisher != configured_publisher:
        raise GalleryError(
            f"release key {key_id!r} is not assigned to publisher {publisher!r}"
        )
    public_bytes = _b64decode(
        public_value,
        field=f"public key {key_id}",
        expected_length=32,
    )
    return Ed25519PublicKey.from_public_bytes(public_bytes)


def release_policy_issues(cfg):
    if not release_required(cfg):
        return []
    issues = []
    publisher = _text(cfg.get("embedding_release_publisher"))
    if _placeholder(publisher):
        issues.append(
            (
                "embedding_release_publisher_missing",
                "embedding_release_publisher must identify the trusted publisher",
            )
        )
    keys = cfg.get("embedding_release_trusted_keys")
    if not isinstance(keys, dict) or not keys:
        issues.append(
            (
                "embedding_release_keys_missing",
                "embedding_release_trusted_keys must contain at least one Ed25519 key",
            )
        )
    tolerance = int(cfg.get("embedding_release_future_tolerance_seconds", 300) or 0)
    if tolerance < 0 or tolerance > 3600:
        issues.append(
            (
                "embedding_release_time_tolerance_invalid",
                "embedding_release_future_tolerance_seconds must be between 0 and 3600",
            )
        )
    return issues


def validate_release(payload, cfg, prior_state=None, *, now=None):
    if not isinstance(payload, dict):
        raise GalleryError("gallery release must be a JSON object")
    generated_at = parse_generated_at(payload.get("generated_at"))
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    future_tolerance = max(
        0, min(3600, int(cfg.get("embedding_release_future_tolerance_seconds", 300)))
    )
    if generated_at > now + timedelta(seconds=future_tolerance):
        raise GalleryError("generated_at is too far in the future")

    release = payload.get("release")
    if release is None:
        if release_required(cfg):
            raise GalleryError("a signed embedding gallery release is required")
        return {
            "verified": False,
            "sequence": None,
            "publisher": "",
            "key_id": "",
            "algorithm": "",
            "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
            "checksum": _text(payload.get("checksum")),
        }
    if not isinstance(release, dict):
        raise GalleryError("release must be a JSON object")

    try:
        sequence = int(release.get("sequence"))
    except (TypeError, ValueError) as exc:
        raise GalleryError("release.sequence must be an integer") from exc
    if sequence <= 0:
        raise GalleryError("release.sequence must be greater than zero")
    publisher = _text(release.get("publisher"))
    key_id = _text(release.get("key_id"))
    algorithm = _text(release.get("algorithm")).lower()
    if _placeholder(publisher):
        raise GalleryError("release.publisher is required")
    if _placeholder(key_id):
        raise GalleryError("release.key_id is required")
    if algorithm != ALGORITHM:
        raise GalleryError(f"unsupported release algorithm {algorithm!r}")
    signature = _b64decode(
        release.get("signature"),
        field="release.signature",
        expected_length=64,
    )
    public_key = _trusted_key(cfg, publisher, key_id)
    try:
        public_key.verify(signature, canonical_release_bytes(payload))
    except InvalidSignature as exc:
        raise GalleryError("embedding gallery release signature is invalid") from exc

    checksum = _text(payload.get("checksum"))
    if not checksum:
        raise GalleryError("signed gallery has no checksum")
    generated_text = generated_at.isoformat().replace("+00:00", "Z")
    prior_state = prior_state if isinstance(prior_state, dict) else {}
    if prior_state:
        try:
            previous_sequence = int(prior_state.get("sequence"))
        except (TypeError, ValueError):
            previous_sequence = None
        previous_checksum = _text(prior_state.get("checksum"))
        previous_generated = _text(prior_state.get("generated_at"))
        previous_publisher = _text(prior_state.get("publisher"))
        if previous_publisher and previous_publisher != publisher:
            raise GalleryError("release publisher changed within the same source scope")
        if previous_sequence is not None:
            if sequence < previous_sequence:
                raise GalleryError(
                    f"embedding gallery rollback refused: sequence {sequence} < {previous_sequence}"
                )
            if sequence == previous_sequence:
                if checksum != previous_checksum or generated_text != previous_generated:
                    raise GalleryError(
                        "release sequence equivocation refused: the same sequence has different content"
                    )
            elif previous_generated:
                previous_time = parse_generated_at(previous_generated)
                if generated_at < previous_time:
                    raise GalleryError(
                        "embedding gallery generated_at regressed across release sequences"
                    )

    return {
        "verified": True,
        "sequence": sequence,
        "publisher": publisher,
        "key_id": key_id,
        "algorithm": algorithm,
        "generated_at": generated_text,
        "checksum": checksum,
    }


def validate_installed_release(payload, cfg, status, *, source_url=None, now=None):
    source_url = source_url or configured_source_url(cfg)
    if not source_url:
        if release_required(cfg):
            raise GalleryError("central_url is required to bind release state")
        return validate_release(payload, cfg, now=now)
    scope_id, descriptor = release_scope(source_url, cfg)
    state = scope_state(status, scope_id)
    info = validate_release(payload, cfg, state, now=now)
    if release_required(cfg) and not state:
        raise GalleryError("no accepted release state exists for the installed gallery")
    if state:
        if _text(state.get("checksum")) != _text(payload.get("checksum")):
            raise GalleryError("installed gallery checksum does not match accepted release state")
        if info.get("verified"):
            if int(state.get("sequence")) != int(info.get("sequence")):
                raise GalleryError("installed gallery sequence does not match accepted release state")
            if _text(state.get("publisher")) != _text(info.get("publisher")):
                raise GalleryError("installed gallery publisher does not match accepted release state")
    return {**info, "scope_id": scope_id, "scope": descriptor}


def record_acceptance(
    status,
    scope_id,
    descriptor,
    release_info,
    *,
    etag,
    accepted_at=None,
    history_limit=32,
):
    current = dict(status) if isinstance(status, dict) else {}
    scopes = dict(current.get("release_scopes") or {})
    existing = dict(scopes.get(scope_id) or {})
    history = list(existing.get("history") or [])
    entry = {
        "sequence": release_info.get("sequence"),
        "publisher": release_info.get("publisher", ""),
        "key_id": release_info.get("key_id", ""),
        "generated_at": release_info.get("generated_at", ""),
        "checksum": release_info.get("checksum", ""),
        "accepted_at": accepted_at or utc_now(),
    }
    identity = (entry["sequence"], entry["checksum"], entry["publisher"])
    if not history or (
        history[-1].get("sequence"),
        history[-1].get("checksum"),
        history[-1].get("publisher"),
    ) != identity:
        history.append(entry)
    history_limit = min(256, max(1, int(history_limit or 32)))
    history = history[-history_limit:]
    scopes[scope_id] = {
        **descriptor,
        **entry,
        "etag": _text(etag),
        "verified": bool(release_info.get("verified")),
        "history": history,
    }
    return scopes


def sign_gallery_payload(
    payload,
    private_key,
    *,
    publisher,
    key_id,
    sequence,
    generated_at=None,
    validation_options=None,
):
    base = dict(payload)
    base.pop("checksum", None)
    base.pop("release", None)
    base["generated_at"] = generated_at or base.get("generated_at") or utc_now()
    options = dict(validation_options or {})
    sanitized, _, _ = validate_gallery(base, **options)
    sanitized.pop("checksum", None)
    sanitized["release"] = {
        "sequence": int(sequence),
        "publisher": _text(publisher),
        "key_id": _text(key_id),
        "algorithm": ALGORITHM,
    }
    if int(sequence) <= 0:
        raise GalleryError("release sequence must be greater than zero")
    if _placeholder(publisher) or _placeholder(key_id):
        raise GalleryError("publisher and key_id are required")
    signature = private_key.sign(canonical_release_bytes(sanitized))
    sanitized["release"]["signature"] = _b64encode(signature)
    final, _, _ = validate_gallery(sanitized, **options)
    return final


def _atomic_write(path, data, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
        os.chmod(path, mode)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def generate_keypair(private_path, public_path):
    private_key = Ed25519PrivateKey.generate()
    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    _atomic_write(private_path, private_bytes, 0o600)
    _atomic_write(public_path, (_b64encode(public_bytes) + "\n").encode("ascii"), 0o644)


def load_private_key(path):
    try:
        key = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
    except (OSError, ValueError, TypeError) as exc:
        raise GalleryError(f"could not load Ed25519 private key: {exc}") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise GalleryError("release private key is not Ed25519")
    return key


def main():
    parser = argparse.ArgumentParser(description="Manage signed embedding-gallery releases.")
    sub = parser.add_subparsers(dest="command", required=True)
    generate = sub.add_parser("generate-key")
    generate.add_argument("--private-key", type=Path, required=True)
    generate.add_argument("--public-key", type=Path, required=True)
    sign = sub.add_parser("sign")
    sign.add_argument("--gallery", type=Path, required=True)
    sign.add_argument("--private-key", type=Path, required=True)
    sign.add_argument("--publisher", required=True)
    sign.add_argument("--key-id", required=True)
    sign.add_argument("--sequence", type=int, required=True)
    sign.add_argument("--generated-at")
    args = parser.parse_args()

    if args.command == "generate-key":
        generate_keypair(args.private_key, args.public_key)
        print(f"wrote {args.private_key} and {args.public_key}")
        return

    try:
        payload = json.loads(args.gallery.read_text(encoding="utf-8"))
        signed = sign_gallery_payload(
            payload,
            load_private_key(args.private_key),
            publisher=args.publisher,
            key_id=args.key_id,
            sequence=args.sequence,
            generated_at=args.generated_at,
            validation_options={"require_model_match": False},
        )
        write_gallery_atomic(
            args.gallery,
            signed,
            require_model_match=False,
            allow_empty=False,
        )
    except (OSError, json.JSONDecodeError, GalleryError) as exc:
        raise SystemExit(str(exc)) from exc
    print(
        f"signed {args.gallery}: publisher={args.publisher} "
        f"key_id={args.key_id} sequence={args.sequence}"
    )


if __name__ == "__main__":
    main()
