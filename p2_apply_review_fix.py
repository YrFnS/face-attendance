from pathlib import Path


def replace_once(path, old, new):
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(
            f"{path}: expected one replacement target, found {count}: {old!r}"
        )
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "processing_recovery.py",
    "                  AND lease_expires_unix >= ?\n",
    "                  AND lease_expires_unix > ?\n",
)
replace_once(
    "processing_recovery.py",
    """            if row["lease_owner"] != owner or float(
                row["lease_expires_unix"] or 0
            ) < now:
""",
    """            if row["lease_owner"] != owner or float(
                row["lease_expires_unix"] or 0
            ) <= now:
""",
)
replace_once(
    "test_delivery_outbox.py",
    "from runtime_state import (\n",
    "from processing_recovery import ProcessingLeaseError\nfrom runtime_state import (\n",
)
replace_once(
    "test_delivery_outbox.py",
    """    def test_expired_delivery_marks_event_and_job_uncertain(self):
""",
    """    def test_exact_lease_expiry_blocks_renewal_and_delivery(self):
        self.record_event()
        lease = self.state.acquire_event_lease(
            self.event_id,
            owner="watcher-a",
            lease_seconds=60,
            now=1000.0,
        )
        decision_id = self.record_decision(decision_version=lease.attempt)
        self.assertFalse(
            self.state.event_lease_is_current(
                self.event_id,
                owner="watcher-a",
                now=1060.0,
            )
        )
        with self.assertRaisesRegex(
            ProcessingLeaseError,
            "missing, expired, or owned by another worker",
        ):
            self.state.renew_event_lease(
                self.event_id,
                owner="watcher-a",
                lease_seconds=60,
                now=1060.0,
            )
        with self.assertRaisesRegex(
            ProcessingLeaseError,
            "active processing lease",
        ):
            self.state.begin_delivery_attempt(
                self.event_id,
                owner="watcher-a",
                decision_id=decision_id,
                lease_seconds=60,
                transport="rest",
                now=1060.0,
            )
        job = self.state.delivery_job_for_decision(decision_id)
        self.assertEqual(job["state"], "pending")
        self.assertEqual(job["attempt_count"], 0)
        self.assertEqual(
            self.state.get_event(self.event_id)["processing_phase"],
            "pre_delivery",
        )

    def test_expired_delivery_marks_event_and_job_uncertain(self):
""",
)

for path in ("processing_recovery.py", "test_delivery_outbox.py"):
    compile(Path(path).read_text(encoding="utf-8"), path, "exec")
