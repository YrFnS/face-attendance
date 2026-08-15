# ERPNext adapter and transactional delivery outbox

Phase 2 begins by separating ERPNext transport code from recognition and by
persisting every accepted recognition delivery before any network side effect.

This slice implements:

- `P2-01` — an explicit ERPNext adapter interface with independently tested REST
  and local-bench transports;
- `P2-02` — durable `delivery_jobs` created in the same SQLite transaction as an
  accepted recognition decision.

It does **not** introduce the background delivery worker, retry scheduler,
ERPNext-side uniqueness constraint, attachment job, reconciliation, or
dead-letter workflow. Those remain P2-03 through P2-09.

## Adapter boundary

`erpnext_adapter.py` defines one transport-neutral request:

```text
employee
IN/OUT log type
immutable effective event time
```

The supported implementations are:

```text
RESTERPNextAdapter
BenchERPNextAdapter
```

`erpnext_transport` may be set explicitly to `rest` or `bench`. Installations
without that key retain the previous compatibility choice: REST is used only
when `frappe_url`, `frappe_api_key`, and `frappe_api_secret` are all present;
otherwise the local bench adapter is used.

The REST adapter validates the base URL, requires HTTPS unless the existing
insecure-development acknowledgement is enabled, creates `Employee Checkin`,
and preserves the current private attachment behavior.

The bench adapter receives explicit execute and attachment callbacks. This
keeps shell/WSL process handling outside the interface and makes the transport
independently testable.

The canonical watcher still calls the synchronous compatibility wrapper in this
slice. The adapter separation exists now so the P2-03 worker can use the same
contract without importing recognition code.

## Schema version 6

Migration 6 adds `delivery_jobs`.

Each job keeps the immutable delivery snapshot needed after detailed event
history is eventually pruned:

```text
delivery ID and scheme
recognition decision ID
local event ID
employee
IN/OUT direction
immutable effective time
camera ID
branch
delivery contract version
creation time
```

Mutable operational state includes:

```text
pending / leased / retry_wait / delivered
permanent_failure / uncertain / cancelled
selected transport
attempt count
next retry time
lease owner and expiry
last classified error
ERPNext document name
delivery timestamp
```

The job has no cascading foreign key to the detailed event ledger. A delivered
job therefore remains available for audit and future reconciliation after the
event's normal retention window.

## Atomic decision and job creation

Schema 6 installs an `AFTER INSERT` trigger on `recognition_decisions`.

For every accepted decision, the trigger copies the immutable event and decision
snapshot into exactly one pending delivery job. A missing event snapshot,
duplicate delivery ID, duplicate decision ID, or invalid delivery identity
aborts the recognition-decision insert. SQLite rolls back both records together.

Rejected decisions must not contain delivery identity and never create jobs.

This means there is no crash window where recognition is accepted but the
delivery task is forgotten.

## Compatibility delivery path

Before the existing synchronous ERPNext call, `begin_delivery_attempt()` now
leases the event and its delivery job in the same transaction.

On remote success, the job records:

```text
state = delivered
transport
ERPNext Employee Checkin document name
delivered_at
```

On a transport exception or an expired/restarted delivery boundary, the job is
marked `uncertain`. It is not automatically retried because the remote commit
may already exist.

A failure to commit the local attendance-policy reservation after ERPNext
success does not downgrade a delivered job. The event and policy state remain
visible as an exception for later reconciliation.

## Migration from schema 5

Accepted schema-5 decisions that already contain a stable delivery ID are
backfilled:

- `checkin_created` events become delivered legacy-synchronous jobs;
- delivery-started or uncertain events become uncertain jobs;
- other accepted decisions become pending jobs.

The old synchronous path did not persist the returned ERPNext document name, so
backfilled delivered jobs intentionally have an empty `remote_docname` until
reconciliation discovers it.

Older released decisions migrated through schema 5 have no durable delivery ID
and remain historical evidence without fabricated jobs.

## Retention

Normal event pruning now refuses to delete detailed events that still have a
pending, leased, retry-wait, or uncertain delivery job.

After a job reaches a safe terminal state, detailed event history may expire,
while both the permanent replay tombstone and durable delivery job remain.

Delivery jobs cannot be directly deleted. A future retention policy must be
introduced through a separately reviewed migration rather than ordinary event
cleanup.

## ERPNext boundary

P2-04 adds the companion Frappe app and authenticated capability proof described
in `docs/erpnext-idempotency.md`. A delivery job is bound to the verified ERPNext
site and contract before submission. This makes response loss and restart after
commit safely replayable with the same immutable delivery ID.

Jobs without that verified binding retain the conservative P2-03 behavior:
ambiguous post-submit outcomes become `uncertain` rather than automatically
retrying.

## P2-03 leased worker

Schema version 7 adds `submission_started_at` and retry-delay evidence. The
single-node worker claims due jobs atomically, renews its lease during ERPNext
calls, retries only provably safe failures, and marks ambiguous post-submission
outcomes `uncertain`. Full configuration and operations are documented in
`docs/delivery-worker.md`.

## P2-04 verified idempotency

Schema version 8 stores the approved ERPNext site, app/version, idempotency
contract, create method, capability fingerprint, and verification timestamp on
the delivery job. The binding becomes immutable after first use. A populated
schema-7 database is backed up and verified before migration.
