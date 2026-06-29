import json
from pathlib import Path

from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import FTPServer


ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"


def load_config():
    return json.loads(CONFIG.read_text())


def main():
    cfg = load_config()
    uploads = Path(cfg.get("camera_uploads_dir", ROOT / "camera_uploads"))
    uploads.mkdir(exist_ok=True)
    authorizer = DummyAuthorizer()
    users = cfg.get("ftp_users") or {
        cfg["ftp_username"]: {"password": cfg["ftp_password"], "dir": str(uploads)}
    }
    for username, item in users.items():
        folder = Path(item.get("dir", uploads / username))
        folder.mkdir(parents=True, exist_ok=True)
        authorizer.add_user(username, item["password"], str(folder), perm="elradfmwMT")

    handler = FTPHandler
    handler.authorizer = authorizer
    handler.passive_ports = range(30000, 30010)

    port = int(cfg.get("ftp_port", 2121))
    server = FTPServer(("0.0.0.0", port), handler)
    print(f"FTP receiver listening on 0.0.0.0:{port} -> {uploads}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
