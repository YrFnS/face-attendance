import hashlib
import json
import os
import time
import unicodedata
from pathlib import Path

from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler, TLS_FTPHandler
from pyftpdlib.servers import FTPServer

from camera_sources import (
    CameraSourceError,
    ftp_user_config,
    load_camera_sources,
    normalize_ip,
    receipt_path,
    source_by_username,
    write_source_receipt,
)
from data_contract import safe_log_message


ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png"}


def load_config():
    try:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"missing config: {CONFIG}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON in {CONFIG}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit("config must contain a JSON object")
    return data


def resolve_folder(value):
    path = Path(value).expanduser()
    return path if path.is_absolute() else ROOT / path


def file_sha256(path, chunk_size=1024 * 1024):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def validate_upload_filename(value):
    name = Path(str(value)).name
    if not name or name.startswith("."):
        raise ValueError("upload filename must be a visible basename")
    if len(name) > 255:
        raise ValueError("upload filename exceeds 255 characters")
    for character in name:
        if unicodedata.category(character) in {"Cc", "Cf", "Cs"}:
            raise ValueError("upload filename contains a control or formatting character")
    if Path(name).suffix.lower() not in ALLOWED_EXTENSIONS:
        raise ValueError("unsupported image extension")
    return name


def unique_destination(folder, filename):
    name = validate_upload_filename(filename)
    destination = folder / name
    if not destination.exists() and not receipt_path(destination).exists():
        return destination
    stamp = time.strftime("%Y%m%d_%H%M%S")
    stem = destination.stem
    suffix = destination.suffix
    counter = 1
    while True:
        candidate = folder / f"{stem}_{stamp}_{counter}{suffix}"
        if not candidate.exists() and not receipt_path(candidate).exists():
            return candidate
        counter += 1


class AtomicUploadMixin:
    user_sources = {}
    receipt_config = {}
    max_upload_bytes = 20 * 1024 * 1024
    staging_enabled = True

    def _log(self, message):
        print(safe_log_message(message), flush=True)

    def _session_source(self, username=None):
        username = str(username if username is not None else getattr(self, "username", ""))
        source = source_by_username(tuple(self.user_sources.values()), username)
        remote_ip = normalize_ip(getattr(self, "remote_ip", "")).compressed
        if not source.allows_ip(remote_ip):
            raise CameraSourceError(
                f"source IP {remote_ip} is not allowed for camera {source.camera_id}"
            )
        return source, remote_ip

    def on_login(self, username):
        try:
            source, remote_ip = self._session_source(username)
        except Exception as exc:
            self._log(f"FTP login rejected user={username} ip={getattr(self, 'remote_ip', '-')} error={exc}")
            try:
                self.respond("530 Source network is not allowed for this camera credential.")
            finally:
                self.close_when_done()
            return
        self._log(
            f"FTP login accepted user={username} camera={source.camera_id} "
            f"policy={source.policy} branch={source.branch} ip={remote_ip}"
        )

    def on_file_received(self, file):
        source_path = Path(file)
        destination = None
        try:
            camera_source, remote_ip = self._session_source()
            expected_home = camera_source.upload_dir
            if self.staging_enabled:
                expected_home = expected_home / ".incoming" / camera_source.ftp_username
            expected_home = expected_home.resolve(strict=False)
            try:
                source_path.resolve(strict=False).relative_to(expected_home)
            except ValueError as exc:
                raise ValueError("FTP upload arrived outside its bound staging route") from exc

            name = validate_upload_filename(source_path.name)
            size = source_path.stat().st_size
            if self.max_upload_bytes and size > self.max_upload_bytes:
                raise ValueError(
                    f"upload exceeds {self.max_upload_bytes} byte limit"
                )
            camera_source.upload_dir.mkdir(parents=True, exist_ok=True)
            destination = unique_destination(camera_source.upload_dir, name)
            os.replace(source_path, destination)
            try:
                os.chmod(destination, 0o600)
            except OSError:
                pass
            digest = file_sha256(destination)
            write_source_receipt(
                destination,
                camera_source,
                self.receipt_config,
                remote_ip=remote_ip,
                source_sha256=digest,
                source_size=size,
            )
            self._log(
                f"FTP upload complete user={camera_source.ftp_username} "
                f"camera={camera_source.camera_id} policy={camera_source.policy} "
                f"branch={camera_source.branch} ip={remote_ip} "
                f"file={destination.name} sha256={digest[:16]} size={size}"
            )
        except Exception as exc:
            for path in (source_path, destination, receipt_path(destination) if destination else None):
                if path is None:
                    continue
                try:
                    Path(path).unlink()
                except FileNotFoundError:
                    pass
            self._log(
                f"FTP upload rejected user={getattr(self, 'username', '-')} "
                f"ip={getattr(self, 'remote_ip', '-')} file={source_path.name} error={exc}"
            )

    def on_incomplete_file_received(self, file):
        path = Path(file)
        for candidate in (path, receipt_path(path)):
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass
        self._log(
            f"FTP incomplete upload removed user={getattr(self, 'username', '-')} "
            f"ip={getattr(self, 'remote_ip', '-')} file={path.name}"
        )


class AtomicUploadHandler(AtomicUploadMixin, FTPHandler):
    pass


class AtomicTLSUploadHandler(AtomicUploadMixin, TLS_FTPHandler):
    pass


def configure_tls(cfg, handler):
    if not bool(cfg.get("ftp_tls_enabled", False)):
        return "ftp"
    certfile = resolve_folder(cfg.get("ftp_tls_certfile", ""))
    keyfile = resolve_folder(cfg.get("ftp_tls_keyfile", ""))
    if not certfile.is_file():
        raise SystemExit(f"FTPS certificate not found: {certfile}")
    if not keyfile.is_file():
        raise SystemExit(f"FTPS private key not found: {keyfile}")
    handler.certfile = str(certfile)
    handler.keyfile = str(keyfile)
    handler.tls_control_required = bool(cfg.get("ftp_tls_control_required", True))
    handler.tls_data_required = bool(cfg.get("ftp_tls_data_required", True))
    return "ftps"


def build_authorizer(cfg, root=ROOT):
    try:
        sources = load_camera_sources(cfg, root)
    except CameraSourceError as exc:
        raise SystemExit(f"invalid camera source configuration: {exc}") from exc
    authorizer = DummyAuthorizer()
    staging_enabled = bool(cfg.get("ftp_staging_enabled", True))
    source_map = {}
    for source in sources:
        account = ftp_user_config(cfg, source)
        source.upload_dir.mkdir(parents=True, exist_ok=True)
        home = (
            source.upload_dir / ".incoming" / source.ftp_username
            if staging_enabled
            else source.upload_dir
        )
        home.mkdir(parents=True, exist_ok=True)
        authorizer.add_user(
            source.ftp_username,
            account["password"],
            str(home),
            perm=account["permissions"],
        )
        source_map[source.ftp_username] = source
    return authorizer, source_map, staging_enabled


def main():
    cfg = load_config()
    uploads = resolve_folder(cfg.get("camera_uploads_dir", ROOT / "camera_uploads"))
    uploads.mkdir(parents=True, exist_ok=True)
    authorizer, source_map, staging_enabled = build_authorizer(cfg, ROOT)

    handler = (
        AtomicTLSUploadHandler
        if bool(cfg.get("ftp_tls_enabled", False))
        else AtomicUploadHandler
    )
    handler.authorizer = authorizer
    handler.user_sources = source_map
    handler.receipt_config = cfg
    handler.staging_enabled = staging_enabled
    handler.max_upload_bytes = int(
        cfg.get("max_camera_upload_bytes", 20 * 1024 * 1024)
    )
    handler.banner = "Face Attendance camera upload service"
    handler.timeout = int(cfg.get("ftp_client_timeout_seconds", 120))
    protocol = configure_tls(cfg, handler)

    passive_start = int(cfg.get("ftp_passive_port_start", 30000))
    passive_end = int(cfg.get("ftp_passive_port_end", 30009))
    if passive_end < passive_start:
        raise SystemExit("ftp_passive_port_end must be >= ftp_passive_port_start")
    handler.passive_ports = range(passive_start, passive_end + 1)
    masquerade = str(cfg.get("ftp_masquerade_address") or "").strip()
    if masquerade:
        handler.masquerade_address = masquerade

    port = int(cfg.get("ftp_port", 2121))
    bind_host = str(cfg.get("ftp_bind_host", "0.0.0.0"))
    server = FTPServer((bind_host, port), handler)
    server.max_cons = max(1, int(cfg.get("ftp_max_connections", 20)))
    server.max_cons_per_ip = max(
        1, int(cfg.get("ftp_max_connections_per_ip", 5))
    )
    bindings = ", ".join(
        f"{source.ftp_username}->{source.camera_id}/{source.policy}@{source.branch}"
        for source in sorted(source_map.values(), key=lambda item: item.camera_id)
    )
    print(
        safe_log_message(
            f"{protocol.upper()} receiver listening on {bind_host}:{port} -> {uploads}; "
            f"staging={'on' if staging_enabled else 'off'}; "
            f"tls_control_required={getattr(handler, 'tls_control_required', False)}; "
            f"tls_data_required={getattr(handler, 'tls_data_required', False)}; "
            f"sources={bindings}"
        ),
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
