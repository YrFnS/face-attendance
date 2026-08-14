from pathlib import Path


ROOT = Path(__file__).resolve().parent


def replace_once(path, old, new):
    path = ROOT / path
    source = path.read_text(encoding="utf-8")
    count = source.count(old)
    if count != 1:
        raise SystemExit(
            f"expected one portability match in {path}, found {count}: {old[:120]!r}"
        )
    path.write_text(source.replace(old, new, 1), encoding="utf-8")


def main():
    replace_once(
        "runtime_state.py",
        '''            if metadata.get("backup_path") != str(backup_path.resolve()):
                report["errors"].append("backup path does not match metadata")
''',
        "",
    )

    replace_once(
        "test_runtime_state.py",
        '''    def test_backup_metadata_is_bound_to_schema_and_path(self):
        backup = create_runtime_backup(self.database, self.backups)
        backup_path = Path(backup["path"])
        metadata_path = backup_path.with_suffix(backup_path.suffix + ".json")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["schema"] = "untrusted-format"
        metadata["backup_path"] = str(self.root / "other.sqlite3")
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        report = verify_runtime_backup(backup_path)
        self.assertFalse(report["ok"])
        self.assertTrue(
            any("metadata schema" in error for error in report["errors"])
        )
        self.assertTrue(
            any("path does not match" in error for error in report["errors"])
        )

''',
        '''    def test_backup_metadata_schema_is_verified_and_backup_is_relocatable(self):
        backup = create_runtime_backup(self.database, self.backups)
        backup_path = Path(backup["path"])
        metadata_path = backup_path.with_suffix(backup_path.suffix + ".json")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["schema"] = "untrusted-format"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        report = verify_runtime_backup(backup_path)
        self.assertFalse(report["ok"])
        self.assertTrue(
            any("metadata schema" in error for error in report["errors"])
        )

        metadata["schema"] = "face-attendance-runtime-backup/v1"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        relocated = self.root / "offsite" / backup_path.name
        relocated.parent.mkdir()
        relocated.write_bytes(backup_path.read_bytes())
        relocated.with_suffix(relocated.suffix + ".json").write_text(
            metadata_path.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        relocated_report = verify_runtime_backup(relocated)
        self.assertTrue(relocated_report["ok"], relocated_report)

''',
    )

    replace_once(
        "docs/runtime-state-migrations.md",
        '''The sidecar records the source and target schema versions, reason, size, and complete SHA-256 digest. Do not delete the sidecar while retaining the backup.
''',
        '''The sidecar records the source and target schema versions, reason, size, and complete SHA-256 digest. Do not delete the sidecar while retaining the backup. The database file and its sidecar may be copied together to encrypted off-host storage or another recovery host; recorded absolute paths are provenance fields and do not bind verification to the original location.
''',
    )


if __name__ == "__main__":
    main()
