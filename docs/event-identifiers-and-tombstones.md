# Event identifiers and replay tombstones

This document defines the Phase 1 identifier contract and the minimal replay
state retained after detailed event history expires. It implements `P1-10` and
`P1-11`.

## Identifier contract

The contract version is:

```text
face-attendance-identifiers/v1
```

The identifiers have deliberately different scopes.

### Content hash

`source_sha256` is the SHA-256 digest of the exact uploaded file bytes.

It contains no camera, filename, time, employee, recognition, or delivery data.
Identical bytes therefore have the same digest across cameras. Re-encoding the
same visual image produces different bytes and a different digest; camera
authentication, PAD, policy, and later evaluation remain necessary to address
that residual replay class.

### Capture ID

`capture_id` fingerprints one observed upload from:

```text
camera ID
content SHA-256
source filename
byte size
filesystem modification time rounded to six decimal places
```

Two uploads with the same bytes can have different capture IDs when their
source metadata differs. A capture ID is trace metadata, not the ERPNext
idempotency key.

### Event ID

`event_id` remains the existing local compatibility key:

```text
camera ID + IN/OUT direction + content SHA-256
```

A repeated upload of the same bytes through the same camera/direction scope has
the same event ID even when its capture ID differs. This behavior is preserved
for existing databases. Phase 2 must not use the local event ID as the ERPNext
idempotency key.

### Recognition-decision ID

`decision_id` is derived from:

```text
event ID + face index + decision version
```

The processing attempt is the decision version. A reprocessed event therefore
appends new decision IDs without overwriting earlier face evidence. Different
faces in one capture receive different decision IDs.

### Delivery ID

`delivery_id` is domain-separated and derived from exactly one immutable
recognition-decision ID:

```text
SHA-256("face-attendance/delivery/v1" + NUL + decision_id)
```

Every retry of the same future delivery job must reuse that delivery ID. A
different face or a later processing generation receives a different delivery
ID. Multiple accepted faces from one capture may share the capture trace but
must never share a delivery ID.

Phase 1 derives and exposes delivery IDs but does not yet create delivery jobs
or claim exactly-once ERPNext behavior.

## Replay tombstones

Schema version 5 adds `event_idempotency_tombstones`.

Normal event pruning now runs in one SQLite write transaction:

1. select only old, resolved, terminal events;
2. refuse to prune uncertain, quarantined, leased, or policy-reserved work;
3. insert one minimal tombstone for each selected event;
4. delete the detailed parent event and cascade its decisions, transitions, and
   operator actions;
5. commit the tombstone and detailed deletion together.

The tombstone stores only:

```text
event ID
capture ID
camera ID
IN/OUT direction
content SHA-256
identifier-contract version
original received time
pruned time
final lifecycle state
final reason code
```

It does not retain employee identity, scores, PAD detail, model versions,
source paths, filenames, receipt JSON, operator notes, crops, or embeddings.

The table has unique camera/content and capture indexes. A database trigger
also refuses a direct `camera_events` insert that matches a tombstoned event,
capture, or camera/content scope.

## Duplicate behavior after pruning

When the same exact bytes arrive again through the same camera scope,
`record_event_receipt` returns:

```text
accepted = false
reason = tombstone
existing_status = pruned
```

The canonical watcher stops before image decoding, PAD, recognition, policy, or
ERPNext delivery. A different camera or different exact content remains a
separate event.

## Tombstone retention

Normal detailed-event pruning never deletes tombstones. There is intentionally
no Phase 1 delivery retry, tombstone deletion, or automatic tombstone expiry
command.

A later retention implementation must be explicit, audited, and coordinated
with the approved replay-risk window. Deleting tombstones can make old exact
content eligible again, so it must never be coupled silently to ordinary event
or media cleanup.

Treat content hashes as biometric-adjacent security metadata: keep them out of
logs and exports that do not need them, protect backups, and apply the approved
access and retention policy.
