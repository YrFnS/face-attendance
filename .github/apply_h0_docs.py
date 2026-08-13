from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(path, old, new):
    path = ROOT / path
    text = path.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise SystemExit(f"unexpected source state: {path}")
    path.write_text(text.replace(old, new), encoding="utf-8")


replace_once(
    "README.md",
    "- `face_attendance.py` — recognition/check-in helpers and legacy watcher commands.\n",
    "- `face_attendance.py` — recognition/check-in helpers and legacy diagnostic commands that refuse live processing.\n",
)
replace_once(
    "README.md",
    "On a fresh installation, FTP and the web UI start, but the live watcher stays stopped until a valid embedding gallery exists. This prevents accidental checkin creation before enrollment is verified.\n",
    "On a fresh installation, FTP and the web UI start, but the live watcher stays stopped until a valid `embedding_gallery.json` exists. A legacy `embeddings.pkl` never enables the watcher. This prevents accidental checkin creation before enrollment is verified.\n",
)
replace_once(
    "README.md",
    "`face_attendance.py watch` and `watch-folder` are legacy compatibility paths. Do not use them for live production processing because they bypass the production event ledger, PAD, and readiness gate.\n",
    "`watch_service.py` is the only supported live watcher. The legacy `face_attendance.py watch` and `watch-folder` commands fail closed unless `--dry-run` is present, because they do not provide the production readiness, PAD, replay, and event-ledger controls.\n",
)
replace_once(
    "HANDOFF.md",
    "Both are ignored by Git and written with restrictive file permissions where supported. `embeddings.pkl` is legacy local state only. It is converted once when no JSON gallery exists.\n",
    "Both are ignored by Git and written with restrictive file permissions where supported. `embeddings.pkl` is legacy local state only. Service startup never deserializes it; migration requires the explicit provenance-checked offline converter documented in the README.\n",
)
replace_once(
    "HANDOFF.md",
    "Run only the intended FTP receiver and this repository's watcher. Do not run old Frigate/RTSP recognition workers in parallel.\n",
    "Run only the intended FTP receiver and the canonical `watch_service.py` watcher. Stop any legacy, Frigate, or RTSP recognition worker before live delivery.\n",
)
replace_once(
    "docs/security-hardening.md",
    "The production watcher is `watch_service.py`, not the legacy direct folder command. It computes a SHA-256 digest for each completed camera upload and claims an event in SQLite before recognition. The same binary image from the same camera cannot be processed again, including after a restart.\n",
    "The only supported live watcher is `watch_service.py`. Linux systemd and every bundled Windows launcher execute that entry point. The legacy commands refuse execution unless `--dry-run` is present, so they cannot silently bypass readiness, PAD, replay protection, or event state. The canonical watcher computes a SHA-256 digest for each completed camera upload and claims an event in SQLite before recognition. The same binary image from the same camera cannot be processed again, including after a restart.\n",
)
replace_once(
    "docs/production-readiness.md",
    "In production mode, a live watcher start is refused when license acknowledgement, model integrity, PAD, admin authentication, HTTPS acknowledgement, protected camera transport, camera IDs, or required HTTPS service URLs are invalid. Dry-run mode remains available for controlled setup and diagnostics.\n",
    "The systemd service and bundled Windows launchers must execute `watch_service.py`. The legacy watcher paths refuse non-dry-run execution. In production mode, a canonical live watcher start is refused when license acknowledgement, model integrity, PAD, admin authentication, HTTPS acknowledgement, protected camera transport, camera IDs, or required HTTPS service URLs are invalid. Dry-run mode remains available for controlled setup and diagnostics.\n",
)
