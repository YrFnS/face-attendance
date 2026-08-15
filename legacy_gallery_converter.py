"""Explicit offline converter for trusted legacy embedding pickle files.

Pickle deserialization can execute code. This module is intentionally separate
from every service startup path and requires both a pre-recorded SHA-256 digest
and an explicit risk acknowledgement before it will deserialize anything.
"""

import argparse
import hashlib
import hmac
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from embedding_gallery import GalleryError, build_gallery_payload, write_gallery_atomic


ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"
DEFAULT_SOURCE = ROOT / "embeddings.pkl"
DEFAULT_DESTINATION = ROOT / "embedding_gallery.json"
DEFAULT_BACKUP_DIR = ROOT / "legacy_backups"
DEFAULT_QUARANTINE_DIR = ROOT / "legacy_quarantine"
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def utc_stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _write_bytes_atomic(path, data, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temp_path, mode)
        except OSError:
            pass
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _read_regular_file(path, max_bytes):
    path = Path(path)
    try:
        stat = path.lstat()
    except FileNotFoundError as exc:
        raise GalleryError(f"legacy pickle not found: {path}") from exc
    if path.is_symlink() or not path.is_file():
        raise GalleryError("legacy pickle must be a regular file, not a symlink")
    max_bytes = max(1024, int(max_bytes))
    if stat.st_size > max_bytes:
        raise GalleryError(
            f"legacy pickle exceeds maximum size of {max_bytes} bytes"
        )
    data = path.read_bytes()
    if len(data) > max_bytes:
        raise GalleryError(
            f"legacy pickle exceeds maximum size of {max_bytes} bytes"
        )
    return data


def _validated_digest(data, expected_sha256):
    expected = str(expected_sha256 or "").strip().lower()
    if not SHA256_RE.fullmatch(expected):
        raise GalleryError("expected_sha256 must be exactly 64 hexadecimal characters")
    actual = hashlib.sha256(data).hexdigest()
    if not hmac.compare_digest(actual, expected):
        raise GalleryError(
            f"legacy pickle SHA-256 mismatch: received {actual}, expected {expected}"
        )
    return actual


def _load_config(path):
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GalleryError(f"config file not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise GalleryError(f"could not read config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise GalleryError("config must contain a JSON object")
    return data


def _deserialize_trusted_pickle(data):
    # Import only inside this explicit offline conversion path. Never import or
    # deserialize pickle from a watcher, web process, sync job, or startup path.
    import pickle

    return pickle.loads(data)


def _unique_path(directory, filename):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / filename
    counter = 1
    while candidate.exists():
        candidate = directory / f"{filename}.{counter}"
        counter += 1
    return candidate


def convert_legacy_gallery(
    *,
    source,
    destination,
    config,
    expected_sha256,
    backup_dir,
    quarantine_dir,
    acknowledge_risk=False,
    keep_source=False,
    max_bytes=100 * 1024 * 1024,
):
    if not acknowledge_risk:
        raise GalleryError(
            "conversion requires explicit acknowledgement that pickle "
            "deserialization can execute code"
        )

    source = Path(source)
    destination = Path(destination)
    backup_dir = Path(backup_dir)
    quarantine_dir = Path(quarantine_dir)
    if destination.exists():
        raise GalleryError(f"refusing to overwrite existing gallery: {destination}")

    data = _read_regular_file(source, max_bytes)
    digest = _validated_digest(data, expected_sha256)
    stamp = utc_stamp()
    backup_path = _unique_path(
        backup_dir, f"{source.name}.{stamp}.{digest[:12]}.backup"
    )
    _write_bytes_atomic(backup_path, data)

    try:
        legacy = _deserialize_trusted_pickle(data)
        if not isinstance(legacy, (list, tuple)):
            raise GalleryError("legacy pickle must contain a list of employee records")
        payload = build_gallery_payload(
            legacy,
            model=config.get("model", "buffalo_l"),
            model_version=config.get("model_version", ""),
            branch=config.get("branch_name", ""),
            gallery_version=f"legacy-migration-{stamp}-{digest[:12]}",
        )
        _, metadata = write_gallery_atomic(
            destination,
            payload,
            expected_model=config.get("model", "buffalo_l"),
            expected_model_version=config.get("model_version"),
            expected_branch=config.get("branch_name", ""),
            require_model_match=True,
            require_model_version_match=bool(
                config.get("require_model_version_match", False)
            ),
            allow_empty=False,
            max_employees=int(config.get("max_gallery_employees", 10000)),
            max_embeddings_per_employee=int(
                config.get("max_embeddings_per_employee", 50)
            ),
        )
    except Exception:
        try:
            destination.unlink()
        except FileNotFoundError:
            pass
        raise

    quarantine_path = None
    if not keep_source:
        current = _read_regular_file(source, max_bytes)
        current_digest = hashlib.sha256(current).hexdigest()
        if not hmac.compare_digest(current_digest, digest):
            raise GalleryError(
                "legacy pickle changed during conversion; output was created but "
                "the source was not quarantined"
            )
        quarantine_path = _unique_path(
            quarantine_dir, f"{source.name}.{stamp}.{digest[:12]}.quarantined"
        )
        try:
            os.replace(source, quarantine_path)
        except OSError:
            shutil.copy2(source, quarantine_path)
            source.unlink()
        try:
            os.chmod(quarantine_path, 0o600)
        except OSError:
            pass

    return {
        "source_sha256": digest,
        "backup_path": str(backup_path),
        "destination_path": str(destination),
        "quarantine_path": str(quarantine_path) if quarantine_path else "",
        "gallery_version": metadata.get("gallery_version"),
        "employee_count": metadata.get("employee_count"),
        "embedding_count": metadata.get("embedding_count"),
    }


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Convert one trusted local embeddings.pkl into the validated JSON "
            "gallery format. Stop attendance services and disconnect the host "
            "from untrusted networks before running this command."
        )
    )
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--destination", type=Path, default=DEFAULT_DESTINATION)
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    parser.add_argument(
        "--quarantine-dir", type=Path, default=DEFAULT_QUARANTINE_DIR
    )
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument(
        "--acknowledge-pickle-code-execution-risk",
        action="store_true",
        help="Required acknowledgement. Never use this for an untrusted pickle.",
    )
    parser.add_argument(
        "--keep-source",
        action="store_true",
        help="Keep the original source instead of moving it to quarantine.",
    )
    parser.add_argument(
        "--max-bytes", type=int, default=100 * 1024 * 1024
    )
    args = parser.parse_args()

    try:
        result = convert_legacy_gallery(
            source=args.source,
            destination=args.destination,
            config=_load_config(args.config),
            expected_sha256=args.expected_sha256,
            backup_dir=args.backup_dir,
            quarantine_dir=args.quarantine_dir,
            acknowledge_risk=args.acknowledge_pickle_code_execution_risk,
            keep_source=args.keep_source,
            max_bytes=args.max_bytes,
        )
    except GalleryError as exc:
        raise SystemExit(str(exc)) from exc

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
