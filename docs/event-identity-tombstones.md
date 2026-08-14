# Event identity, released-schema fixtures, and replay tombstones

This document defines the final Phase 1 identifier contract and the schema-version-5 replay boundary for `runtime_state.sqlite3`.

The exact uploaded image bytes, a camera capture, a durable event, a face decision, and an ERPNext delivery are different things. They must not share one overloaded identifier.

## Identifier contract

| Value | Current scheme | Inputs | Meaning |
|---|---|---|---|
| Content hash | `sha256` | Exact uploaded bytes | Evidence for byte-identical content and replay comparison. It is not an event ID. |
| Capture ID | `face-attendance-capture-v2` | Camera ID, content hash, immutable source basename, byte size, source timestamp normalized to microseconds | One local upload envelope. It is audit identity, not proof that physical hardware produced a new observation. |
| Event ID | `face-attendance-event-v2` | Camera ID, `IN`/`OUT`, content hash | The detailed camera-scoped content idempotency key. |
| Recognition decision ID | `face-attendance-decision-v2` | Event ID, one-based face index, numbered decision/processing version | One immutable face decision in one event attempt. |
| Delivery ID | `face-attendance-delivery-v1` | ERPNext delivery-contract version and one accepted recognition-decision ID | One future Employee Checkin delivery. Two accepted faces in one capture receive different delivery IDs. |

All derived IDs are domain-separated SHA-256 values. Equal input text used for two different domains cannot produce the same identifier merely because the fields happen to look alike.

The delivery contract currently defaults to:

```text
erpnext-employee-checkin-v1
```

Phase 1 stores a delivery ID for every accepted decision, but it does not submit by that ID yet. Phase 2 must make `face_attendance_delivery_id` unique in ERPNext or provide an atomic get-or-create endpoint keyed by it. A client-side lookup before create is not enough.

## Why event and tombstone keys are both needed

`camera_events` and its child tables are detailed operational evidence. They include source attribution, timestamps, transitions, PAD evidence, recognition scores, model/gallery versions, policy state, and operator history. Those records may be pruned after the approved detailed-event retention period.

Replay protection must survive that pruning. Schema version 5 therefore creates a separate `event_tombstones` table containing only:

```text
event ID and scheme
capture ID and scheme
camera ID
IN/OUT direction
content hash and algorithm
first received time
```

It deliberately excludes employee identity, recognition scores, PAD evidence, source filenames, receipt JSON, embeddings, and operator notes.

A tombstone is inserted in the same SQLite transaction as a new normalized receipt. If detailed receipt creation fails, both writes roll back. Migration 5 also backfills a tombstone for every retained schema-version-1, -2, -3, or -4 event before activating the new schema.

## Replay behavior

When a camera upload arrives, the ledger checks `event_tombstones` before inserting detailed state.

- A matching tombstone with a live detailed event returns `duplicate` and the current event status.
- A matching tombstone after detailed history was pruned returns `tombstoned`.
- A reused event ID or capture ID bound to different camera content is treated as an identifier collision and fails closed.
- Matching uses camera plus content hash as the long-lived replay scope. A later policy/direction change does not make the same camera bytes eligible again.

The canonical watcher rejects a tombstoned replay before image decoding, PAD, recognition, cooldown, or ERPNext work. When duplicate cleanup is enabled, it removes the replayed image and companion receipt together so the upload route does not loop on the same rejected file.

## Pruning behavior

`RuntimeState.prune_events()` only removes terminal detailed rows. Before deletion it verifies that the event's tombstone exists and matches the event ID, capture ID, camera, and content hash. Missing or conflicting tombstone state aborts the transaction.

Normal detailed-event pruning:

- may prune `processed`, `checkin_created`, `rejected`, `failed`, and `dismissed` events after retention;
- does not prune `uncertain` events;
- does not prune quarantined events;
- cascades detailed decisions, transitions, and operator actions;
- never deletes the corresponding tombstone.

Tombstones are immutable while retained. Direct update and deletion are blocked even after the detailed parent expires. This release has no automatic tombstone-expiry path; an eventual expiry mechanism must be separately approved, audited, tested against the data-retention policy, and must explicitly accept that expired content can become eligible again.

## Released-schema fixtures

`tests/fixtures/` contains frozen synthetic SQLite databases for released schema versions 1, 2, 3, and 4. Each file is stored as base64-encoded gzip with a manifest containing:

```text
source release and commit
migration name and checksum
raw SQLite SHA-256 and size
compressed SHA-256
```

The compatibility tests do not reconstruct old databases from the current migration code. They materialize the committed bytes, verify both digests and the original migration checksum, run the real backup-before-migrate path, and confirm that old event, decision, operator, lease, and policy rows remain readable in schema version 5.

The fixtures contain synthetic records only. They contain no employee photos, embeddings, production credentials, or real attendance information.

## Legacy identifier labels

Existing rows are preserved exactly. Migration 5 does not rewrite historical primary keys. Instead it labels them with:

```text
legacy-event-v1
legacy-capture-v1
legacy-decision-v1
legacy-source-key-v1
```

Their tombstones are backfilled using those legacy IDs. Replay matching by camera plus source key/hash means an old retained or pruned event still blocks an equivalent upload generated under the current identifier scheme.

## Migration and rollback

Before applying schema version 5, the migration framework creates and verifies a schema-version-4 backup and metadata sidecar. Migration 5 then atomically:

1. Adds identifier-scheme columns to events and decisions.
2. Adds delivery ID, delivery scheme, and delivery-contract version fields.
3. Creates and backfills `event_tombstones`.
4. Adds unique tombstone and nonempty-delivery-ID indexes.
5. Adds tombstone immutability protections.
6. Records the migration checksum and activates `PRAGMA user_version = 5`.

Rollback requires the matching schema-version-4 application revision and the verified pre-migration backup. Do not manually remove migration history or decrement `PRAGMA user_version`.
