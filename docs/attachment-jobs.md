# Durable private crop attachment jobs

Phase 2 task `P2-05` separates Employee Checkin creation from private crop
attachment. A confirmed Employee Checkin remains delivered even when the crop
upload later retries, fails permanently, or has an uncertain result.

## Boundary

An accepted recognition decision can create two durable jobs:

```text
Employee Checkin delivery job
private crop attachment job
```

The records are linked by the immutable delivery and recognition-decision IDs,
but have independent states, attempts, leases, errors, and terminal outcomes.
The attachment job cannot be claimed until the parent delivery job is confirmed
`delivered` and has an ERPNext Employee Checkin document name.

## Transaction and filesystem ordering

The accepted crop is copied into a private mode-`0700` spool before the SQLite
decision transaction begins. The spool filename is derived from the immutable
recognition-decision ID and contains no employee name.

The following records are then committed together:

```text
accepted recognition_decision
delivery_job
attachment_job
```

A failure to insert either job rolls back the accepted decision. The watcher
removes a newly copied spool file when the transaction fails normally. A crash
before the transaction commits can leave an unreferenced file; the attachment
worker removes only unreferenced files older than
`attachment_orphan_grace_seconds`.

If the crop cannot be safely spooled, the Employee Checkin job is still created.
A terminal attachment failure is recorded in the same decision transaction so
the missing audit media is visible without turning a valid attendance decision
into a failed check-in.

## Schema version 8

Migration 8 creates `attachment_jobs`. Each row stores:

```text
attachment, delivery, decision, and event IDs
immutable source path, SHA-256, size, filename, and content type
source state: available / deleted / missing
waiting, retry, lease, submission, and terminal state
confirmed Employee Checkin document name
attempt count and next attempt time
lease owner, heartbeat, and expiry
bounded classified error
ERPNext File document name and URL
creation and completion timestamps
```

Job identity and terminal state are protected by database triggers. Direct
attachment-job deletion is refused.

## Lifecycle

```text
waiting_for_checkin
  -> pending
  -> leased
  -> attached
       or retry_wait
       or permanent_failure
       or uncertain
       or cancelled
```

`waiting_for_checkin` becomes `pending` only after the linked Employee Checkin
job is `delivered`. A parent `permanent_failure` or `cancelled` state cancels the
attachment job. A parent `uncertain` state leaves the crop protected while
reconciliation determines whether the Employee Checkin exists.

## Retry and uncertainty

Safe retries use a bounded exponential schedule with jitter. Current examples:

```text
connect timeout before upload -> retry
HTTP 429 -> retry using bounded Retry-After
validation/authentication/file-too-large -> permanent failure
read timeout after upload began -> uncertain
connection loss after upload began -> uncertain
lease expiry after submission began -> uncertain
```

The attachment upload endpoint is not treated as idempotent. An ambiguous
post-submit result is therefore never retried automatically, because doing so
could create duplicate ERPNext `File` records. P2-08/P2-09 will provide
reconciliation and audited resolution.

## Retention

The normal accepted/rejected crop cleanup does not scan the attachment spool.
An event cannot be pruned while its attachment job is waiting, pending, leased,
retrying, or uncertain. After the job reaches `attached`, `permanent_failure`,
or `cancelled`, detailed event retention may proceed. The durable attachment
row remains available for later audit and dead-letter handling.

On confirmed attachment success, the default is to delete the local spool copy
and record `source_state=deleted`. Set
`attachment_delete_spool_after_success=false` only when an approved retention
policy requires a second local copy.

## Configuration

```json
{
  "attach_checkin_crop": true,
  "attachment_worker_enabled": true,
  "attachment_spool_dir": "/opt/face-attendance/attachment_spool",
  "attachment_max_image_bytes": 10485760,
  "attachment_worker_poll_seconds": 2.0,
  "attachment_worker_batch_size": 10,
  "attachment_worker_lease_seconds": 120,
  "attachment_worker_heartbeat_seconds": 20.0,
  "attachment_worker_max_attempts": 6,
  "attachment_retry_base_seconds": 10.0,
  "attachment_retry_max_seconds": 3600.0,
  "attachment_retry_jitter_fraction": 0.2,
  "attachment_queue_max_active_jobs": 10000,
  "attachment_queue_min_free_bytes": 536870912,
  "attachment_orphan_grace_seconds": 3600,
  "attachment_delete_spool_after_success": true
}
```

In worker delivery mode, `attachment_worker_enabled` is required whenever
`attach_checkin_crop` is enabled. Synchronous compatibility mode still performs
a separate best-effort attachment call without durable retries. The same
`face-attendance-delivery.service` supervises both durable queues.

## Deployment

1. Stop the watcher and delivery service.
2. Create a verified runtime-state backup.
3. Deploy the new code and run `runtime_state_admin.py migrate`.
4. Create the spool directory for the service account with mode `0700`.
5. Validate configuration and strict readiness.
6. Start `face-attendance-delivery.service`.
7. Submit one controlled check-in with a crop.
8. Confirm the Employee Checkin reaches `delivered` before its attachment job
   becomes claimable.
9. Simulate an attachment failure and verify the Employee Checkin remains
   delivered.

Rollback requires the matching schema-7 application revision and the verified
schema-7 backup created before migration.
