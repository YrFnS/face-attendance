import json
import os
import time
from pathlib import Path

from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler, TLS_FTPHandler
from pyftpdlib.servers import FTPServer


ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png"}
PLACEHOLDER_PASSWORDS = {"", "CHANGE_ME", "REPLACE_ME", "CHANGEME"}


def load_config():
    try:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"missing config: {CONFIG}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON in {CONFIG}: {exc}") from exc
    return data


def resolve_folder(value):
    path = Path(value).expanduser()
    return path if path.is_absolute() else ROOT / path


def unique_destination(folder, filename):
    destination = folder / Path(filename).name
    if not destination.exists():
        return destination
    stamp = time.strftime("%Y%m%d_%H%M%S")
    stem = destination.stem
    suffix = destination.suffix
    counter = 1
    while True:
        candidate = folder / f"{stem}_{stamp}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


class AtomicUploadMixin:
    user_targets = {}
    max_upload_bytes = 20 * 1024 * 1024

    def on_file_received(self, file):
        source = Path(file)
        target = self.user_targets.get(self.username)
        try:
            if not target:
                raise ValueError("FTP user has no final upload directory")
            if source.suffix.lower() not in ALLOWED_EXTENSIONS:
                raise ValueError("unsupported image extension")
            size = source.stat().st_size
            if self.max_upload_bytes and size > self.max_upload_bytes:
                raise ValueError(
                    f"upload exceeds {self.max_upload_bytes} byte limit"
                )
            target.mkdir(parents=True, exist_ok=True)
            destination = unique_destination(target, source.name)
            os.replace(source, destination)
            try:
                os.chmod(destination, 0o600)
            except OSError:
                pass
            print(
                f"FTP upload complete user={self.username} file={destination} size={size}",
                flush=True,
            )
        except Exception as exc:
            try:
                source.unlink()
            except FileNotFoundError:
                pass
            print(
                f"FTP upload rejected user={self.username} file={source.name} error={exc}",
                flush=True,
            )

    def on_incomplete_file_received(self, file):
        path = Path(file)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        print(
            f"FTP incomplete upload removed user={self.username} file={path.name}",
            flush=True,
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


def main():
    cfg = load_config()
    uploads = resolve_folder(cfg.get("camera_uploads_dir", ROOT / "camera_uploads"))
    uploads.mkdir(parents=True, exist_ok=True)
    authorizer = DummyAuthorizer()
    users = cfg.get("ftp_users") or {
        cfg["ftp_username"]: {
            "password": cfg["ftp_password"],
            "dir": str(uploads),
        }
    }
    default_permissions = str(cfg.get("ftp_permissions", "elw"))
    staging_enabled = bool(cfg.get("ftp_staging_enabled", True))
    targets = {}

    for username, item in users.items():
        username = str(username).strip()
        password = str(item.get("password") or "")
        if not username:
            raise SystemExit("FTP username cannot be empty")
        if password.strip().upper() in PLACEHOLDER_PASSWORDS:
            raise SystemExit(f"FTP password for {username} is missing or still a placeholder")
        target = resolve_folder(item.get("dir", uploads / username))
        target.mkdir(parents=True, exist_ok=True)
        home = target / ".incoming" / username if staging_enabled else target
        home.mkdir(parents=True, exist_ok=True)
        permissions = str(item.get("permissions") or default_permissions)
        authorizer.add_user(username, password, str(home), perm=permissions)
        targets[username] = target

    handler = AtomicTLSUploadHandler if bool(cfg.get("ftp_tls_enabled", False)) else AtomicUploadHandler
    handler.authorizer = authorizer
    handler.user_targets = targets
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
    print(
        f"{protocol.upper()} receiver listening on {bind_host}:{port} -> {uploads}; "
        f"staging={'on' if staging_enabled else 'off'}; "
        f"tls_control_required={getattr(handler, 'tls_control_required', False)}; "
        f"tls_data_required={getattr(handler, 'tls_data_required', False)}",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
