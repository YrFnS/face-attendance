# ERPNext atomic delivery-ID idempotency

Phase 2 task `P2-04` makes Employee Checkin creation retry-safe across the
attendance-node/ERPNext boundary. Local SQLite deduplication is not sufficient:
a request may commit in ERPNext and lose its response before the edge node can
record success.

The repository includes a small ERPNext/Frappe v15 companion app at:

```text
frappe_apps/face_attendance_idempotency
```

The app installs hidden trace fields on **Employee Checkin**, enforces a database
unique constraint on `custom_face_attendance_delivery_id`, exposes an
authenticated capability probe, and provides one atomic create-or-get method for
both REST and local-bench delivery.

## Contract boundaries

The server contract version is:

```text
face-attendance/erpnext-checkin-idempotency/v1
```

The Employee Checkin payload contract is:

```text
erpnext-employee-checkin-v1
```

The cross-system idempotency key is the immutable per-decision `delivery_id`.
`event_id` remains non-unique capture trace metadata because one camera event can
contain more than one independently accepted face.

The server persists and compares:

```text
delivery ID
employee
IN/OUT direction
effective event time
delivery payload contract
event ID
recognition-decision ID
camera ID
branch
```

Reusing one delivery ID with different immutable data is a permanent conflict.

## Why the operation is atomic

The create method attempts the Employee Checkin insert first. The database unique
constraint decides concurrent races. When another request has already committed
the same delivery ID, the losing request rolls back to a savepoint, locks and
reads the winner, verifies every immutable field, and returns the existing
Employee Checkin.

There is no correctness claim based on a client-side or server-side
lookup-before-create sequence.

## Install on an ERPNext v15 bench

Stop attendance delivery before changing the ERPNext schema. Back up the ERPNext
site according to the site's normal recovery procedure.

From the bench host, install the companion app from this repository checkout:

```bash
cd /home/frappe/frappe-bench

bench get-app \
  /path/to/face-attendance/frappe_apps/face_attendance_idempotency

bench --site <site> install-app face_attendance_idempotency
bench --site <site> migrate
```

For an app already installed, deploy the new revision and run:

```bash
bench --site <site> migrate
```

The install/migrate hook fails when duplicate nonempty delivery IDs already
exist or when the exact single-column unique constraint cannot be verified.
Blank delivery IDs are normalized to SQL `NULL`, allowing ordinary manually
created Employee Checkins to coexist with the unique constraint.

## Service-account permissions

The attendance service account needs only the permissions required for its
approved workflow:

```text
Employee Checkin: create and read
File: create, only when private crop attachment is later enabled
```

Do not use Administrator credentials. The capability and create methods are
whitelisted but not guest-accessible.

## Probe the installed contract

REST:

```bash
curl --fail-with-body \
  -H 'Authorization: token <api-key>:<api-secret>' \
  -H 'Content-Type: application/json' \
  -X POST \
  https://erp.example.com/api/method/face_attendance_idempotency.api.get_contract \
  --data '{}'
```

Local bench:

```bash
bench --site <site> execute \
  face_attendance_idempotency.api.get_contract \
  --kwargs '{}'
```

The result identifies the site, app/version, database type, field contract,
unique constraint, create/probe methods, delivery payload contract, and a
canonical SHA-256 capability fingerprint.

Record the returned site and fingerprint in the attendance-node secret/config
management system. Do not copy a fingerprint from another ERPNext site.

## Attendance-node configuration

Production delivery requires explicit pins:

```json
{
  "erpnext_idempotency_required": true,
  "erpnext_idempotency_contract_version": "face-attendance/erpnext-checkin-idempotency/v1",
  "erpnext_idempotency_create_method": "face_attendance_idempotency.api.create_or_get_employee_checkin",
  "erpnext_idempotency_probe_method": "face_attendance_idempotency.api.get_contract",
  "erpnext_expected_site": "approved-site-name",
  "erpnext_expected_idempotency_fingerprint": "64-lowercase-hex-characters",
  "erpnext_idempotency_probe_cache_seconds": 300
}
```

`production_readiness.py` rejects production delivery without these pins. The
worker also performs a live authenticated probe before leasing a job. Every
leased job is immutably bound to the verified site, contract, method, app
version, and fingerprint before submission starts.

The create request repeats the expected site, contract, and fingerprint. The
ERPNext method recalculates its live capability and rejects destination or schema
drift before insertion.

## Staging acceptance test

Do not enable production worker delivery based only on unit tests. On the exact
supported ERPNext/HRMS v15 staging version:

1. Install and migrate the companion app.
2. Verify the capability endpoint and record its fingerprint.
3. Send one valid create request with delivery ID `D` and confirm document `C`.
4. Send the identical request again and confirm it returns `C` with
   `created=false`.
5. Send `D` with a changed employee, direction, timestamp, event, or decision and
   confirm a conflict.
6. Run two concurrent identical requests and confirm only one Employee Checkin
   exists.
7. Simulate loss of the first response after ERPNext commits, retry `D`, and
   confirm the existing document is returned.
8. Test both REST and local-bench transports.
9. Remove or rename the unique constraint in a disposable staging database and
   confirm the capability probe and production readiness fail closed.
10. Restore the staging database from its approved backup if destructive fault
    injection was used.

Store the commands, ERPNext/HRMS versions, database engine, output, timestamps,
and resulting Employee Checkin names as release evidence.

## Retry behavior after verification

Once a delivery job is bound to the verified contract, these ambiguous transport
outcomes can be replayed with the same delivery ID:

```text
response/read timeout
connection loss after submission
worker restart after submission started
local SQLite failure after ERPNext returned success
expired worker lease after submission
```

A replay can only return the existing matching Employee Checkin or create it
once. Contract drift and payload conflict remain permanent failures.

Without a verified binding, ambiguous post-submit outcomes remain `uncertain` and
are not automatically retried.

## Migration and rollback

Local runtime schema version 9 adds immutable ERPNext contract evidence to each
delivery job. Existing jobs are not silently declared idempotent; they acquire a
binding only while held by the current worker lease and only after a successful
live probe.

The normal runtime migration framework creates and verifies a schema-7 backup
before upgrading a populated database.

Rolling the attendance node back requires the matching P2-03 application
revision and verified schema-7 backup. Removing the ERPNext companion app or
unique constraint while jobs are active is not a supported rollback. Stop the
worker, reconcile all submitted/uncertain jobs, and follow the ERPNext site's
approved schema rollback procedure.

## Remaining external gate

The repository implementation and simulated concurrency/timeout tests do not
replace real-site acceptance. Keep the pull request in draft and production
worker delivery disabled until the staging procedure above passes on the actual
ERPNext/HRMS version and database engine selected for deployment.
