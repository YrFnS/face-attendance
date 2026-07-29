import argparse
import json
import time
from pathlib import Path

from embedding_gallery import GalleryError, gallery_status, sync_gallery


ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"
GALLERY = ROOT / "embedding_gallery.json"
SYNC_STATUS = ROOT / "embedding_sync_status.json"


def load_config():
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"missing config: {CONFIG}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON in {CONFIG}: {exc}") from exc


def sync_once(cfg):
    result = sync_gallery(cfg, GALLERY, SYNC_STATUS)
    action = "updated" if result["changed"] else "already current"
    print(
        f"embedding gallery {action}: "
        f"{result['employee_count']} employee(s), "
        f"{result['embedding_count']} embedding(s), "
        f"version={result['gallery_version']}"
    )
    return result


def print_status(cfg):
    status = gallery_status(
        GALLERY,
        max_age_seconds=cfg.get("embedding_max_age_seconds", 86400),
    )
    print(json.dumps(status, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(
        description="Synchronize the local employee embedding gallery from the central server."
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Keep synchronizing using embedding_sync_interval_seconds.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print local gallery status without contacting the central server.",
    )
    args = parser.parse_args()
    cfg = load_config()

    if args.status:
        print_status(cfg)
        return

    if not args.watch:
        try:
            sync_once(cfg)
        except GalleryError as exc:
            raise SystemExit(str(exc)) from exc
        return

    interval = max(10, int(cfg.get("embedding_sync_interval_seconds", 300)))
    while True:
        try:
            sync_once(cfg)
        except GalleryError as exc:
            print(f"embedding sync failed: {exc}", flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    main()
