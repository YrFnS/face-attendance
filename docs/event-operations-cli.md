# Event inspection and audited operator commands

Phase 1 provides a command-line interface for explaining attendance events without shell-level SQL access and for resolving only the event states that are safe before durable ERPNext delivery exists.

The commands are intentionally split into two groups:

- `list`, `inspect`, and `explain` open SQLite with `mode=ro`, set `PRAGMA query_only=ON`, do not run migrations, and do not resolve application secrets.
- `dismiss`, `reprocess`, and `resolve-quarantine` are explicit database mutations. They require an actor, a meaningful reason, and `--confirm`. Reprocessing or moving source evidence additionally requires `--confirm-watcher-stopped`.

There is no Phase 1 command to retry or cancel ERPNext delivery. Once delivery may have started, the event must be reconciled against ERPNext after Phase 2 provides server-enforced idempotency and durable delivery jobs.

## Read-only event listing

```bash
cd /opt/face-attendance
source .venv/bin/activate

python event_admin.py list \
  --database /opt/face-attendance/runtime_state.sqlite3 \
  --state rejected \
  --camera entrance-in \
  --branch Baghdad \
  --direction IN \
  --reason-code no_face \
  --from-time 2026-08-14T00:00:00Z \
  --to-time 2026-08-14T23:59:59Z \
  --limit 50
```

Supported list filters are state, reason code, camera, branch, direction, employee, and inclusive RFC 3339 time bounds. Pagination is server-side through `--limit` and `--offset`; a single invocation is capped at 200 rows.

The list output contains event and capture IDs, camera/branch/direction, lifecycle/reason, source size/name, processing phase, decision counts, first accepted employee, and effective time. It does not return receipt JSON, tokens, passwords, embeddings, or arbitrary audit detail.

## Inspect one event

```bash
python event_admin.py inspect \
  --database /opt/face-attendance/runtime_state.sqlite3 \
  <64-character-event-id>
```

Inspection returns:

- the current event summary;
- normalized receipt evidence;
- append-only transitions;
- recognition decisions and score/margin/PAD/model/gallery versions;
- append-only operator actions;
- attendance-policy rows connected to the event.

The serializer recursively redacts token, password, secret, authorization, cookie, session, signature, private-key, API-key, and API-secret fields. Keys representing embeddings or vectors are replaced with an omission marker even if unexpected data was inserted into an operator detail record. The ledger does not normally store biometric vectors at all.

## Explain an outcome

```bash
python event_admin.py explain \
  --database /opt/face-attendance/runtime_state.sqlite3 \
  <event-id>
```

The explanation combines the transition timeline, recognition decisions, source receipt state, processing lease, policy reservation, and delivery boundary into:

- a current-state headline;
- whether delivery started or is confirmed;
- whether operator reprocessing or dismissal is safe;
- compact face-decision evidence;
- exact gallery/model/PAD/policy versions;
- a chronological timeline;
- the recommended next action.

For an `uncertain` event, the recommendation is always ERPNext reconciliation. The CLI reports `delivery_retry: false` and `delivery_cancel: false` because those actions begin only after Phase 2 creates idempotent delivery jobs.

## Dismiss a safe pre-delivery event

Dismissal is for an event that needs no further attendance action and did not cross the ERPNext delivery boundary.

```bash
python event_admin.py dismiss \
  --database /opt/face-attendance/runtime_state.sqlite3 \
  --actor operator@example.com \
  --reason "Reviewed camera evidence; no attendance correction is required." \
  --confirm \
  <event-id>
```

The command refuses:

- a missing event;
- an active unexpired processing lease;
- a delivered or delivery-started event;
- an event with an uncertain attendance-policy reservation;
- an incompatible lifecycle state.

The operator action and transition to `dismissed` are written in one `BEGIN IMMEDIATE` transaction. The prior state and prior reason remain available in append-only history.

## Reprocess retained source evidence

Reprocessing is limited to processed-without-delivery, rejected, failed, dismissed, or quarantined events where ERPNext delivery never started.

Stop the canonical watcher first:

```bash
sudo systemctl stop face-attendance-watch.service
```

Verify the cause is corrected and run:

```bash
python event_admin.py reprocess \
  --database /opt/face-attendance/runtime_state.sqlite3 \
  --config /opt/face-attendance/config.json \
  --actor operator@example.com \
  --reason "Camera obstruction was corrected and the original signed source was verified." \
  --confirm \
  --confirm-watcher-stopped \
  <event-id>
```

Before resetting the event, the command verifies:

- the source is on the exact configured camera upload route;
- the source is a regular non-symlink file;
- its byte size and SHA-256 match the immutable event receipt;
- its HMAC source receipt verifies against the configured camera binding;
- the event has no active lease, delivery marker, or uncertain policy reservation.

The transaction appends an operator action and transition, clears only terminal/lease/delivery-attempt summary fields, preserves all earlier decisions and transitions, and returns the event to `received`. The next watcher lease creates a new processing attempt and appends a new decision version instead of overwriting prior evidence.

Restart the watcher only after the command succeeds:

```bash
sudo systemctl start face-attendance-watch.service
```

## Resolve quarantined evidence

### Retain after review

```bash
python event_admin.py resolve-quarantine \
  --database /opt/face-attendance/runtime_state.sqlite3 \
  --config /opt/face-attendance/config.json \
  --resolution retain \
  --actor auditor@example.com \
  --reason "Retain evidence through the approved incident-review window." \
  --confirm \
  <event-id>
```

The command performs a bounded scan below `logs/quarantine`, matches the immutable source name, size, and SHA-256, and records an append-only `quarantine_retained` action. It does not retry recognition or delivery.

### Requeue after correcting the cause

Stop the watcher, then run:

```bash
python event_admin.py resolve-quarantine \
  --database /opt/face-attendance/runtime_state.sqlite3 \
  --config /opt/face-attendance/config.json \
  --resolution requeue \
  --actor operator@example.com \
  --reason "The source and signed receipt were verified after correcting the rejection cause." \
  --confirm \
  --confirm-watcher-stopped \
  <event-id>
```

The command:

1. finds exactly one matching quarantined source;
2. requires the companion source receipt;
3. refuses an existing destination image or receipt;
4. moves both files back to the camera's configured upload route;
5. verifies content and the signed camera-source receipt at the destination;
6. atomically records `quarantine_requeued` and returns the event to `received`;
7. moves the files back to quarantine if database requeue fails.

The default scan stops after 5,000 regular files. `--max-scan` can raise that bound to at most 100,000 for a controlled investigation.

## Required operational rules

- Run mutating commands as the application service user so SQLite ownership and systemd/file secret checks remain valid.
- Do not edit event, decision, transition, policy, or operator-action tables manually.
- Keep the watcher stopped for any command that makes a source file visible on its camera route.
- Never use reprocess to handle `uncertain`, `checkin_created`, or delivery-started events.
- Delivered Employee Checkin correction or deletion remains an ERPNext operation and should later be annotated locally through the Phase 4 control plane.
- Preserve source, quarantine, event, and backup evidence under the approved biometric-retention policy.
