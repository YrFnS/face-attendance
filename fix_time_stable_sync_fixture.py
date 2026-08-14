from pathlib import Path


PATH = Path(__file__).resolve().parent / "test_secure_sync.py"


def replace_once(source, old, new):
    count = source.count(old)
    if count != 1:
        raise SystemExit(
            f"expected exactly one fixture match, found {count}: {old!r}"
        )
    return source.replace(old, new, 1)


def main():
    source = PATH.read_text(encoding="utf-8")
    source = replace_once(
        source,
        "        self.private = Ed25519PrivateKey.generate()\n",
        "        self.release_base_time = (\n"
        "            datetime.now(timezone.utc).replace(microsecond=0)\n"
        "            - timedelta(minutes=10)\n"
        "        )\n"
        "        self.private = Ed25519PrivateKey.generate()\n",
    )
    source = replace_once(
        source,
        "            generated_at=f\"2026-08-13T12:{sequence:02d}:00Z\",\n",
        "            generated_at=(\n"
        "                self.release_base_time + timedelta(minutes=sequence)\n"
        "            ).isoformat().replace(\"+00:00\", \"Z\"),\n",
    )
    PATH.write_text(source, encoding="utf-8")


if __name__ == "__main__":
    main()
