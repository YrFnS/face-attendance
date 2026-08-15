# Face Attendance Platform Improvement Plan

- **Status:** Proposed
- **Baseline:** `master` at `01f6e2391c6a01f396aeb68e00a8c2f7e34c5c34`
- **Last updated:** 2026-08-13
- **Goal:** Make this repository the safer, more reliable, and more operable attendance product while retaining ERPNext as the HR system of record.

## 1. Outcome

The target product is an attendance edge and operations platform, not a second HR database:

```text
HOLOWITS camera
  -> protected upload and durable event ledger
  -> face quality, recognition, ambiguity, and PAD decision
  -> explicit camera policy
  -> durable ERPNext delivery queue
  -> Employee Checkin plus optional private audit crop
  -> operator review, retry, reconciliation, and metrics
```

A trusted enrollment service owns reference images and publishes a validated, versioned embedding gallery. Attendance nodes receive embeddings, not employee photos. ERPNext remains authoritative for employees, departments, shifts, holidays, attendance interpretation, and reporting.

The initial supported topology is one branch and one active gallery per attendance node, with one trusted enrollment publisher. The local authenticated UI operates one node. A future fleet or multi-branch control plane requires an explicit aggregation design; this plan does not imply that a single UI already manages every site.

The work is complete only when an operator can enroll an employee, verify every camera on the node, explain every recognition decision, survive service and ERPNext outages without losing or duplicating check-ins, and reconcile the final result from the authenticated operations UI.

## 2. Product boundary

### Build here

- Camera ingestion, identity, direction, and health.
- Durable event capture and recognition evidence.
- Face matching, quality gates, PAD integration, and calibration tooling.
- Reliable ERPNext delivery, retry, and reconciliation.
- Central enrollment and embedding-gallery lifecycle.
- Operational UI, audit trail, alerts, diagnostics, and deployment controls.
- Explicit entry, exit, and presence-camera policies when those modes are required.

### Keep in ERPNext

- Employee master data and employment status.
- Departments, shifts, assignments, holidays, and payroll policy.
- Canonical Employee Checkin and Attendance records.
- Business reports and final working-hours calculations.

The control plane may display read-only ERPNext summaries and links, but it must not silently create a competing employee, shift, or payroll database.

### Non-goals

- Porting the Django application or adopting Django only for feature parity.
- A local payroll or shift-calculation engine.
- Automatic enrollment from attendance captures.
- Silent threshold tuning in production.
- Continuous browser video streaming unless a measured operational need justifies it.
- Supporting horizontal multi-writer deployment on SQLite.
- A standalone non-ERPNext HR mode without a separate approved RFC and a current customer requirement.

## 3. Design principles

1. **Record evidence before applying policy.** Preserve event metadata and recognition decisions independently from the resulting check-in.
2. **Prefer rejection to false identity.** No gallery, model, PAD, or transport failure may become a positive match.
3. **Idempotency must cross the ERPNext boundary.** Local deduplication alone cannot resolve a timeout after ERPNext committed a request.
4. **Separate source from policy.** Production capture transport and camera identity are explicit; IN, OUT, or presence behavior is a separate policy, never a filename guess.
5. **One source of HR truth.** Synchronize or read from ERPNext; do not fork its domain model.
6. **Biometric data is controlled and attributable.** Source/audit media is temporary; long-lived reference templates have explicit lifecycle, access, revocation, and retention controls.
7. **Every recovery action is auditable.** Retry, dismissal, enrollment, configuration, and deletion require an actor and reason.
8. **Ship evidence, not claims.** Production readiness requires measured biometric, failure-recovery, security, and real-camera results.
9. **Start single-node and explicit.** Preserve the current simple deployment until a real scale requirement warrants a different database or broker.

## 4. Current baseline

The repository already provides important foundations that must be preserved rather than rebuilt:

- Bearer-authenticated central embedding export and a bounded synchronization client in `secure_sync.py`. The legacy `face_attendance.py sync` command still reaches a duplicate, less-bounded sync implementation and must be unified.
- Configurable schema, vector, dimension, branch, model, and optional model-version validation. Model-version matching is disabled by default, and branch/model checks are not yet mandatory in every production configuration.
- Atomic gallery file activation plus in-memory fallback to the last successfully loaded gallery. The repository does not yet retain versioned gallery files for operator rollback.
- Multiple embeddings per employee, cosine threshold, second-best margin, face-size, detection-score, per-image duplicate, and cooldown controls.
- FTP staging, file-size/pixel/filesystem-mtime age checks, quarantine, per-camera content-hash replay protection, and persistent SQLite runtime state. Filesystem mtime is receipt evidence, not yet an authenticated camera capture timestamp.
- Admin authentication, scrypt password storage, persistent session secret, CSRF protection, login throttling, security headers, and admin audit rows.
- Configurable cleanup for accepted/rejected/temp crops and optional source deletion/private ERPNext attachments. Quarantine, watcher logs, enrollment photos, reports, audit rows, backups, and remote attachments do not yet share a complete lifecycle policy.
- Model manifest verifier, PAD adapter, production-readiness gate, systemd units, reverse-proxy examples, and FTPS or isolated-network controls. Several integrity and insecure-transport overrides remain configurable and need a stricter production profile.
- Unit coverage for galleries, synchronization, runtime state, web security, PAD behavior, readiness, watcher finalization, and an installer-template assertion. There is no clean-host installer test, and `test_match_employee.py` is a script rather than a discoverable unit test.

The next stages should extend these controls instead of creating parallel implementations.

## 5. Confirmed gaps to close

### Event and delivery reliability

- `camera_events` stores a coarse final status but not the individual face decisions, scores, gallery/model version, PAD evidence, ERPNext document, or delivery attempts.
- A crash can leave an event in `processing` indefinitely; there is no lease, recovery classification, or operator resolution workflow.
- ERPNext creation is synchronous. There is no durable outbound queue, retry schedule, dead-letter state, or reconciliation job.
- A timeout after ERPNext accepts a check-in is ambiguous. Retrying safely requires a stable per-check-in delivery ID enforced on the ERPNext side.
- Check-in creation and crop attachment are coupled. Attachment failure after check-in creation can make a successful check-in appear failed.
- Check-in time currently comes from the attendance server clock at delivery time rather than an explicitly validated event-time policy.
- Cooldown state is a separate JSON file and lock rather than part of the transactional event policy.

### Camera and policy behavior

- Direction is inferred from `in`/`out` folder names. There is no first-class camera registry with branch, mode, expected source, health, or retirement state.
- Entry/exit behavior exists, but there is no durable, scheduled presence-session policy for sites that use presence cameras.
- Cross-camera overlap, repeated appearances, direction conflicts, and day-boundary behavior are not represented as explicit tested policies.

### Operator and enrollment experience

- The current web UI shows readiness, gallery status, a basic employee list, raw photo upload, rebuild, and sync controls.
- It does not show camera health, event decisions, rejected reasons, queue/dead-letter state, delivery attempts, ERPNext reconciliation, or per-event audit detail.
- Enrollment lacks guided multi-view capture, quality feedback, ERPNext employee lookup, per-employee readiness, gallery version history, revocation, and rollback.
- There is one administrative identity rather than least-privilege operator, enrollment, and audit roles.

### Assurance and operations

- Existing tests are mostly unit tests; there is no complete FTP-to-ERPNext integration suite or real camera/model/ERPNext acceptance record.
- There is no representative biometric calibration dataset, versioned threshold report, or drift process.
- The PAD adapter still requires a selected, licensed, measured provider for production.
- Production processing is split across `watch_service.py` and legacy `face_attendance.py watch` / `watch-folder` routes. The PowerShell launchers still select legacy routes that bypass the production event ledger, PAD, and readiness gate.
- Startup can automatically deserialize a legacy local pickle; synchronization has a duplicate legacy implementation; strict branch/model-version/freshness/integrity rules are not uniformly required; and the verified model directory is not explicitly passed to the runtime model loader.
- Content hashes prevent exact replay but not re-encoded replay, and camera identity is not yet cryptographically bound to the device.
- Operational health lacks structured metrics, camera freshness, queue depth, delivery latency, rejection-rate alerts, and backup/restore evidence.
- Dependencies use bounded ranges but there is no reproducible lock, artifact inventory, or documented update procedure.

## 6. Target data model

Introduce versioned migrations for `runtime_state.sqlite3`. Do not replace an existing database in place without backup and rollback validation.

### `cameras`

- Stable `camera_id` primary key.
- Branch/site identifier.
- Source type: `holowits_ftp`, `rtsp`, or `fixture`.
- Attendance policy: `IN`, `OUT`, or optional `presence`.
- Expected FTP user/staging route or other source configuration.
- Enabled/retired state.
- Last upload, last accepted event, and health status.
- Optional validated timestamp behavior and per-camera notes.

Each source adapter emits one normalized `CapturedEvent` envelope containing capture ID, camera ID, receipt time, optional source time and provenance, content hash, source reference, and media metadata. RTSP is non-production until it enters the same ledger, PAD, policy, and delivery path as FTP.

### `camera_events`

Extend the current record with:

- Immutable `received_at`, parsed `source_at` plus timezone/trust provenance, and immutable `effective_at`.
- Separate capture ID and content hash rather than treating the current camera/log-type/hash key as every identity.
- Current lifecycle state and version.
- Processing lease owner/expiry and recovery classification.
- Gallery, recognition model, preprocessing, PAD provider, and policy versions.
- Final disposition and operator resolution fields.
- Retention state for source image and audit crops.

Persist a normalized receipt even when the upload is stale, future-dated, oversized, unreadable, or rejected before recognition. Add append-only `event_transitions` and `operator_actions` rows so explanations do not depend only on mutable status columns.

Keep a minimal idempotency tombstone longer than detailed event/media retention. Pruning the normal event history must not make an old capture eligible for replay.

### `recognition_decisions`

One row per detected face:

- Immutable recognition-decision ID, capture event ID, and face index.
- Bounding-box and quality metadata.
- Best employee, best score, runner-up score, and margin.
- PAD result, score, model, evidence ID, and reason.
- Accepted/rejected result with stable reason code.
- Selected policy and resulting candidate log type.

Do not store complete embeddings in the event ledger.

### `delivery_jobs` and `delivery_attempts`

- Immutable delivery ID for one accepted recognition decision, plus the capture event ID for traceability.
- Target adapter and intended Employee Checkin payload.
- States: `pending`, `delivering`, `delivered`, `retryable`, `uncertain`, `dead_letter`, or `cancelled`.
- Attempt count, next attempt time, bounded error, ERPNext document name, and reconciliation result.
- Separate optional attachment delivery state so a crop failure cannot invalidate a created check-in.

### `presence_sessions`

Create only if a verified camera workflow requires presence mode:

- Employee, camera, start event, last-seen time, scheduled close time, and closed event.
- Persistent state that survives restart.
- A scheduler closes expired sessions; reappearance is not required to manufacture an OUT.

### `gallery_releases` and `calibration_profiles`

- Gallery version, branch, model/preprocessing version, employee/template counts, publisher, and activation/rollback audit.
- Calibration dataset reference, threshold, margin, PAD threshold, metrics, approver, and validity window.
- Reports and references only; no evaluation images in Git.
- Rollback publishes a new attributable release referencing previously approved content; it never silently reuses an old version identifier.

## 7. External prerequisites and implementation touchpoints

### External prerequisites

- A supported ERPNext/HRMS version matrix, staging site, least-privilege service account, and documented Employee/Employee Checkin permissions.
- An ERPNext-side atomic idempotency extension: either a unique `face_attendance_delivery_id` per Employee Checkin with duplicate-conflict lookup or a small whitelisted get-or-create endpoint keyed by that delivery ID. Retain `face_attendance_event_id` as non-unique capture trace metadata; client-side lookup alone is not exactly-once delivery.
- Real HOLOWITS filenames and upload samples, documented NTP/timezone behavior, and an approved capture-time trust policy.
- A selected, licensed, deployment-evaluated recognition model and PAD provider.
- Named privacy/retention and operational owners.
- SQLite backup, disk-capacity, filesystem, and restore requirements for the supported single-node topology.

Release A requires no Redis, Celery, PostgreSQL, or message broker. Introduce one only after a measured throughput or horizontal multi-writer requirement and a separate architecture decision.

### Planned code touchpoints

| Path/module | Planned responsibility |
| --- | --- |
| `runtime_state.py` | Versioned migrations and stores for receipts, decisions, transitions, policy state, delivery outbox/attempts, audit actions, and idempotency tombstones. Split event storage from login/audit helpers if the module becomes unwieldy. |
| `watch_service.py` | Normalize and persist receipt before rejection/policy; manage leases; turn structured recognition results into decisions and delivery jobs transactionally. |
| `face_attendance.py` | Extract pure recognition/evidence functions; remove synchronous ERPNext side effects from `process_image`; delegate or deprecate legacy production watcher commands. |
| `camera_sources.py` and `holowits.py` | Define `CapturedEvent` and source adapters; parse/validate HOLOWITS timestamps and track metadata without treating them as employee identity. |
| `erpnext_client.py` | Least-privilege Employee lookup plus atomic idempotent Employee Checkin create/get/reconcile for REST and local-bench transports. |
| `delivery_service.py` | Leased delivery worker, retry classification/backoff, uncertain/dead-letter handling, reconciliation, and separate crop-attachment jobs. |
| `employee_directory.py` | Active/branch eligibility snapshot, freshness/fail-closed policy, and automatic deactivation handling. |
| `enrollment_service.py` | Enrollment sessions, quality/diversity feedback, revocation, release publication, and rollback orchestration. |
| `embedding_gallery.py` | Eligibility and release metadata, compatibility rules, release activation, and attributable rollback. |
| `web_admin.py`, `templates/`, `static/` | Role-aware operations/enrollment routes and maintainable UI assets; keep business logic in services. |
| `config.example.json`, `production_readiness.py` | Validate camera sources/policies, directory freshness, ERP idempotency dependency, delivery capacity, canonical watcher, and new production blockers. |
| `deploy/systemd/`, PowerShell launchers | Supervise canonical watcher/delivery/directory jobs and eliminate hardened-path bypasses. |
| `test_*.py`, integration fixtures, `docs/` | Migration, policy, delivery, UI, security, calibration, field-acceptance, upgrade, rollback, and runbook evidence. |

## 8. Delivery phases

### Phase 0 — Decide and measure the real deployment

**Purpose:** Prevent an implementation that solves the wrong camera or HR workflow.

- [ ] `P0-01` Document every deployed/planned camera: model, branch, direction, transport, filename/timestamp behavior, event frequency, and overlapping field of view.
- [ ] `P0-02` Decide whether each site uses directional door events or true presence sessions. Do not support presence mode speculatively.
- [ ] `P0-03` Document the ERPNext Employee Checkin API contract, custom fields, permissions, rate limits, outage behavior, and staging site.
- [ ] `P0-04` Choose an atomic cross-system idempotency design: a server-enforced unique `face_attendance_delivery_id` per Employee Checkin with duplicate-conflict lookup, or a whitelisted get-or-create endpoint keyed by that delivery ID. Keep `face_attendance_event_id` as non-unique capture trace metadata; lookup-before-create alone is insufficient.
- [ ] `P0-05` Define event time: receipt time by default; accept a camera/source timestamp only after format, timezone, age, and authenticity checks.
- [ ] `P0-06` Agree on image, crop, event, audit, and failed-delivery retention periods with the data owner.
- [ ] `P0-07` Identify operator roles and which actions require a reason or second approval.
- [ ] `P0-08` Select the production PAD provider and record licensing, hosting, privacy, latency, and outage requirements.
- [ ] `P0-09` Capture baseline metrics from a controlled deployment: upload rate, recognition latency, acceptance/rejection counts, ERPNext latency, failures, and current false-match/false-reject samples.
- [ ] `P0-10` Define the supported Linux and native Windows deployment matrix, including the one production watcher entry point, installation, service supervision, upgrades, and validation required on each OS.
- [ ] `P0-11` Record the supported ERPNext/HRMS version matrix and a least-privilege active-Employee read contract. Define branch eligibility, snapshot freshness/fail-closed behavior, and removal of deactivated employees from the next gallery.
- [ ] `P0-12` Complete the biometric data inventory/DPIA: purpose and legal basis, employee notice, non-biometric alternative/accommodation, human review/appeal/correction, offboarding, incident response, and backup expiry.
- [ ] `P0-13` Pre-register numeric recognition and PAD acceptance targets, minimum trial counts, confidence bounds, locked calibration/test split, per-camera/condition/subgroup reporting, and maximum operator/recovery times.
- [ ] `P0-14` Review central, PAD, ERPNext, and proxy vendors for data processing terms, residency, subprocessors, retention, breach duties, and face-crop transfer.
- [ ] `P0-15` Confirm the initial topology: one branch/gallery and one active writer per attendance node, one trusted enrollment publisher, no implied fleet-wide UI.
- [ ] `P0-16` Threat-model camera impersonation/replay, gallery rollback/exfiltration, enrollment poisoning, admin compromise, PAD bypass, ERP credential abuse, and biometric deletion failure.

**Exit gate**

- Camera/source/policy matrix, ERPNext version/eligibility/idempotency contracts, topology, retention/DPIA decisions, PAD choice, threat model, and numeric acceptance targets are approved.

### Phase 0A — Close existing live-path blockers

**Purpose:** Do not carry known bypasses or unsafe compatibility behavior into Release A. This gate applies before any new or existing live production deployment.

- [ ] `H0-01` Remove automatic startup `pickle.loads()` migration. Replace it with an explicit offline, one-shot converter; verify provenance, back up first, and quarantine or securely delete the pickle after the approved rollback window.
- [ ] `H0-02` Delete/unify the duplicate synchronization paths so every CLI, service, and UI route uses the bounded client. Validate the final redirect URL as well as the initial URL.
- [ ] `H0-03` Add authenticated anti-rollback gallery releases: validate `generated_at`, require monotonic attributable versions, bind conditional/status state to source+branch+model, and define publisher authenticity such as a signed manifest.
- [ ] `H0-04` Enforce a strict production profile: nonblank branch and model version; required branch/model/version match; nonempty and nonstale gallery; complete model manifest and hashing; single-face PAD or per-face PAD binding; authenticated TLS service endpoints; no insecure/unauthenticated overrides.
- [ ] `H0-05` Make `/readyz`, production startup, sync, and watcher validate the same effective branch/model/version/gallery/PAD policy. Validate the actual admin password-hash structure rather than only its prefix.
- [ ] `H0-06` Ensure the manifest-verified model directory is the directory InsightFace actually loads, then prove a changed/unlisted runtime model blocks startup.
- [x] `H0-07` Constrain and encode employee IDs and all gallery string/numeric fields before filesystem, URL, log, or ERP use. Add path-traversal, length, character, dimension, and count tests.
- [x] `H0-08` Bind PAD evidence to each face that can create a check-in. In production require exactly one face unless every recognized face receives its own PAD result; pin/allowlist PAD provider and model versions.
- [x] `H0-09` Bind each upload credential and source route to one camera, direction/policy, branch, and allowed network. Use unique credentials and document stronger device authentication when supported; content hashing alone does not stop re-encoded replay.
- [x] `H0-10` Route every supported Linux and Windows launcher through `watch_service.py` or refuse live operation. Legacy RTSP and direct `watch-folder` modes remain dry-run/non-production until they enter the same ledger/PAD/readiness path.
- [x] `H0-11` Add scoped/rotatable gallery credentials, export audit/rate limits, trusted-proxy-aware login throttling, secret-manager/systemd credential support, and a roadmap/adapter point for organizational SSO/MFA.
- [ ] `H0-12` Default source/crop/ERP attachment retention to the minimum justified by the DPIA; enumerate and test cleanup for quarantine, logs, enrollment media, reports, audit state, PAD copies, and backups.

**Acceptance**

- No documented or installed live path bypasses replay state, PAD, gallery validation, readiness, or event audit.
- A legacy pickle, old-but-redownloaded gallery, path-shaped employee ID, changed runtime model, unapproved PAD version, or insecure endpoint fails closed in production.
- Existing unit tests plus targeted security regressions pass before Phase 1 work begins.

### Phase 1 — Versioned event ledger and crash recovery

**Purpose:** Make every camera event durable, explainable, and recoverable before adding features.

- [x] `P1-01` Add an explicit schema-version table and transactional forward migrations for `runtime_state.sqlite3`.
- [x] `P1-02` Add backup-before-migrate, migration verification, and documented rollback/restore commands.
- [x] `P1-03` Expand `camera_events`; add `recognition_decisions`, append-only `event_transitions`, and append-only `operator_actions` using stable reason/status enums.
- [x] `P1-04` Persist a normalized receipt before time/size/decode rejection, then persist model, gallery, PAD, scores, margin, policy, immutable event times, and retention outcome for every decision.
- [x] `P1-05` Replace permanent `processing` claims with leases and explicit startup recovery: safely retry pre-delivery work; classify delivery ambiguity as `uncertain`.
- [x] `P1-06` Move cooldown and event-policy state into the transactional store. Scope cooldown deliberately by employee, direction, branch, and policy; eliminate crash-stranded lock files.
- [x] `P1-07` Add read-only CLI commands to list, inspect, and explain events without exposing secrets or biometric vectors.
- [x] `P1-08` Add audited event reprocess, quarantine-resolution, and dismissal commands with required reasons. Delivery retry/cancel begins only after Phase 2 creates delivery jobs.
- [x] `P1-09` Keep old event rows readable through the retention window and test migration from a real copy of the current schema.
- [x] `P1-10` Retain minimal content/capture idempotency tombstones after detailed event/media expiry so normal pruning cannot make an old upload eligible again.
- [x] `P1-11` Define and test distinct capture ID, content hash, recognition-decision ID, and delivery ID semantics.

**Acceptance**

- Killing the watcher at each stage produces either a safe retry or an explicit `uncertain` event, never silent loss.
- Every accepted or rejected face has a stable reason and the exact model/gallery/PAD/policy versions used.
- Migration, restore, and rollback are tested from the previous released database format.
- A repeat IN inside the configured anti-bounce window is deterministically suppressed, while a legitimate IN-to-OUT transition inside that interval remains eligible; a process kill cannot strand the policy lock.

Phase 1 is not independently enabled for live delivery. Deploy Phases 1 and 2 together in shadow mode first because the existing synchronous ERPNext call remains ambiguous until the durable outbox is active.

### Phase 2 — Idempotent ERPNext delivery and reconciliation

**Purpose:** Decouple recognition from network delivery and make check-ins recoverable.

- [x] `P2-01` Introduce an ERPNext adapter interface; keep API and local-bench transports explicit and independently tested.
- [x] `P2-02` Create `delivery_jobs` in the same transaction that accepts a recognition decision.
- [x] `P2-03` Implement a single-node delivery worker with leases, bounded exponential backoff, jitter, and retry budgets.
- [x] `P2-04` Enforce an atomic ERPNext idempotency contract for both REST and local-bench adapters: a unique `face_attendance_delivery_id` per Employee Checkin with duplicate-conflict lookup or a whitelisted get-or-create endpoint keyed by that delivery ID. Keep `face_attendance_event_id` as non-unique capture trace metadata so multiple accepted faces in one capture can create independent check-ins. Client lookup-before-create alone is race-prone. Retry an ambiguous post-submit outcome only when the job is immutably bound to the verified server contract; otherwise preserve it as `uncertain`.
- [x] `P2-05` Separate Employee Checkin creation from private crop attachment. A failed attachment becomes its own retryable job, and any required private crop is protected from retention cleanup until that job reaches a terminal state.
- [ ] `P2-06` Send the validated effective event time, camera ID, branch, immutable decision ID and version, unique delivery ID, and non-unique capture event ID to ERPNext where the agreed schema permits.
- [ ] `P2-07` Classify errors as retryable, permanent, authentication, validation, conflict, rate-limit, or uncertain; never retry permanent errors forever.
- [ ] `P2-08` Add scheduled and manual reconciliation against ERPNext, including missing, duplicate, mismatched, externally changed, and externally deleted records. Treat ERP-owned edits as visible owned exceptions; never silently overwrite or recreate them.
- [ ] `P2-09` Add a dead-letter workflow with actor, reason, retry/cancel controls, and complete audit history. Cancel applies only to undelivered local jobs; delivered Employee Checkin correction/deletion happens in ERPNext and is annotated locally.
- [ ] `P2-10` Keep recognition operating during the P0-defined outage envelope and enforce queue capacity/disk-pressure safeguards.
- [ ] `P2-11` Enforce a fresh active/branch employee-eligibility snapshot before accepting or delivering a match. Fail closed under the P0 policy and remove deactivated employees from the next gallery release.

**Acceptance**

- An ERPNext outage sized from measured peak rate × the approved safety factor queues and later delivers every eligible accepted decision with zero loss, zero duplicate ERP IDs, bounded recovery time, and verified disk headroom.
- A timeout after remote commit does not create a duplicate; without the atomic ERPNext dependency the product does not claim exactly-once delivery.
- A multi-face capture can create one independently idempotent Employee Checkin for each eligible accepted decision; those check-ins have distinct delivery IDs and may share the same non-unique capture event ID.
- Employee Checkin creation persists the returned ERP document name before attachment begins. Attachment failure never changes a delivered check-in to failed, and delivery always uses immutable `effective_at` rather than retry time.
- Reconciliation reports zero unexplained differences for the controlled test window and surfaces ERP-owned changes without overwriting them.

### Phase 3 — Camera registry and policy engine

**Purpose:** Replace implicit folder behavior with explicit, testable site policy.

- [ ] `P3-01` Add validated camera registry configuration with stable ID, branch, `source_type`, separate `IN`/`OUT`/`presence` policy, source route, enabled state, and expected upload freshness.
- [ ] `P3-02` Refuse production readiness when an upload route is unmapped, maps to multiple cameras, or falls back to guessed direction.
- [ ] `P3-03` Preserve raw event evidence before entry/exit/presence policy and cooldown decisions.
- [ ] `P3-04` Define entry/exit repeat-appearance, overlap, cross-camera lockout, opposite-direction, day-boundary, and clock-skew behavior as policy tests.
- [ ] `P3-05` If Phase 0 proves the need, add durable presence sessions with scheduled timeout closure and restart/multi-process tests.
- [ ] `P3-06` Track camera last upload, last valid image, last accepted event, consecutive failures, and clock drift.
- [ ] `P3-07` Provide a safe camera test command that processes a fixture/dry-run upload without creating a live check-in.
- [ ] `P3-08` Provide a migration path from `camera_ids`, `folder_log_types`, and current FTP-user configuration, with warnings before removal.
- [ ] `P3-09` Implement the common `CapturedEvent` adapter contract and migrate HOLOWITS FTP first. Keep RTSP non-production until it produces the same envelope and passes through the same ledger, PAD, policy, and delivery services.
- [ ] `P3-10` Complete native Linux and Windows start, stop, restart, crash recovery, upgrade, and rollback tests for the canonical services established by `H0-10`.

**Acceptance**

- Unknown cameras cannot create production check-ins.
- Entry and exit cameras never depend on their names to determine direction.
- Restart, midnight, overlapping views, rapid reappearance, simultaneous cameras, and clock-skew cases have deterministic results.
- Every supported launcher reaches the same hardened processing pipeline; no documented production route bypasses the event ledger, PAD, or readiness gate.

### Phase 4 — Authenticated operations control plane

**Purpose:** Give one operator visibility without requiring terminal access or a parallel HR application.

- [ ] `P4-01` Replace the single inline page with maintainable templates/static assets while keeping the Flask service and current security middleware.
- [ ] `P4-02` Add least-privilege roles: administrator, enrollment operator, attendance operator, and read-only auditor.
- [ ] `P4-03` Add camera-health cards, gallery/PAD/model readiness, queue depth, dead-letter count, delivery latency, and service freshness.
- [ ] `P4-04` Add searchable event list filters for date, employee, branch, camera, direction, outcome, rejection reason, and delivery state.
- [ ] `P4-05` Add event detail with scores, margin, PAD result, gallery/model versions, retention-safe crop preview, delivery attempts, ERPNext link, and audit history.
- [ ] `P4-06` Add audited retry, reconcile, undelivered-job cancel, quarantine review, and retention-safe media delete actions. Delivered Employee Checkin correction/deletion remains an ERPNext action with a local link and annotation.
- [ ] `P4-07` Add recent accepted/rejected event updates using bounded polling first; use SSE only if polling proves inadequate. Do not stream continuous video by default.
- [ ] `P4-08` Display read-only ERPNext daily summaries and deep links rather than computing payroll or canonical hours locally.
- [ ] `P4-09` Add an operator recognition guide and context-specific remediation for stale gallery, PAD outage, unknown camera, bad time, duplicate, and ERPNext failure.
- [ ] `P4-10` Add pagination, response-size limits, safe CSV export, accessibility checks, and tests for authorization on every route/action.

**Acceptance**

- An operator can find and explain a rejected or missing check-in without shell access.
- Every mutation is CSRF-protected, role-authorized, rate-limited where appropriate, and audited.
- The dashboard remains usable with large event history through server-side pagination.

### Phase 5 — ERPNext-backed guided enrollment

**Purpose:** Match the useful enrollment experience of a standalone HR UI without duplicating HR ownership.

- [ ] `P5-01` Add a least-privilege ERPNext employee-directory adapter with branch and active-status filtering.
- [ ] `P5-02` Select employees from ERPNext; prohibit free-form IDs in production unless explicitly authorized.
- [ ] `P5-03` Build a multi-step enrollment session with limits, expiry, actor, employee, consent/authority record, and cleanup.
- [ ] `P5-04` Give immediate per-image feedback for face count, size, detector confidence, blur, lighting/exposure, pose diversity, and duplicate frames.
- [ ] `P5-05` Require a configurable number of diverse valid images and show why an employee is not gallery-ready.
- [ ] `P5-06` Add optional guided browser capture only for enrollment, over HTTPS, after verifying it solves the deployed workflow.
- [ ] `P5-07` Add employee template review, re-enrollment, revocation, and removal with role checks and audit; require a second approval for production publish/revoke when the P0 risk decision requires it.
- [ ] `P5-08` Add enrollment-poisoning and cross-employee collision checks before publish. Build immutable gallery releases with validation summary, publisher, version, activation, and attributable rollback as a new release referencing old content.
- [ ] `P5-09` Ensure attendance nodes never receive enrollment photos and central enrollment photos follow an explicit retention policy.
- [ ] `P5-10` Prevent model/preprocessing changes from activating until all affected galleries are rebuilt and compatibility checks pass.

**Acceptance**

- A trained enrollment operator can complete and verify an employee without editing files or typing an unverified ID.
- Poor, duplicate, multi-face, or insufficiently diverse samples are rejected with actionable feedback.
- Gallery activation and attributable rollback are atomic and tested on attendance nodes; an old version identifier is never silently reused.

### Phase 6 — Biometric and PAD evaluation

**Purpose:** Replace guessed thresholds and adapter-only assurance with measured deployment evidence.

- [ ] `P6-01` Define consent, access, retention, and deletion rules for the evaluation dataset; keep images and embeddings out of Git.
- [ ] `P6-02` Pre-register ground truth, minimum trial counts, a calibration/test split, relevant conditions/subgroups, and 95% confidence reporting. Lock the test set before threshold selection.
- [ ] `P6-03` Collect representative known-employee and non-employee trials across actual cameras, distances, lighting, glasses, headwear, pose, similar-looking employees, and varying templates per employee.
- [ ] `P6-04` Add a reproducible offline evaluator for detector failure, ambiguity/quality rejection, open-set FPIR/FNIR, score/margin distributions, latency, per-camera/condition/subgroup results, worst-group gaps, and max-over-templates bias.
- [ ] `P6-05` Choose threshold and margin on the calibration split only; publish a versioned report and approval reference, then evaluate once against the locked test split with numeric pass/fail targets.
- [ ] `P6-06` Record recognition and PAD provider/model versions on every event and alert on unapproved version drift.
- [ ] `P6-07` Add shadow/dry-run comparison for a new calibration or model before activation; never auto-learn from attendance events.
- [ ] `P6-08` Define scheduled re-evaluation and drift triggers based on camera/model/gallery changes and rejection trends.
- [ ] `P6-09` Evaluate the selected PAD provider using ISO/IEC 30107-style APCER/BPCER reporting and combined-system attack success for each relevant presentation attack instrument: prints, replay displays, and deployment-relevant masks.
- [ ] `P6-10` Include PAD latency, timeout, malformed response, wrong provider/model version, and complete outage in the locked evaluation.

**Acceptance**

- Production thresholds point to a reproducible, approved report with confidence bounds and meet the P0 numeric FPIR/FNIR/latency/worst-group targets without tuning on the locked test set.
- Known attack classes meet numeric APCER/BPCER and combined-system targets; provider outages satisfy the documented fail-closed policy.
- A model, preprocessing, or calibration change cannot silently mix incompatible galleries.

### Phase 7 — Security, privacy, observability, and recovery

**Purpose:** Turn existing hardening controls into an operated, monitored system.

- [ ] `P7-01` Maintain the P0 threat model as camera, gallery, PAD, ERPNext, enrollment, admin, and retention behavior changes; link mitigations and residual-risk owners.
- [ ] `P7-02` Complete FTPS deployment or document and continuously verify camera VLAN/VPN isolation and firewall scope.
- [ ] `P7-03` Add secret rotation procedures for camera, ERPNext, gallery, PAD, and web-session credentials without logging values.
- [ ] `P7-04` Enforce filesystem ownership/modes and appropriate encryption at rest; encrypt protected backups, assign key ownership/rotation/recovery, and validate restore onto a clean host.
- [ ] `P7-05` Add structured, redacted logs with event IDs and stable reason codes; never log tokens, complete embeddings, or unnecessary PII.
- [ ] `P7-06` Add per-process health/readiness/heartbeat and synthetic probes for FTP, watcher, sync, delivery, web, and required PAD/ERPNext dependencies. `/healthz` alone proves only that Flask is alive.
- [ ] `P7-07` Export bounded metrics for camera age, event counts, outcome/reason rates, processing and delivery latency, queue/dead-letter depth, gallery age, PAD failures, disk use, cleanup/audit failures, and process heartbeat. Define PII/cardinality rules.
- [ ] `P7-08` Add alerts with owners/runbooks for process failure, camera silence, rejection spikes, stale/rollback gallery, PAD outage, ERPNext backlog, disk pressure, readiness failure, cleanup/audit failure, and reconciliation mismatch.
- [ ] `P7-09` Exercise event database, gallery, configuration, audit, and idempotency-tombstone backup/restore; document RPO/RTO and disaster recovery ownership.
- [ ] `P7-10` Produce an exact hashed dependency lock and artifact/SBOM report; document reviewed update and rollback cadence.
- [ ] `P7-11` Implement and test retention/deletion for enrollment images/templates, unknown visitor crops, accepted crops, ERP attachments, sources/quarantine, decisions/scores, audit/logs, import reports, PAD-provider copies, and backups. Never prune unresolved, uncertain, or dead-letter evidence solely because it reached the normal age cutoff.
- [ ] `P7-12` Operationalize employee notice, non-biometric accommodation, appeal/correction, offboarding/revocation propagation, backup expiry, and breach/incident response from the DPIA.
- [ ] `P7-13` Define SLOs/error budgets, log rotation, bounded audit retention, capacity thresholds, and alert tests; record when a deployment is outside its supported envelope.

**Acceptance**

- A clean host can be restored within the agreed RTO without duplicating a delivered event.
- Every production blocker and alert has an owner, runbook, and tested response.
- Secret, dependency, and retention changes are repeatable and auditable.

### Phase 8 — Integration, field validation, and controlled release

**Purpose:** Prove the complete system under realistic failures before live check-ins.

- [ ] `P8-01` Add an isolated integration harness: staged FTP upload -> watcher -> deterministic recognition/PAD doubles -> delivery worker -> fake ERPNext.
- [ ] `P8-02` Test duplicate/re-encoded content, concurrent same-name FTP uploads, partial/malformed uploads, multi-face uploads with multiple accepted per-face decisions, stale/future events, NTP loss, gallery/manual-sync concurrency, PAD failure, ERPNext timeout-after-commit, attachment failure, event-pruning replay, and deterministic process-kill/disk-full/SQLite-busy points.
- [ ] `P8-03` Test two processes against the supported single-node topology, corrupted-database recovery, and queue capacity. If horizontal multi-writer deployment is required, select PostgreSQL/a broker through a separate architecture decision and rerun the suite.
- [ ] `P8-04` Add web/security end-to-end tests for login, roles, session expiry, CSRF, trusted-proxy throttling, auth matrix, upload/parser/path traversal, redirect/TLS policy, event review, enrollment, gallery publish/rollback, retry, and reconciliation.
- [ ] `P8-05` Run a real staging trial with each camera model, the approved recognition/PAD models, protected transport, and a non-production ERPNext site.
- [ ] `P8-06` Run controlled employee, visitor, similar-face, printed-photo, screen-replay, simultaneous-camera, restart, and ERPNext-outage scenarios.
- [ ] `P8-07` Establish performance budgets for ingest rate, recognition latency, delivery latency, queue recovery, web response, CPU, memory, and disk.
- [ ] `P8-08` Inventory deployed Linux/Windows/Frigate/RTSP workers, configs, photos, pickles, galleries, cooldown and SQLite state. Add versioned config/database/gallery migration preflight, encrypted backup/restore, and rollback compatibility for binaries, schemas, configs, and service drop-ins.
- [ ] `P8-09` Rebuild galleries with the pinned model/preprocessing, shadow-compare one camera/branch, stop every parallel legacy worker, rotate old credentials, verify counts/checksums/reconciliation, and securely delete attendance-node photos/pickles/reports after the rollback window.
- [ ] `P8-10` Deploy one canary branch in dry-run/shadow mode, compare against manual truth, then enable live delivery with enhanced monitoring and an emergency watcher-stop procedure.
- [ ] `P8-11` On clean native Linux and Windows hosts test install, service supervision, actual FTPS or verified isolation, HTTPS proxy, firewall, upgrade, rollback, capacity, and log rotation for every supported Python/OS combination.
- [ ] `P8-12` Exercise real-model fixtures, both ERPNext API and local-bench adapters, and the complete gallery exporter -> bounded sync -> hot-reload path.
- [ ] `P8-13` Run retention/deletion tests and corrupted/clean-host restore drills, proving offboarded templates and expired media disappear from live state and backups under the approved policy.
- [ ] `P8-14` Harden packaging/release: root-owned immutable code, separate least-privilege service identities and writable paths, stronger systemd sandbox/resource/start limits/watchdog, and atomic versioned releases.
- [ ] `P8-15` Strengthen CI/release gates with the hashed lock, action commit pinning, lint/type/coverage thresholds, SAST/secret/dependency/license scans, SBOM, signed/provenanced artifact/tag, changelog/schema compatibility, branch protection, and staging-to-canary promotion.
- [ ] `P8-16` Record a signed release checklist with numeric test/calibration/PAD/SLO evidence, readiness output, backup restore, security/privacy reviews, cutover/rollback result, and accountable owner approval.

**Release gate**

- Unit, integration, browser, migration, concurrency, restart, outage, and real-camera acceptance checks pass.
- Calibration and PAD evidence meet the agreed targets.
- ERPNext reconciliation has no unexplained differences.
- Rollback, restore, retention/deletion, alerts, cutover, and operator runbooks have been exercised on every supported platform.
- The release artifact is reproducible, scanned, signed/provenanced, and linked to compatible database/config/gallery schema versions.

## 9. Capability target relative to the standalone Django app

| Capability | Target in this repository |
| --- | --- |
| Employee directory | Read active employees from ERPNext; do not create a competing employee table. |
| Guided enrollment | Central, authenticated, quality-scored, multi-view enrollment with gallery release/rollback. |
| Live/dual camera screen | Camera health and recent event decisions; continuous video only if a measured need appears. |
| Attendance log | Searchable durable event/decision/delivery history with ERPNext links and reconciliation. |
| Daily summary | Read-only ERPNext summary; ERPNext remains canonical. |
| Shift management | Keep in ERPNext; show assignment/context only when useful. |
| Entry/exit behavior | Explicit camera modes and tested overlap/reappearance policy. |
| Presence behavior | Optional persistent scheduled sessions, implemented only for verified presence-camera deployments. |
| Recognition guide | Contextual operator guidance based on stable reason codes. |
| Security | Preserve authenticated admin, CSRF, protected sync, readiness, replay controls, PAD, retention, and audit; add roles and operational evidence. |
| Restart/concurrency | Durable ledger, delivery idempotency, explicit recovery, and tested supported topology. |
| Multi-site operation | Initial scope is one branch/gallery per node. Standardize branch-scoped registry, health, calibration, and rollout first; fleet aggregation or multi-gallery control requires a later explicit design. |

Feature parity is not achieved by reproducing Django screens or tables. It is achieved when every useful workflow has a safer implementation or an intentional ERPNext-owned route.

## 10. Dependency order

```text
Phase 0 decisions
  -> Phase 0A live-path hardening
      -> Phase 1 event ledger
          -> Phase 2 delivery/reconciliation
          -> Phase 3 camera policy
          -> Phase 4 operations UI
              -> Phase 5 enrollment
      -> Phase 6 recognition/PAD evaluation

Phases 0A-6
  -> Phase 7 operated assurance/recovery
  -> Phase 8 field validation/release
```

Do not begin UI polish before the event and delivery state model is stable. Do not enable live delivery before ERPNext idempotency, recovery, reconciliation, and field acceptance are proven.

## 11. Suggested release increments

### Release A — Event integrity

Phases 0, 0A, 1, and 2: close current live-path blockers, then deliver the versioned ledger, crash recovery, durable queue, remote idempotency, attachment separation, eligibility gate, and reconciliation CLI. This is the first implementation priority and is shadowed before live delivery.

### Release B — Camera policy and operator visibility

Phases 3-4: explicit registry/modes, camera health, event history/detail, dead-letter workflow, roles, and read-only ERPNext summaries.

### Release C — Enrollment and biometric assurance

Phases 5-6: ERPNext-backed guided enrollment, gallery releases/rollback, measured recognition calibration, and validated PAD.

### Release D — Production evidence

Phases 7-8: metrics/alerts, backup/restore, dependency reproducibility, end-to-end/field tests, canary, and signed release gate.

No release is live-deployable until Phase 0A passes. After that gate, each release must be useful and recoverable by itself; later releases must not be required to recover safely from failures introduced earlier.

## 12. Success measures

Targets must be finalized in Phase 0, but the product should report at least:

- Camera upload freshness and availability by branch.
- Processing success, rejection, quarantine, and stable reason rates.
- Recognition and PAD latency percentiles.
- Accepted score/margin distributions and approved calibration version.
- ERPNext queue depth, delivery latency, retry, uncertain, and dead-letter rates.
- Duplicate prevention and reconciliation mismatch counts.
- Gallery age, version, employee/template counts, and rollback events.
- Retention cleanup failures and protected disk usage.
- Operator time to explain and resolve a failed/missing check-in.
- Backup restore time and recovery-point evidence.

A useful north-star operational target is: **every camera event ends with all face decisions explainable, every accepted decision producing exactly one reconciled ERPNext check-in, or a visible and owned exception.**

## 13. Decisions required before implementation

1. Which current pain is first: missing/duplicate check-ins, ERPNext outages, enrollment quality, camera visibility, or operator reporting?
2. Is any deployed camera truly a presence camera, or are all cameras directional event cameras?
3. Can ERPNext enforce a unique external face-attendance delivery ID per Employee Checkin while retaining the capture event ID as non-unique trace metadata?
4. Which timestamp is authoritative and trustworthy for each camera model?
5. Which PAD provider and model can be licensed and evaluated?
6. What biometric/audit retention policy and operator roles are approved?
7. Is the supported topology one watcher per attendance node, or is horizontal multi-writer operation a current requirement?
8. Is a browser enrollment capture flow needed now, or is controlled photo upload sufficient?

Answer these with deployment evidence, select Release A scope, and then create small implementation issues from the IDs above. Do not implement all phases as one change.
