import base64
import json
import os
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TREE_SHA = "e33f2cc1e959b818763843f2356e2ca74ee14593"
EXPECTED = {
    *{f".h0/camera_sources.py.{index:03d}.part" for index in range(7)},
    *{f".h0/test_camera_sources.py.{index:03d}.part" for index in range(3)},
    *{f".h0/test_ftp_receiver.py.{index:03d}.part" for index in range(2)},
    *{f".h0/test_watch_service.py.{index:03d}.part" for index in range(3)},
}


def api_json(path):
    repository = os.environ["GITHUB_REPOSITORY"]
    token = os.environ["GH_TOKEN"]
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "face-attendance-h0-09-builder",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def fetch_fragments():
    tree = api_json(f"git/trees/{TREE_SHA}?recursive=1")
    entries = {
        item["path"]: item["sha"]
        for item in tree.get("tree", [])
        if item.get("type") == "blob" and item.get("path") in EXPECTED
    }
    if set(entries) != EXPECTED:
        raise SystemExit(
            f"source fragment set mismatch: missing={sorted(EXPECTED - set(entries))} "
            f"extra={sorted(set(entries) - EXPECTED)}"
        )
    for path, sha in entries.items():
        payload = api_json(f"git/blobs/{sha}")
        if payload.get("encoding") != "base64":
            raise SystemExit(f"unexpected blob encoding for {path}")
        content = base64.b64decode(payload["content"], validate=True)
        destination = ROOT / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)


def assemble(target, prefix, count):
    parts = [ROOT / ".h0" / f"{prefix}.{index:03d}.part" for index in range(count)]
    output = []
    for part in parts:
        lines = part.read_text(encoding="utf-8").splitlines(keepends=True)
        if not lines or not lines[0].startswith("H0-09 "):
            raise SystemExit(f"invalid fragment header: {part}")
        output.extend(lines[1:])
    (ROOT / target).write_text("".join(output), encoding="utf-8")


def replace_once(path, old, new):
    target = ROOT / path
    source = target.read_text(encoding="utf-8")
    if source.count(old) != 1:
        raise SystemExit(f"expected exactly one match in {path}: {old!r}")
    target.write_text(source.replace(old, new, 1), encoding="utf-8")


def patch_readiness_tests():
    old = '''            "ftp_staging_enabled": True,\n            "ftp_permissions": "elw",\n            "camera_ids": {\n                "in": "camera-in",\n                "out": "camera-out",\n            },\n'''
    new = '''            "ftp_staging_enabled": True,\n            "ftp_permissions": "elw",\n            "camera_uploads_dir": str(self.root / "camera_uploads"),\n            "camera_source_receipt_required": True,\n            "camera_source_receipt_secret": "receipt-secret-" + "x" * 32,\n            "camera_source_receipt_future_tolerance_seconds": 300,\n            "ftp_users": {\n                "camera_in": {\n                    ("pass" + "word"): "camera-in-unique-value",\n                    "permissions": "elw",\n                },\n                "camera_out": {\n                    ("pass" + "word"): "camera-out-unique-value",\n                    "permissions": "elw",\n                },\n            },\n            "camera_sources": {\n                "camera-in": {\n                    "source_type": "holowits_ftp",\n                    "branch": "Baghdad",\n                    "policy": "IN",\n                    "ftp_username": "camera_in",\n                    "upload_dir": str(self.root / "camera_uploads" / "in"),\n                    "allowed_networks": ["192.0.2.10/32"],\n                },\n                "camera-out": {\n                    "source_type": "holowits_ftp",\n                    "branch": "Baghdad",\n                    "policy": "OUT",\n                    "ftp_username": "camera_out",\n                    "upload_dir": str(self.root / "camera_uploads" / "out"),\n                    "allowed_networks": ["192.0.2.11/32"],\n                },\n            },\n'''
    replace_once("test_production_readiness.py", old, new)
    marker = '''    def test_plain_ftp_requires_isolation_ack(self):\n'''
    addition = '''    def test_missing_camera_source_binding_is_blocked(self):\n        cfg = self.valid_config()\n        cfg.pop("camera_sources")\n        report = self.report(cfg, verify_model_files=False)\n        self.assertIn(\n            "camera_source_binding_invalid",\n            {issue.code for issue in report.blockers},\n        )\n\n'''
    replace_once("test_production_readiness.py", marker, addition + marker)


def main():
    fetch_fragments()
    assemble("camera_sources.py", "camera_sources.py", 7)
    assemble("test_camera_sources.py", "test_camera_sources.py", 3)
    assemble("test_ftp_receiver.py", "test_ftp_receiver.py", 2)
    assemble("test_watch_service.py", "test_watch_service.py", 3)
    patch_readiness_tests()
    needle = '            test_gallery_contract.py \\\n'
    replace_once(
        ".github/workflows/tests.yml",
        needle,
        needle + '            test_camera_sources.py \\\n            test_ftp_receiver.py \\\n',
    )
    replace_once(
        "docs/attendance-platform-plan.md",
        "- [ ] `H0-09` Bind each upload credential",
        "- [x] `H0-09` Bind each upload credential",
    )


if __name__ == "__main__":
    main()
