import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"

try:
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
except (FileNotFoundError, OSError, json.JSONDecodeError):
    cfg = {}

bind = f"{cfg.get('web_bind_host', '127.0.0.1')}:{int(cfg.get('web_port', 8088))}"
workers = max(1, int(cfg.get("web_workers", 2)))
worker_class = "gthread"
threads = max(1, int(cfg.get("web_threads", 4)))
timeout = max(30, int(cfg.get("web_request_timeout_seconds", 120)))
graceful_timeout = 30
keepalive = 5
accesslog = "-"
errorlog = "-"
loglevel = str(cfg.get("web_log_level", "info"))
capture_output = True
max_requests = max(100, int(cfg.get("web_max_requests", 2000)))
max_requests_jitter = max(0, int(cfg.get("web_max_requests_jitter", 200)))
preload_app = False
umask = 0o077
