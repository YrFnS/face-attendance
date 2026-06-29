import json
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parent
FACES = ROOT / "faces"
CONFIG = ROOT / "config.json"


def main():
    cfg = json.loads(CONFIG.read_text())
    central = cfg["central_url"].rstrip("/")
    branch = cfg["branch_name"]
    manifest = requests.get(f"{central}/api/faces/manifest", params={"branch": branch}, timeout=30)
    manifest.raise_for_status()

    saved = 0
    for item in manifest.json():
        employee = item["person"]
        filename = item["file"]
        folder = FACES / employee
        folder.mkdir(parents=True, exist_ok=True)
        suffix = Path(filename).suffix or ".jpg"
        path = folder / f"{Path(filename).stem}{suffix}"
        if path.exists():
            continue
        image = requests.get(
            f"{central}/api/faces/file/{item['company']}/{item['branch']}/{employee}/{filename}",
            timeout=30,
        )
        image.raise_for_status()
        path.write_bytes(image.content)
        saved += 1
    print(f"saved {saved} image(s)")


if __name__ == "__main__":
    main()
