from pathlib import Path


path = Path("p1_post_runtime_state.py")
source = path.read_text(encoding="utf-8")
replacements = (
    (
        r'"CREATE UNIQUE INDEX camera_events_camera_hash\n"',
        r'"CREATE UNIQUE INDEX camera_events_camera_hash\\n"',
    ),
    (
        r'"CREATE INDEX camera_events_camera_hash\n"',
        r'"CREATE INDEX camera_events_camera_hash\\n"',
    ),
    (
        'errors.append(f"required index is missing: {index_name}")',
        'errors.append(f"required indexes are missing: {index_name}")',
    ),
)
for old, new in replacements:
    count = source.count(old)
    if count != 1:
        raise SystemExit(f"expected one generator repair match, found {count}: {old}")
    source = source.replace(old, new, 1)
path.write_text(source, encoding="utf-8")
