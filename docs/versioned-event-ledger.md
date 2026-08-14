# Versioned event ledger and recognition evidence

Phase 1 schema version 2 turns `runtime_state.sqlite3` into the durable evidence ledger for every camera capture. The ledger records a normalized receipt before time, size, decoding, PAD, recognition, cooldown, or ERPNext policy can reject the upload.

This slice implements `P1-03` and `P1-04`. It does **not** yet make ERPNext delivery asynchronous or exactly-once; the current synchronous delivery path remains until Phase 2 introduces the outbox and ERPNext-side idempotency contract.

## Identity and time fields

The ledger keeps these identifiers distinct:

- `event_id`: current camera, direction, and content-hash idempotency identity.
- `capture_id`: deterministic capture identity using camera, content hash, source filename, byte size, and observed filesystem time.
- `source_sha256`: complete source-file digest.
- `decision_id`: immutable hash of event ID, face index, and decision version.

`received_at` is the attendance node's immutable observation time and is the default `effective_at`. The signed FTP receipt time is stored separately as `transport_received_at`. Filesystem modification time is stored as `source_at` with provenance `filesystem_mtime_untrusted`; it is not silently promoted to authoritative attendance time.

Phase 2 will add a separate immutable delivery ID. Phase 1 does not reuse a capture or event identifier as an ERPNext idempotency key.

## Schema version 2

Migration 2 expands `camera_events` with:

- Capture, receipt, source, branch, camera, and policy identity.
- Immutable receipt, source, transport, and effective times.
- Receipt verification state and normalized receipt JSON.
- Lifecycle state, state version, stable reason code, and final disposition.
- Gallery, recognition model, preprocessing, PAD, and policy versions.
- Source and crop retention outcome.

It adds three history tables:

### `recognition_decisions`

One immutable row per detected face and decision version. It stores:

- Face index/count and bounding box.
- Width, height, and detector score.
- Best employee, best score, runner-up score, and margin.
- PAD pass/skip status, score, provider, model, evidence ID, and binding ID.
- Accepted/rejected result and stable reason code.
- Candidate direction and exact gallery/model/preprocessing/policy versions.
- Retention outcome.

Complete face embeddings are never written to the event ledger.

### `event_transitions`

An append-only lifecycle history. Every row records:

- Ordered per-event sequence.
- Previous and next state.
- Stable reason code.
- Actor type and optional actor ID.
- Bounded JSON detail.
- Timestamp.

The mutable `camera_events.lifecycle_state` is a summary only; explanations come from the transition history.

### `operator_actions`

An append-only operator record for future reprocess, quarantine, dismissal, and review actions. This slice creates and protects the table; audited mutation commands arrive in `P1-08`.

Update and direct-delete triggers prevent decisions, transitions, and operator actions from being rewritten. Normal parent-event retention deletion may cascade to detailed history. Long-lived replay tombstones are intentionally deferred to `P1-10`.

## Stable reason codes

The initial reason catalog includes:

```text
receipt_recorded
source_verified
source_binding_invalid
upload_too_large
future_timestamp
stale_event
unreadable_image
image_too_large
no_face
pad_face_limit
pad_single_face_required
pad_rejected
recognition_started
quality_rejected
unknown_employee
score_below_threshold
margin_below_threshold
duplicate_face
accepted_candidate
cooldown_suppressed
checkin_created
processed_no_checkin
processing_failed
invalid_upload
unexpected_error
```

New code must add a reviewed stable reason instead of storing arbitrary exception text as the only explanation. Bounded exception text may remain in the compatibility `error` field for diagnostics.

## Watcher ordering

For a stable file on a configured camera route, the watcher now performs this order:

1. Read file metadata and calculate the complete content digest.
2. Resolve the explicit camera source and create event/capture IDs.
3. Insert the normalized `received` ledger row and first transition.
4. Enforce the configured upload-size limit.
5. Verify the signed source receipt and record its evidence.
6. Enforce time, decode, pixel, and face-count rules.
7. Record PAD-only rejected face decisions when recognition cannot proceed.
8. Record gallery/model/PAD/policy versions before recognition.
9. Persist every recognition decision through a callback before check-in side effects.
10. Record the final lifecycle state and retention outcome.

A duplicate content identity is not inserted twice. A new receipt is therefore never used to bypass the existing replay rule.

## Migration and rollback

Use the Phase 1 migration commands from `docs/runtime-state-migrations.md`:

```bash
python runtime_state_admin.py status \
  --database /opt/face-attendance/runtime_state.sqlite3

python runtime_state_admin.py migrate \
  --database /opt/face-attendance/runtime_state.sqlite3

python runtime_state_admin.py verify \
  --database /opt/face-attendance/runtime_state.sqlite3
```

Migration 2 creates and verifies a schema-v1 backup before changing an existing database. Rollback requires the matching application revision and the matching verified pre-migration backup.

## Operational boundary

Do not enable this Phase 1 branch as a claim of reliable live ERPNext delivery. Recognition still invokes the existing synchronous ERPNext function, so a network loss after a remote commit remains ambiguous. Deploy Phase 1 and Phase 2 together in shadow mode before changing the production delivery path.

The next ledger slices are:

- `P1-05`: processing leases and startup recovery.
- `P1-06`: transactional cooldown and policy state.
- `P1-07`: read-only list, inspect, and explain commands.
- `P1-08`: audited reprocess, quarantine resolution, and dismissal.
- `P1-10`: long-lived idempotency tombstones after detailed-event pruning.
