import argparse
import getpass
import json
import os
import secrets
import tempfile
from pathlib import Path

from web_security import auth_configured, hash_password


ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"


def load_config(path=CONFIG):
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"missing config: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"config must contain a JSON object: {path}")
    return data


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


def configure_admin(path=CONFIG, username=None, password=None):
    cfg = load_config(path)
    username = str(username or cfg.get("web_admin_username") or "admin").strip()
    if not username:
        raise ValueError("admin username cannot be empty")
    if password is None:
        password = getpass.getpass("New admin password: ")
        repeated = getpass.getpass("Repeat admin password: ")
        if password != repeated:
            raise ValueError("passwords do not match")
    cfg["web_admin_username"] = username
    cfg["web_admin_password_hash"] = hash_password(password)
    secret = str(cfg.get("web_session_secret") or "").strip()
    if len(secret) < 32 or secret.upper() in {"CHANGE_ME", "REPLACE_ME", "CHANGEME"}:
        cfg["web_session_secret"] = secrets.token_urlsafe(48)
    write_config_atomic(path, cfg)
    return cfg


def rotate_session_secret(path=CONFIG):
    cfg = load_config(path)
    cfg["web_session_secret"] = secrets.token_urlsafe(48)
    write_config_atomic(path, cfg)
    return cfg


def main():
    parser = argparse.ArgumentParser(description="Manage Face Attendance web-admin credentials.")
    parser.add_argument("--config", type=Path, default=CONFIG)
    sub = parser.add_subparsers(dest="command", required=True)

    password = sub.add_parser("set-password")
    password.add_argument("--username", default=None)
    sub.add_parser("rotate-session-secret")
    sub.add_parser("status")
    args = parser.parse_args()

    if args.command == "set-password":
        try:
            cfg = configure_admin(args.config, username=args.username)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        print(f"configured web admin user: {cfg['web_admin_username']}")
        print("all existing admin sessions were invalidated")
    elif args.command == "rotate-session-secret":
        rotate_session_secret(args.config)
        print("rotated session secret; all existing admin sessions were invalidated")
    elif args.command == "status":
        cfg = load_config(args.config)
        print("configured" if auth_configured(cfg) else "not configured")


if __name__ == "__main__":
    main()
