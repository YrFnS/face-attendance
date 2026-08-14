# Processing leases, startup recovery, and attendance policy state

This document describes the Phase 1 processing-lease and transactional attendance-policy controls used by the canonical `watch_service.py` path.

These controls replace permanent `processing` claims and the old `cooldown_state.json` / `cooldown_state.lock` files. They do not make the current synchronous ERPNext delivery exactly once. Phase 2 must still introduce the durable delivery outbox and the ERPNext-side idempotency contract.

## Configuration

```json
{
  "event_processing_lease_seconds": 180,
  "event_startup_recovery_enabled": true,
  "attendance_policy_reservation_seconds": 300,
  "cooldown_seconds": 600
}
```

Production requirements:

- `event_processing_lease_seconds` must be between 30 and 3600 seconds.
- `attendance_policy_reservation_seconds` must be between 30 and 86400 seconds and must not be shorter than the processing lease.
- `cooldown_seconds` must be between 0 and 86400 seconds.
- `event_startup_recovery_enabled` must be `true`.

The processing lease must comfortably exceed the normal local recognition time and the synchronous ERPNext request timeout. It is renewed at durable processing boundaries. The reservation window must cover the same operation so another worker cannot submit the same employee/direction policy scope while the first attempt is unresolved.

## Processing lease model

Each nonterminal `camera_events` row records:

```text
processing_attempt
lease_owner
lease_acquired_at
lease_heartbeat_at
lease_expires_unix
recovery_count
processing_phase
```

`processing_phase` is one of:

```text
idle
pre_delivery
delivery_in_progress
terminal
```

A watcher instance receives a unique owner ID containing its host, process ID, and a random component. Before processing an event, it must acquire the lease in one SQLite `BEGIN IMMEDIATE` transaction.

An unexpired lease owned by another worker blocks processing. A lease owned by the same worker can be renewed. An expired pre-delivery lease can be acquired by a replacement worker as a new processing attempt.

Recognition decision IDs include the processing-attempt number as the decision version. A recovered attempt therefore appends a new immutable decision row instead of overwriting the evidence from an interrupted attempt.

## Safe pre-delivery recovery

Work is considered pre-delivery until the watcher has:

1. persisted the accepted recognition decision;
2. reserved the employee/direction policy scope; and
3. durably marked `delivery_in_progress` immediately before invoking ERPNext.

A crash before that boundary is recoverable because no remote submission should have started. Startup recovery:

- clears the expired lease;
- releases any pending policy reservation for that event;
- appends a recovery transition;
- leaves the event nonterminal and eligible for a new processing attempt;
- verifies that the original upload is still available on its configured camera route.

When the source upload is missing, recovery fails closed and marks the event failed. It does not silently discard the unfinished event.

The same lease-aware behavior applies when the watcher encounters the upload again during normal polling. A duplicate content hash is not automatically deleted while its event is nonterminal.

## Delivery ambiguity

Immediately before the synchronous ERPNext call, the watcher persists:

```text
processing_phase = delivery_in_progress
delivery_started_at
delivery_decision_id
```

Any crash, timeout, connection reset, or local failure after this boundary is treated as ambiguous. The application cannot prove whether ERPNext committed the Employee Checkin.

An expired delivery-phase lease is therefore never retried automatically. The event becomes:

```text
lifecycle_state = uncertain
status = uncertain
processing_phase = terminal
```

Its policy reservation becomes `uncertain`, and the source evidence is quarantined or retained according to the configured rejection policy. A future operator/reconciliation workflow must resolve the event. This deliberately favors a visible missing check-in over an automatic duplicate.

Phase 2 will replace this conservative synchronous boundary with a durable delivery job, server-enforced delivery ID, retry classification, and reconciliation.

## Transactional attendance policy

The old filesystem cooldown map and exclusive lock file are removed. SQLite now stores policy state in `attendance_policy_state`.

The policy scope is the hash of:

```text
employee
IN or OUT direction
branch
attendance policy version
```

This means:

- a repeated `IN` for the same employee, branch, and policy inside the cooldown window is suppressed;
- a legitimate `OUT` remains independently eligible during that same interval;
- changing branch or policy version creates a distinct scope;
- all competing workers serialize through SQLite instead of a crash-strandable filesystem lock.

Cooldown comparisons use the event's immutable `effective_at`, not the current retry time.

## Reservation lifecycle

A candidate must reserve its policy scope before remote delivery. The reservation records the event ID, recognition-decision ID, effective time, state, and expiry.

Reservation states are:

```text
none
pending
uncertain
```

`pending` prevents another worker from submitting the same scope while the first attempt is active. A pre-delivery failure releases it. Successful ERPNext creation commits the reservation as the latest accepted policy state. An ambiguous delivery marks it `uncertain` and blocks that same scope until reconciliation or a future audited operator action resolves it.

The committed cooldown state and the policy reservation are durable across process and host restarts.

## Startup sequence

The canonical watcher performs recovery after opening and migrating `runtime_state.sqlite3`, and before model loading or new event processing:

1. classify every nonterminal event with an absent or expired lease;
2. mark delivery-phase events `uncertain`;
3. release pre-delivery policy reservations and queue safe retries;
4. verify the original upload for each retry;
5. mark missing-source retries failed;
6. start the normal watcher loop with a new unique worker ID.

Only one canonical watcher should run per attendance node. SQLite leases protect accidental overlap, but they are not a substitute for the documented single-writer deployment topology.

## Migration and rollback

Schema version 3 adds the processing columns and `attendance_policy_state`. The existing migration framework:

- creates and verifies a schema-version-2 backup before migration;
- runs migration 3 in one transaction;
- verifies the new columns and indexes;
- preserves existing events and recognition evidence.

Before deploying:

```bash
sudo systemctl stop face-attendance-watch.service
sudo systemctl stop face-attendance-ftp.service
sudo systemctl stop face-attendance-web.service
sudo systemctl stop face-attendance-sync.timer

cd /opt/face-attendance
source .venv/bin/activate
python runtime_state_admin.py migrate \
  --database /opt/face-attendance/runtime_state.sqlite3
python runtime_state_admin.py verify \
  --database /opt/face-attendance/runtime_state.sqlite3
```

Rollback requires the matching older application revision and the verified schema-version-2 pre-migration backup. Follow `docs/runtime-state-migrations.md` rather than editing schema versions or policy rows manually.

## Acceptance checks

Before a shadow deployment, verify all of these cases:

- a second worker cannot acquire an unexpired lease;
- an expired pre-delivery lease creates a new attempt and preserves prior decisions;
- a missing source during recovery becomes an explainable failed event;
- a crash after `delivery_in_progress` becomes `uncertain` and is not retried;
- repeated same-direction attendance inside cooldown is suppressed;
- opposite-direction attendance inside that interval remains eligible;
- an uncertain reservation blocks only its exact employee/direction/branch/policy scope;
- ERPNext receives the immutable event `effective_at` rather than restart or retry time;
- no `cooldown_state.json` or `cooldown_state.lock` file is created.
