import base64
import hashlib
import io
import tarfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PARTS = tuple(ROOT / f"p1_payload.part.{index:02d}" for index in range(4))
EXPECTED_SHA256 = "b035279614b1f46a0481bed380d1c253ef9accebb66ee494447083a06a75c206"


def safe_member(member):
    path = Path(member.name)
    if path.is_absolute() or ".." in path.parts:
        raise SystemExit(f"unsafe payload path: {member.name}")
    if member.issym() or member.islnk() or member.isdev():
        raise SystemExit(f"unsupported payload entry: {member.name}")


def replace_once(path, old, new):
    source = path.read_text(encoding="utf-8")
    count = source.count(old)
    if count != 1:
        raise SystemExit(
            f"expected one occurrence in {path}: {old!r}; found {count}"
        )
    path.write_text(source.replace(old, new, 1), encoding="utf-8")


def main():
    encoded = "".join(part.read_text(encoding="ascii") for part in PARTS)
    payload = base64.b64decode(encoded, validate=True)
    actual = hashlib.sha256(payload).hexdigest()
    if actual != EXPECTED_SHA256:
        raise SystemExit(
            f"P1 payload checksum mismatch: expected {EXPECTED_SHA256}, got {actual}"
        )

    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        members = archive.getmembers()
        for member in members:
            safe_member(member)
        archive.extractall(ROOT, members=members)

    plan = ROOT / "docs" / "attendance-platform-plan.md"
    replace_once(
        plan,
        "- [ ] `P1-01` Add an explicit schema-version table and transactional forward migrations for `runtime_state.sqlite3`.",
        "- [x] `P1-01` Add an explicit schema-version table and transactional forward migrations for `runtime_state.sqlite3`.",
    )
    replace_once(
        plan,
        "- [ ] `P1-02` Add backup-before-migrate, migration verification, and documented rollback/restore commands.",
        "- [x] `P1-02` Add backup-before-migrate, migration verification, and documented rollback/restore commands.",
    )


if __name__ == "__main__":
    main()
