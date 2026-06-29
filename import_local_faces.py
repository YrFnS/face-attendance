import argparse
import json
import shutil
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
REPORT = ROOT / "local_face_import_report.json"


def load_config():
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    missing = [key for key in ("frappe_url", "frappe_api_key", "frappe_api_secret") if not cfg.get(key)]
    if missing:
        raise SystemExit(f"missing Frappe config: {', '.join(missing)}")
    return cfg


def frappe_headers(cfg):
    token = cfg["frappe_api_key"] + ":" + cfg["frappe_api_secret"]
    return {"Authorization": "token " + token}


def find_employee(cfg, folder_name):
    url = cfg["frappe_url"].rstrip("/") + "/api/resource/Employee"
    params = {
        "filters": json.dumps([["employee_name", "=", folder_name]], ensure_ascii=False),
        "fields": json.dumps(["name", "employee_name"], ensure_ascii=False),
        "limit_page_length": 2,
    }
    response = requests.get(url, headers=frappe_headers(cfg), params=params, timeout=20)
    response.raise_for_status()
    return response.json().get("data", [])


def image_files(folder):
    return sorted(path for path in folder.iterdir() if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS)


def copy_faces(source, dest, dry_run):
    cfg = load_config()
    report = {"copied": [], "skipped": []}

    for folder in sorted(source.iterdir(), key=lambda path: path.name):
        if not folder.is_dir():
            continue

        files = image_files(folder)
        if not files:
            report["skipped"].append({"folder": folder.name, "reason": "no supported images"})
            continue

        matches = find_employee(cfg, folder.name)
        if len(matches) != 1:
            report["skipped"].append(
                {
                    "folder": folder.name,
                    "reason": "employee match count is not 1",
                    "matches": matches,
                    "image_count": len(files),
                }
            )
            continue

        employee_id = matches[0]["name"]
        target = dest / employee_id
        copied = []
        if not dry_run:
            target.mkdir(parents=True, exist_ok=True)

        for file_path in files:
            target_path = target / f"local_{file_path.name}"
            if not dry_run and not target_path.exists():
                shutil.copy2(file_path, target_path)
            copied.append(str(target_path))

        report["copied"].append(
            {
                "folder": folder.name,
                "employee": employee_id,
                "employee_name": matches[0]["employee_name"],
                "image_count": len(files),
                "files": copied,
            }
        )

    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"matched {len(report['copied'])} folder(s)")
    print(f"skipped {len(report['skipped'])} folder(s)")
    print(f"report: {REPORT}")


def main():
    parser = argparse.ArgumentParser(description="Import local employee face folders into faces/<employee-id>.")
    parser.add_argument("--source", required=True, type=Path, help="Folder containing one subfolder per employee name.")
    parser.add_argument("--dest", default=ROOT / "faces", type=Path, help="Destination faces folder.")
    parser.add_argument("--dry-run", action="store_true", help="Only write the report; do not copy images.")
    args = parser.parse_args()

    if not args.source.exists():
        raise SystemExit(f"source does not exist: {args.source}")

    copy_faces(args.source, args.dest, args.dry_run)


if __name__ == "__main__":
    main()
