import argparse
import getpass
import json
import os
import secrets
import tempfile
from pathlib import Path

from auth_backends import auth_configured
from secret_store import (
    ConfigLoadError,
    is_secret_reference,
    load_config_document,
    load_runtime_config,
    write_secret_file_atomic,
)
from web_security import hash_password


ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"


def load_config(path=CONFIG):
    try:
        return load_config_document(path)
    except ConfigLoadError as exc:
        raise SystemExit(str(exc)) from exc


def write_config_atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
        os.chmod(path, 0o600)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _credential_reference(directory, name, value):
    directory = Path(directory).expanduser().resolve()
    write_secret_file_atomic(directory / name, value)
    return f"systemd://{name}"


def configure_admin(
    path=CONFIG,
    username=None,
    password=None,
    *,
    credential_directory=None,
):
    cfg = load_config(path)
    username = str(username or cfg.get("web_admin_username") or "admin").strip()
    if not username:
        raise ValueError("admin username cannot be empty")
    if password is None:
        password = getpass.getpass("New admin password: ")
        repeated = getpass.getpass("Repeat admin password: ")
        if password != repeated:
            raise ValueError("passwords do not match")
    password_hash = hash_password(password)
    session_secret = secrets.token_urlsafe(48)
    cfg["web_admin_username"] = username
    if credential_directory:
        cfg["web_admin_password_hash"] = _credential_reference(
            credential_directory,
            "web_admin_password_hash",
            password_hash,
        )
        cfg["web_session_secret"] = _credential_reference(
            credential_directory,
            "web_session_secret",
            session_secret,
        )
    else:
        cfg["web_admin_password_hash"] = password_hash
        current = cfg.get("web_session_secret")
        if not is_secret_reference(current):
            secret = str(current or "").strip()
            if len(secret) < 32 or secret.upper() in {
                "CHANGE_ME",
                "REPLACE_ME",
                "CHANGEME",
            }:
                cfg["web_session_secret"] = session_secret
    write_config_atomic(path, cfg)
    return cfg


def rotate_session_secret(path=CONFIG, *, credential_directory=None):
    cfg = load_config(path)
    value = secrets.token_urlsafe(48)
    if credential_directory:
        cfg["web_session_secret"] = _credential_reference(
            credential_directory,
            "web_session_secret",
            value,
        )
    elif is_secret_reference(cfg.get("web_session_secret")):
        raise ValueError(
            "web_session_secret is externally managed; supply --credential-directory to rotate it"
        )
    else:
        cfg["web_session_secret"] = value
    write_config_atomic(path, cfg)
    return cfg


def main():
    parser = argparse.ArgumentParser(description="Manage Face Attendance web-admin credentials.")
    parser.add_argument("--config", type=Path, default=CONFIG)
    sub = parser.add_subparsers(dest="command", required=True)

    password = sub.add_parser("set-password")
    password.add_argument("--username", default=None)
    password.add_argument(
        "--credential-directory",
        type=Path,
        default=None,
        help="Write 0600 systemd credential files and store systemd:// references in config.json.",
    )
    rotate = sub.add_parser("rotate-session-secret")
    rotate.add_argument("--credential-directory", type=Path, default=None)
    sub.add_parser("status")
    args = parser.parse_args()

    if args.command == "set-password":
        try:
            cfg = configure_admin(
                args.config,
                username=args.username,
                credential_directory=args.credential_directory,
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        print(f"configured web admin user: {cfg['web_admin_username']}")
        print("all existing admin sessions were invalidated")
    elif args.command == "rotate-session-secret":
        try:
            rotate_session_secret(
                args.config,
                credential_directory=args.credential_directory,
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        print("rotated session secret; all existing admin sessions were invalidated")
    elif args.command == "status":
        try:
            cfg = load_runtime_config(args.config)
        except ConfigLoadError as exc:
            raise SystemExit(str(exc)) from exc
        print("configured" if auth_configured(cfg) else "not configured")


if __name__ == "__main__":
    main()