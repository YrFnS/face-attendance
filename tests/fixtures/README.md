# Released runtime-state schema fixtures

These files are frozen, synthetic SQLite databases produced by the released
schema implementations listed in `manifest.json`. They are committed as
base64-encoded gzip payloads so Git and review tools can handle them without
mistaking them for mutable runtime databases.

The tests do **not** rebuild an old schema from the current migration catalog.
They verify the compressed and raw SHA-256 digests, materialize the committed
bytes, confirm the original `PRAGMA user_version` and migration checksum, and
then exercise the real backup-before-migrate path to schema version 5.

The fixture data is deliberately synthetic. It contains no employee photos,
embeddings, production credentials, or real attendance records. Versions 2 through 4 contain one synthetic recognition decision and operator
action; version 3 adds lease/policy state, and version 4 adds retained-source
and operator-revision metadata. The migration tests prove that this released
evidence remains readable and receives a legacy-scheme tombstone.

To replace a fixture, use the exact released application revision named in the
manifest, create a populated database through that revision, run SQLite
`quick_check`, and update both raw/compressed digests. Do not generate old
fixtures by importing the current `MIGRATIONS` tuple: that would only test the
current code against itself and would not detect accidental edits to a released
migration.
