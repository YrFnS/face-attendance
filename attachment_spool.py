"""Filesystem spool for private attendance crops.

The spool contains copies used only by durable attachment jobs.  Normal crop
cleanup never scans this directory, so a required crop remains available until
the attachment job reaches an explicit terminal outcome.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from attachment_outbox import make_attachment_id


class AttachmentSpoolError(RuntimeError):
    pass


@dataclass(frozen=True)
class SpoolRecord:
    attachment_id: str
    source_path: str
    source_sha256: str
    source_size: int
    filename: str
    content_type: str = "image/jpeg"
    delete_after_success: bool = True
    newly_created: bool = False

    def to_job_metadata(self):
        return {
            "source_path": self.source_path,
            "source_sha256": self.source_sha256,
            "source_size": self.source_size,
            "filename": self.filename,
            "content_type": self.content_type,
            "delete_after_success": self.delete_after_success,
        }


def _has_symlink_component(path):
    cursor = Path(path)
    while True:
        if cursor.exists() and cursor.is_symlink():
            return True
        if cursor == cursor.parent:
            return False
        cursor = cursor.parent


def _sha256(path, *, max_bytes):
    size = Path(path).stat().st_size
    if size <= 0:
        raise AttachmentSpoolError("attachment crop is empty")
    if size > int(max_bytes):
        raise AttachmentSpoolError(
            f"attachment crop exceeds maximum size of {int(max_bytes)} bytes"
        )
    digest = hashlib.sha256()
    read = 0
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            read += len(chunk)
            if read > int(max_bytes):
                raise AttachmentSpoolError(
                    f"attachment crop exceeds maximum size of {int(max_bytes)} bytes"
                )
            digest.update(chunk)
    return digest.hexdigest(), size


def _verify_jpeg(path):
    with Path(path).open("rb") as handle:
        prefix = handle.read(3)
        if prefix[:2] != b"\xff\xd8":
            raise AttachmentSpoolError("attachment crop is not a JPEG image")


def resolve_spool_root(root, cfg):
    value = cfg.get("attachment_spool_dir") or "delivery_attachments"
    candidate = Path(value).expanduser()
    candidate = candidate if candidate.is_absolute() else Path(root) / candidate
    # Check the lexical path before resolving it. Resolving first would hide a
    # symbolic-link component and could let the private spool escape its
    # configured location.
    if _has_symlink_component(candidate):
        raise AttachmentSpoolError(
            f"attachment spool must not use symbolic links: {candidate}"
        )
    candidate.mkdir(parents=True, exist_ok=True)
    if _has_symlink_component(candidate):
        raise AttachmentSpoolError(
            f"attachment spool must not use symbolic links: {candidate}"
        )
    path = candidate.resolve(strict=False)
    if os.name != "nt":
        try:
            os.chmod(path, 0o700)
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError as exc:
            raise AttachmentSpoolError(
                f"attachment spool permissions could not be secured: {exc}"
            ) from exc
        if mode & 0o077:
            raise AttachmentSpoolError(
                "attachment spool must not be accessible by group or other users"
            )
    return path


def spool_private_crop(source_path, *, decision_id, root, cfg):
    source_path = Path(source_path)
    if source_path.is_symlink() or not source_path.is_file():
        raise AttachmentSpoolError(
            "attachment crop source must be a regular non-symbolic-link file"
        )
    if _has_symlink_component(source_path.parent):
        raise AttachmentSpoolError(
            "attachment crop source path must not use symbolic links"
        )
    max_bytes = int(cfg.get("attachment_max_image_bytes", 10 * 1024 * 1024))
    if max_bytes < 1024 or max_bytes > 100 * 1024 * 1024:
        raise AttachmentSpoolError(
            "attachment_max_image_bytes must be between 1024 and 104857600"
        )
    digest, size = _sha256(source_path, max_bytes=max_bytes)
    _verify_jpeg(source_path)
    attachment_id = make_attachment_id(decision_id)
    spool_root = resolve_spool_root(root, cfg)
    destination = spool_root / f"{attachment_id}.jpg"
    newly_created = False
    if destination.exists():
        if destination.is_symlink() or not destination.is_file():
            raise AttachmentSpoolError(
                "existing attachment spool destination is unsafe"
            )
        if os.name != "nt":
            try:
                os.chmod(destination, 0o600)
            except OSError as exc:
                raise AttachmentSpoolError(
                    f"existing attachment spool permissions are unsafe: {exc}"
                ) from exc
        _verify_private_file_permissions(destination)
        existing_digest, existing_size = _sha256(
            destination, max_bytes=max_bytes
        )
        if (existing_digest, existing_size) != (digest, size):
            raise AttachmentSpoolError(
                "attachment spool identifier is bound to different content"
            )
    else:
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{attachment_id}.", suffix=".incoming", dir=spool_root
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(descriptor, "wb") as output, source_path.open("rb") as source:
                shutil.copyfileobj(source, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            copied_digest, copied_size = _sha256(temp_path, max_bytes=max_bytes)
            if (copied_digest, copied_size) != (digest, size):
                raise AttachmentSpoolError(
                    "attachment spool copy did not match source content"
                )
            try:
                os.chmod(temp_path, 0o600)
            except OSError:
                pass
            os.replace(temp_path, destination)
            newly_created = True
            if os.name != "nt":
                directory_fd = os.open(spool_root, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
    return SpoolRecord(
        attachment_id=attachment_id,
        source_path=str(destination),
        source_sha256=digest,
        source_size=size,
        filename=f"attendance-{attachment_id[:16]}.jpg",
        delete_after_success=bool(
            cfg.get("attachment_delete_spool_after_success", True)
        ),
        newly_created=newly_created,
    )


def _path_within_root(path, spool_root):
    path = Path(path).resolve(strict=False)
    spool_root = Path(spool_root).resolve(strict=False)
    return path.parent == spool_root


def _verify_private_file_permissions(path):
    if os.name == "nt":
        return
    try:
        mode = stat.S_IMODE(Path(path).stat().st_mode)
    except OSError as exc:
        raise AttachmentSpoolError(
            f"attachment crop permissions could not be inspected: {exc}"
        ) from exc
    if mode & 0o077:
        raise AttachmentSpoolError(
            "spooled attachment must not be accessible by group or other users"
        )


def discard_spooled_crop(record, *, spool_root=None):
    if not record:
        return False
    path = Path(
        record.source_path if isinstance(record, SpoolRecord) else record
    )
    if spool_root is not None and not _path_within_root(path, spool_root):
        raise AttachmentSpoolError(
            "refusing to delete an attachment outside the configured spool"
        )
    try:
        if path.is_symlink():
            raise AttachmentSpoolError(
                "refusing to delete a symbolic-link attachment spool"
            )
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def verify_spooled_crop(job, *, max_bytes, spool_root=None):
    if not isinstance(job, dict):
        raise AttachmentSpoolError("attachment job must be a mapping")
    path = Path(str(job.get("source_path") or ""))
    if spool_root is not None and not _path_within_root(path, spool_root):
        raise AttachmentSpoolError(
            "spooled attachment is outside the configured spool directory"
        )
    if path.is_symlink() or not path.is_file():
        raise AttachmentSpoolError(
            "spooled attachment is missing or is not a regular file"
        )
    _verify_private_file_permissions(path)
    if _has_symlink_component(path.parent):
        raise AttachmentSpoolError(
            "spooled attachment path uses a symbolic link"
        )
    digest, size = _sha256(path, max_bytes=int(max_bytes))
    _verify_jpeg(path)
    if digest != str(job.get("source_sha256") or ""):
        raise AttachmentSpoolError("spooled attachment SHA-256 mismatch")
    if size != int(job.get("source_size") or 0):
        raise AttachmentSpoolError("spooled attachment size mismatch")
    if path.name != f"{job['attachment_id']}.jpg":
        raise AttachmentSpoolError(
            "spooled attachment filename does not match attachment identity"
        )
    return path


def cleanup_orphaned_spool(state, *, root, cfg, now=None):
    """Delete old spool files that are not referenced by durable jobs.

    A crop is copied before the SQLite decision transaction begins. If the
    process dies before that transaction commits, the deterministic final file
    can be left behind. Only unreferenced files older than the configured grace
    period are removed; any path referenced by ``attachment_jobs`` is retained.
    """

    import time

    current = time.time() if now is None else float(now)
    grace = cfg.get("attachment_orphan_grace_seconds", 3600)
    if isinstance(grace, bool) or not isinstance(grace, int):
        raise AttachmentSpoolError(
            "attachment_orphan_grace_seconds must be an integer"
        )
    if grace < 60 or grace > 7 * 86400:
        raise AttachmentSpoolError(
            "attachment_orphan_grace_seconds must be between 60 and 604800"
        )
    spool_root = resolve_spool_root(root, cfg)
    referenced = {str(Path(path).resolve(strict=False)) for path in state.attachment_source_paths()}
    removed = []
    for pattern in ("*.jpg", ".*.incoming"):
        for path in spool_root.glob(pattern):
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                if current - path.stat().st_mtime < grace:
                    continue
                resolved = str(path.resolve(strict=False))
                if resolved in referenced:
                    continue
                path.unlink()
                removed.append(path.name)
            except FileNotFoundError:
                continue
    return removed
