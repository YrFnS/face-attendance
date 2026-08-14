# Runtime state migrations, backup, and restore

`runtime_state.sqlite3` is the durable local store for camera-event replay protection, login throttling, request throttling, and administrative audit records. Beginning with schema version 1, every database records an ordered migration history in `schema_migrations` and mirrors the active version in SQLite `PRAGMA user_version`.

## Invariants

- Migrations are forward-only and numbered consecutively.
- Each migration is identified by version, name, and a SHA-256 checksum of its reviewed SQL.
- The migration history and `PRAGMA user_version` must agree.
- An existing non-empty database is backed up and verified before the first pending migration starts.
- Every migration runs in one `BEGIN IMMEDIATE` transaction. A failed statement rolls back the migration version, its schema changes, and its history row together.
- Startup fails closed when migration history is missing entries, has a changed checksum, claims a future version, or produces an incomplete schema.
- Post-migration verification checks SQLite `quick_check`, required tables, required columns, required indexes, migration checksums, and the active version.
- Backups and metadata sidecars are owner-only where the operating system supports POSIX modes.

The initial migration adopts the previous released schema in place. Existing `camera_events`, login-limit, request-rate-limit, and audit rows remain readable; the migration adds version metadata without recreating those tables.

## Files

The default database path is:

```text
/opt/face-attendance/runtime_state.sqlite3
```

Automatic and manual backups are written to:

```text
/opt/face-attendance/runtime_state_backups/
```

Each backup has a matching metadata sidecar:

```text
runtime_state.<timestamp>.v0-to-v1.pre-migration.<sha>.sqlite3
runtime_state.<timestamp>.v0-to-v1.pre-migration.<sha>.sqlite3.json
```

The sidecar records the source and target schema versions, reason, size, and complete SHA-256 digest. Do not delete the sidecar while retaining the backup. The database file and its sidecar may be copied together to encrypted off-host storage or another recovery host; recorded absolute paths are provenance fields and do not bind verification to the original location.

## Inspect without changing the database

```bash
cd /opt/face-attendance
source .venv/bin/activate
python runtime_state_admin.py status \
  --database /opt/face-attendance/runtime_state.sqlite3
```

`status` reports the installed version, pending migrations, migration history, and integrity result. It does not migrate the database.

To require the current schema:

```bash
python runtime_state_admin.py verify \
  --database /opt/face-attendance/runtime_state.sqlite3
```

To inspect a valid older backup without requiring the current version:

```bash
python runtime_state_admin.py verify \
  --database /path/to/older-runtime-state.sqlite3 \
  --allow-older
```

## Apply pending migrations

Stop every process that opens the shared database before a planned upgrade:

```bash
sudo systemctl stop face-attendance-watch.service
sudo systemctl stop face-attendance-ftp.service
sudo systemctl stop face-attendance-web.service
sudo systemctl stop face-attendance-sync.timer
```

Run the migration command:

```bash
cd /opt/face-attendance
source .venv/bin/activate
python runtime_state_admin.py migrate \
  --database /opt/face-attendance/runtime_state.sqlite3
```

For an existing database, the command prints the verified pre-migration backup path. A missing, corrupt, or unsafe backup aborts the migration.

Verify again before restarting services:

```bash
python runtime_state_admin.py verify \
  --database /opt/face-attendance/runtime_state.sqlite3
```

Then restart and inspect service logs:

```bash
sudo systemctl start face-attendance-ftp.service
sudo systemctl start face-attendance-watch.service
sudo systemctl start face-attendance-web.service
sudo systemctl start face-attendance-sync.timer
sudo journalctl -u face-attendance-watch.service -n 100 --no-pager
```

Normal application startup also runs pending migrations. The explicit command is preferred during a controlled release because it makes the backup and verification output visible before services start.

## Create and verify a manual backup

```bash
python runtime_state_admin.py backup \
  --database /opt/face-attendance/runtime_state.sqlite3 \
  --reason pre-upgrade
```

Verify a selected backup and its metadata:

```bash
python runtime_state_admin.py verify-backup \
  /opt/face-attendance/runtime_state_backups/<backup>.sqlite3
```

Use the SQLite backup API or this command while the database may be active. Do not use a plain file copy of a live WAL-mode database.

## Roll back and restore

A schema rollback requires both the matching application revision and the matching pre-migration database backup.

1. Stop all services that open `runtime_state.sqlite3`.
2. Verify the selected backup.
3. Restore it with explicit confirmation.
4. Switch the application to the revision that understands that schema.
5. Verify the restored database using that revision before starting services.

```bash
cd /opt/face-attendance
source .venv/bin/activate
python runtime_state_admin.py verify-backup \
  /opt/face-attendance/runtime_state_backups/<backup>.sqlite3

python runtime_state_admin.py restore \
  --database /opt/face-attendance/runtime_state.sqlite3 \
  --confirm-restore \
  /opt/face-attendance/runtime_state_backups/<backup>.sqlite3
```

Before replacement, `restore` creates a verified safety backup of the current database. It restores through SQLite into a temporary file, runs integrity verification, atomically replaces the database, removes stale `-wal` and `-shm` files, and verifies the result again.

Do not start the newer application against a deliberately restored older schema: normal startup will apply its pending forward migrations again.

## Failure handling

- **Backup fails:** do not migrate. Correct disk space, ownership, filesystem, or database-integrity issues first.
- **Checksum mismatch:** do not edit `schema_migrations`. Restore an untampered backup or deploy the exact code revision that created the database.
- **Future schema version:** the installed code is older than the database. Deploy the matching or newer code; never decrement the version manually.
- **Post-migration verification fails:** leave services stopped and restore the printed pre-migration backup.
- **Restore fails after the safety backup is created:** preserve both backups and investigate before attempting another replacement.

Backups contain operational and potentially biometric-adjacent metadata. Apply the approved encryption, access, retention, and expiry policy before production use.

## Current schema generations and frozen compatibility fixtures

The current runtime schema is version 5:

- version 1 adopts the original runtime-state tables;
- version 2 adds normalized events, recognition decisions, transitions, and operator actions;
- version 3 adds renewable processing leases and transactional attendance-policy state;
- version 4 adds audited event inspection/reprocessing metadata and immutable retained-source paths;
- version 5 adds explicit identifier schemes, accepted-decision delivery IDs, and permanent minimal replay tombstones.

Migration 4 preserves existing rows and gives older events blank source/retention paths; an operator must supply `--media-path` before reprocessing one of those migrated events. Migration 5 also preserves historical primary keys, labels them as legacy schemes rather than recomputing them, and backfills one minimal tombstone for every retained event.

Frozen synthetic databases for released versions 1–4 live under `tests/fixtures/`. Their manifest records the source commit, released migration checksum, raw database digest, compressed digest, and size. Tests materialize those exact committed bytes and run the normal verified backup-before-migrate path to version 5. See `docs/event-identity-tombstones.md` for the identifier and retention contract.
