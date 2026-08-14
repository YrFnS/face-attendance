# Event inspection and audited operator workflows

`event_admin.py` provides the Phase 1 operator boundary for the local attendance event ledger. It supports read-only investigation and tightly constrained local event actions. It does **not** retry or cancel ERPNext delivery. Delivery controls begin only after Phase 2 adds durable delivery jobs, server-enforced delivery IDs, and reconciliation.

## Safety model

- Read-only commands open SQLite with `mode=ro` and `PRAGMA query_only=ON`.
- Output redacts secret-shaped fields and values, including tokens, credentials, authorization headers, signatures, passwords, private keys, and any embedding/vector/template field.
- Local filesystem paths are reduced to basenames unless `--include-paths` is explicitly supplied.
- Complete biometric embeddings are never stored in the event ledger.
- Every successful mutation creates an append-only `operator_actions` row and an append-only event transition.
- Every mutation requires a stable actor, a human reason of at least five characters, and `--confirm`.
- Reprocessing is refused after the synchronous ERPNext delivery boundary or while another worker owns an unexpired lease.
- Uncertain events can be dismissed only after `--acknowledge-erpnext-checked` confirms that ERPNext was reviewed.
- There are deliberately no delivery retry or delivery cancel commands in this CLI.

## Read-only commands

List recent events:

```bash
python event_admin.py list \
  --database /opt/face-attendance/runtime_state.sqlite3 \
  --limit 50
```

Useful filters include:

```text
--state rejected
--reason pad_rejected
--camera entrance-in
--branch Baghdad
--direction IN
--employee HR-0001
--since 2026-08-14T00:00:00Z
--until 2026-08-15T00:00:00Z
```

Inspect the complete sanitized event record:

```bash
python event_admin.py inspect EVENT_ID \
  --database /opt/face-attendance/runtime_state.sqlite3
```

Explain the event as a compact timeline with recognition and PAD evidence:

```bash
python event_admin.py explain EVENT_ID \
  --database /opt/face-attendance/runtime_state.sqlite3
```

Only trusted local administrators should add `--include-paths`.

All event CLI commands require the latest verified schema and never apply migrations automatically. Use `runtime_state_admin.py migrate` during a controlled release before using either read-only or mutation commands.

## Reprocess a safe pre-delivery event

Review the explanation first. Then run:

```bash
python event_admin.py reprocess EVENT_ID \
  --database /opt/face-attendance/runtime_state.sqlite3 \
  --config /opt/face-attendance/config.json \
  --actor operator@example.com \
  --reason "Verified the retained source and approved another recognition attempt" \
  --confirm
```

Use `--media-path` when an older migrated event has no recorded retention path.

The command verifies that the media is a regular non-symlink file and that its complete SHA-256 and byte size match the immutable event receipt. A companion source receipt is required when the original event says the receipt was verified. The command also verifies that the event camera, branch, source type, and direction still match the current camera registry.

The media is first moved to a watcher-hidden staging name inside the bound camera route. The database then records the operator action and gives that action a short SQLite lease. While this lease exists, a watcher cannot consume or delete the file. The staged file and receipt are published atomically, the operator lease is cleared, and the watcher can acquire a new numbered processing attempt.

If publication fails, the CLI restores the evidence where possible and appends a separate `reprocess_publish_failed` action. If the process dies after staging but before the database action commits, startup recovery verifies the full event ID and content hash encoded by the hidden staging name and rolls the evidence back to its recorded retention path. If it dies after the database action commits, recovery uses the recorded target and hidden stage to finish the approved publication or explicitly fail the event.

Reprocessing is refused for:

- `checkin_created` events;
- `uncertain` events;
- any event with `delivery_started_at` or `delivery_in_progress`;
- an event with another active lease;
- media whose hash or size differs from the original receipt;
- a camera binding that changed branch, source type, or direction.

## Resolve quarantined evidence

Requeue quarantined evidence:

```bash
python event_admin.py resolve-quarantine EVENT_ID \
  --resolution reprocess \
  --database /opt/face-attendance/runtime_state.sqlite3 \
  --config /opt/face-attendance/config.json \
  --actor operator@example.com \
  --reason "Reviewed the quarantine cause and approved reprocessing" \
  --confirm
```

Dismiss quarantined evidence without deleting it:

```bash
python event_admin.py resolve-quarantine EVENT_ID \
  --resolution dismiss \
  --database /opt/face-attendance/runtime_state.sqlite3 \
  --actor operator@example.com \
  --reason "Evidence is invalid and must remain dismissed for audit" \
  --confirm
```

The event must currently have `retention_state=quarantined`. Dismissal records the resolution but leaves media retention/deletion to the approved retention workflow.

## Dismiss an event

```bash
python event_admin.py dismiss EVENT_ID \
  --database /opt/face-attendance/runtime_state.sqlite3 \
  --actor supervisor@example.com \
  --reason "Reviewed locally and no further local processing is authorized" \
  --confirm
```

For an uncertain or delivery-started event, first verify ERPNext and add:

```text
--acknowledge-erpnext-checked
```

The acknowledgement is stored in the append-only operator action. A delivered `checkin_created` event cannot be dismissed locally; corrections or deletion must happen in ERPNext and later be reconciled back to the local ledger.

## Schema version 4

Migration 4 adds:

```text
source_path
retention_path
operator_revision
last_operator_action_id
last_operator_action_at
```

It also adds query indexes and a trigger that makes a nonempty `source_path` write-once. Existing schema-version-3 rows remain readable; they may have blank source and retention paths and therefore require `--media-path` for reprocessing.

The existing migration framework creates and verifies a schema-version-3 backup before applying migration 4. Rollback requires the matching PR #15 application revision and that verified backup.

## Operational acceptance

Before shadow deployment, verify:

- list, inspect, and explain do not modify the database;
- secret-shaped receipt and error values are redacted;
- a hash or size mismatch blocks reprocessing;
- a symlinked image or receipt is rejected;
- a second worker cannot acquire the operator publication lease;
- successful requeue moves the image and source receipt together;
- an interrupted hidden-stage publication is recovered on startup;
- delivery-started and uncertain events cannot be reprocessed;
- uncertain dismissal requires an ERPNext check acknowledgement;
- operator actions and transitions cannot be updated or deleted directly.
