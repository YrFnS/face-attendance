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

A staging node can enable the Employee Checkin worker and the independent
private crop worker only after installing and pinning the P2-04 ERPNext
contract:

```json
{
  "delivery_mode": "worker",
  "delivery_worker_enabled": true,
  "erpnext_idempotency_required": true,
  "erpnext_expected_site": "approved-site-name",
  "erpnext_expected_idempotency_fingerprint": "64-lowercase-hex-characters",
  "attach_checkin_crop": true,
  "attachment_worker_enabled": true
}
```

Production delivery is blocked unless the companion Frappe app exposes the
approved atomic delivery-ID contract. Static configuration is not enough: the
worker performs an authenticated capability probe before claiming work and
immutably binds every job to the verified site, app/version, method, contract,
and fingerprint. See `docs/erpnext-idempotency.md`.

P2-05 now creates a separate durable attachment job. The Employee Checkin worker never uploads crops. The attachment worker starts only after the parent check-in is confirmed delivered; its retries and failures cannot downgrade that check-in. See `docs/attachment-jobs.md`.

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
- an expired lease after submission starts becomes `retry_wait` only when the
  job already carries a verified P2-04 contract binding; otherwise it becomes
  `uncertain`;
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

Without a verified server binding, only failures known to occur before a remote
commit are retried automatically. Read timeout, connection loss, ambiguous HTTP
responses, lease-heartbeat loss, and local failure after remote success remain
`uncertain`.

With a verified P2-04 binding, those ambiguous outcomes can be replayed with the
same immutable delivery ID. ERPNext either creates the Employee Checkin once or
returns the existing matching document. A delivery-ID payload conflict,
capability drift, authentication failure, or invalid payload remains a permanent
failure.

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

The installer enables the unit and starts it when either worker delivery is active or private crop attachment is enabled. The single service process drains the two independent queues.

Useful inspection remains available through SQLite-backed application methods
and the existing event administration tooling. Do not edit delivery rows
manually; delivery jobs are durable audit records and direct deletion is
blocked.

## Failure recovery

On startup and before each batch, the worker recovers expired delivery leases:

- pre-submission lease expiry -> immediate `retry_wait`, subject to the retry
  budget;
- post-submission lease expiry with verified idempotency -> bounded
  `retry_wait`;
- post-submission lease expiry without verified idempotency -> `uncertain` and
  attendance policy uncertainty;
- exhausted pre-submission retry budget -> `permanent_failure`.

An operator must reconcile genuinely uncertain or contract-conflicting jobs
against ERPNext after `P2-08` is implemented. Until then, no automatic retry or
manual force-delivery command is provided for uncertain work.

## P2-05 private attachment worker

The same service also drains `attachment_jobs`. These jobs remain
`waiting_for_checkin` until their parent delivery is confirmed, then use their
own owner-bound leases, retry schedule, submission boundary, and terminal
result. See `docs/attachment-jobs.md` for spool, retention, and recovery details.
