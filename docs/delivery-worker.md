# Leased ERPNext delivery worker

`delivery_service.py` implements Phase 2 task `P2-03`. It drains the durable
`delivery_jobs` outbox created transactionally with accepted recognition
decisions.

## Activation boundary

The default remains the compatibility path:

```json
{
  "delivery_mode": "synchronous",
  "delivery_worker_enabled": false
}
```

A staging node can enable the worker with:

```json
{
  "delivery_mode": "worker",
  "delivery_worker_enabled": true,
  "attach_checkin_crop": false
}
```

Production worker mode is intentionally blocked until `P2-04` verifies an
ERPNext-side atomic `face_attendance_delivery_id` contract. The worker does not
claim exactly-once delivery before that dependency exists.

`attach_checkin_crop` must remain false in worker mode until `P2-05` creates a
separate durable attachment job. A crop upload failure must never downgrade a
successfully created Employee Checkin.

## Job lifecycle

A worker atomically claims one due job and records:

```text
state = leased
lease owner
lease acquisition and heartbeat timestamps
lease expiry
attempt count
selected ERPNext transport
```

Immediately before calling ERPNext it records `submission_started_at`. This is
the crash boundary:

- an expired lease with no submission timestamp is safe to requeue;
- an expired lease after submission starts becomes `uncertain`;
- an uncertain job is never retried automatically.

The worker renews its lease while the adapter call is active. Every completion
operation verifies the current unexpired lease owner, so a stale process cannot
finalize a job after another process has recovered it.

## Retry policy

Retries use bounded exponential backoff with configurable jitter:

```json
{
  "delivery_worker_max_attempts": 6,
  "delivery_retry_base_seconds": 5.0,
  "delivery_retry_max_seconds": 900.0,
  "delivery_retry_jitter_fraction": 0.2
}
```

Before server-side idempotency is available, only failures known to be safe are
retried automatically. Current safe cases are connection establishment timeout
and an explicit HTTP `429` response. Validation/configuration failures become
`permanent_failure`. Read timeout, connection loss, ambiguous HTTP responses,
lease-heartbeat loss, and local failure after remote success become
`uncertain`.

`P2-07` will expand the classifier and operational error taxonomy. It must not
weaken the conservative timeout behavior established here.

## Queue and disk safeguards

The watcher checks capacity before accepting a decision into worker mode:

```json
{
  "delivery_queue_max_active_jobs": 10000,
  "delivery_queue_min_free_bytes": 1073741824
}
```

Active capacity includes pending, delayed-retry, leased, and uncertain jobs.
When the limit or disk floor is reached, the face decision is recorded as an
operational failure and no new delivery job is created. Existing jobs remain
available for delivery and reconciliation.

## Service operation

```bash
python delivery_service.py --once --max-jobs 10
python delivery_service.py
```

The Linux unit is:

```text
face-attendance-delivery.service
```

The installer enables the unit but starts it only when both `delivery_mode` is
`worker` and `delivery_worker_enabled` is true.

Useful inspection remains available through SQLite-backed application methods
and the existing event administration tooling. Do not edit delivery rows
manually; delivery jobs are durable audit records and direct deletion is
blocked.

## Failure recovery

On startup and before each batch, the worker recovers expired delivery leases:

- pre-submission lease expiry -> immediate `retry_wait`, subject to the retry
  budget;
- post-submission lease expiry -> `uncertain` and attendance policy uncertainty;
- exhausted pre-submission retry budget -> `permanent_failure`.

An operator must reconcile uncertain jobs against ERPNext after `P2-08` is
implemented. Until then, no automatic retry or manual force-delivery command is
provided for uncertain work.
