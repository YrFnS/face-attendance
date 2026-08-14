from pathlib import Path


ROOT = Path(__file__).resolve().parent


def read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def write(path, content):
    (ROOT / path).write_text(content, encoding="utf-8")


def replace_once(path, old, new):
    source = read(path)
    count = source.count(old)
    if count != 1:
        raise SystemExit(
            f"expected exactly one match in {path}, found {count}: {old!r}"
        )
    write(path, source.replace(old, new, 1))


def assemble_event_operations():
    parts = [
        ROOT / f"p1_event_operations_final.{index:02d}.part"
        for index in range(5)
    ]
    missing = [str(path) for path in parts if not path.is_file()]
    if missing:
        raise SystemExit(
            "missing event operation fragments: " + ", ".join(missing)
        )
    content = "".join(path.read_text(encoding="utf-8") for path in parts)
    write("event_operations.py", content)


def patch_runtime_state():
    replace_once(
        "runtime_state.py",
        "from event_ledger import (\n",
        "from event_operations import EventOperationsMixin\n"
        "from event_ledger import (\n",
    )
    replace_once(
        "runtime_state.py",
        "class RuntimeState(ProcessingRecoveryMixin, EventLedgerMixin):\n",
        "class RuntimeState(\n"
        "    EventOperationsMixin,\n"
        "    ProcessingRecoveryMixin,\n"
        "    EventLedgerMixin,\n"
        "):\n",
    )


def patch_tests():
    replace_once(
        "test_runtime_state.py",
        "from test_event_ledger import EventLedgerTests\n",
        "from test_event_admin import EventAdminTests\n"
        "from test_event_ledger import EventLedgerTests\n",
    )


def patch_time_sensitive_sync_fixture():
    replace_once(
        "test_secure_sync.py",
        "        self.private = Ed25519PrivateKey.generate()\n",
        "        self.release_base_time = (\n"
        "            datetime.now(timezone.utc).replace(microsecond=0)\n"
        "            - timedelta(minutes=10)\n"
        "        )\n"
        "        self.private = Ed25519PrivateKey.generate()\n",
    )
    replace_once(
        "test_secure_sync.py",
        "            generated_at=f\"2026-08-13T12:{sequence:02d}:00Z\",\n",
        "            generated_at=(\n"
        "                self.release_base_time + timedelta(minutes=sequence)\n"
        "            ).isoformat().replace(\"+00:00\", \"Z\"),\n",
    )


def patch_plan():
    replace_once(
        "docs/attendance-platform-plan.md",
        "- [ ] `P1-07` Add read-only CLI commands to list, inspect, and explain events without exposing secrets or biometric vectors.",
        "- [x] `P1-07` Add read-only CLI commands to list, inspect, and explain events without exposing secrets or biometric vectors.",
    )
    replace_once(
        "docs/attendance-platform-plan.md",
        "- [ ] `P1-08` Add audited event reprocess, quarantine-resolution, and dismissal commands with required reasons. Delivery retry/cancel begins only after Phase 2 creates delivery jobs.",
        "- [x] `P1-08` Add audited event reprocess, quarantine-resolution, and dismissal commands with required reasons. Delivery retry/cancel begins only after Phase 2 creates delivery jobs.",
    )


def patch_readme():
    replace_once(
        "README.md",
        "- `watch_service.py` — production FTP watcher with readiness, PAD, replay protection, and event state.\n",
        "- `watch_service.py` — production FTP watcher with readiness, PAD, replay protection, and event state.\n"
        "- `event_operations.py` — read-only event inspection plus atomic, audited pre-delivery operator actions.\n"
        "- `event_admin.py` — CLI for event list/inspect/explain, safe reprocess, quarantine review, and dismissal.\n",
    )
    marker = "# Explicit offline migration of a trusted legacy pickle only\n"
    addition = (
        "# Inspect and explain durable event history without modifying SQLite\n"
        "python event_admin.py list --state rejected --limit 50\n"
        "python event_admin.py inspect <event-id>\n"
        "python event_admin.py explain <event-id>\n\n"
        "# Mutating event commands require actor, reason, and explicit confirmation.\n"
        "# Reprocess/requeue additionally require the canonical watcher to be stopped.\n"
        "# See docs/event-operations-cli.md before using these commands.\n\n"
    )
    replace_once("README.md", marker, addition + marker)


def patch_lease_runbook():
    marker = "## Acceptance checks\n"
    addition = (
        "## Event inspection and operator resolution\n\n"
        "Use `event_admin.py` for read-only list, inspect, and explain commands, "
        "and for the narrowly scoped audited pre-delivery actions implemented in "
        "Phase 1. See `docs/event-operations-cli.md`. The CLI deliberately provides "
        "no delivery retry or cancel command before Phase 2 idempotent delivery jobs.\n\n"
    )
    replace_once(
        "docs/processing-leases-policy.md",
        marker,
        addition + marker,
    )


def main():
    assemble_event_operations()
    patch_runtime_state()
    patch_tests()
    patch_time_sensitive_sync_fixture()
    patch_plan()
    patch_readme()
    patch_lease_runbook()


if __name__ == "__main__":
    main()
