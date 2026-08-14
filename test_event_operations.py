import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from camera_sources import receipt_path
from event_admin import (
    EventAdminError,
    ReadOnlyEventState,
    _visible_image_name,
    mutable_event_state,
    reprocess_event,
    verify_event_media,
)
from event_ledger import make_capture_id
from event_operations import (
    EventOperationValidationError,
    MAX_FILESYSTEM_NAME_BYTES,
    operator_staging_path,
)
from processing_recovery import attendance_policy_scope_key
from runtime_state import (
    MIGRATION_BY_VERSION,
    RUNTIME_SCHEMA_VERSION,
    RuntimeState,
    file_sha256,
    make_event_id,
    utc_now,
)


class EventOperationsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "runtime.sqlite3"
        self.state = RuntimeState(self.database, backup_dir=self.root / "backups")
        self.upload_dir = self.root / "camera_uploads" / "in"
        self.quarantine_dir = self.root / "logs" / "quarantine" / "no_face"
        self.upload_dir.mkdir(parents=True)
        self.quarantine_dir.mkdir(parents=True)
        self.config = self.root / "config.json"
        self.config.write_text(
            json.dumps(
                {
                    "branch_name": "Baghdad",
                    "camera_uploads_dir": str(self.root / "camera_uploads"),
                    "ftp_permissions": "elw",
                    "ftp_users": {
                        "camera_in": {
                            "password": "camera-in-password-unique",
                            "permissions": "elw",
                        }
                    },
                    "camera_sources": {
                        "camera-in": {
                            "source_type": "holowits_ftp",
                            "branch": "Baghdad",
                            "policy": "IN",
                            "ftp_username": "camera_in",
                            "upload_dir": str(self.upload_dir),
                            "allowed_networks": ["192.0.2.10/32"],
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    def record_event(
        self,
        *,
        name="event.jpg",
        content=b"event-image",
        lifecycle="rejected",
        reason="no_face",
        retention_state="quarantined",
        receipt_detail=None,
        receipt_state="invalid",
        receipt_verified=False,
    ):
        media = self.quarantine_dir / name
        media.write_bytes(content)
        digest, size = file_sha256(media)
        event_id = make_event_id("camera-in", "IN", digest)
        capture_id = make_capture_id(
            "camera-in", digest, name, size, media.stat().st_mtime
        )
        original = self.upload_dir / name
        claim = self.state.record_event_receipt(
            event_id=event_id,
            capture_id=capture_id,
            camera_id="camera-in",
            log_type="IN",
            source_sha256=digest,
            source_name=name,
            source_mtime=media.stat().st_mtime,
            source_size=size,
            received_at="2026-08-14T00:00:00Z",
            effective_at="2026-08-14T00:00:00Z",
            source_path=str(original),
            retention_path=str(media),
            branch="Baghdad",
            source_type="holowits_ftp",
            source_principal="camera_in",
            source_remote_ip="192.0.2.10",
            source_binding_id="b" * 64,
            policy="IN",
            source_time_provenance="filesystem_mtime_untrusted",
            receipt_state=receipt_state,
            receipt_verified=receipt_verified,
            receipt_detail=receipt_detail or {"present": False},
            policy_version="directional-v1",
        )
        self.assertTrue(claim.accepted)
        self.state.transition_event(
            event_id,
            to_state=lifecycle,
            reason_code=reason,
            actor_type="watcher",
            event_updates={
                "retention_state": retention_state,
                "retention_path": str(media),
            },
            compatibility_status=lifecycle,
        )
        return event_id, media

    def test_schema_v4_columns_indexes_and_source_path_trigger(self):
        self.assertEqual(RUNTIME_SCHEMA_VERSION, 4)
        report = self.state.migration_status()
        self.assertEqual(report["schema_version"], 4)
        self.assertTrue(report["ok"], report)
        connection = sqlite3.connect(self.database)
        try:
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(camera_events)"
                ).fetchall()
            }
            indexes = {
                row[1]
                for row in connection.execute(
                    "PRAGMA index_list(camera_events)"
                ).fetchall()
            }
            triggers = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                ).fetchall()
            }
        finally:
            connection.close()
        self.assertTrue(
            {
                "source_path",
                "retention_path",
                "operator_revision",
                "last_operator_action_id",
                "last_operator_action_at",
            }.issubset(columns)
        )
        self.assertIn("camera_events_operator_revision", indexes)
        self.assertIn("camera_events_retention_state", indexes)
        self.assertIn("camera_events_source_path_immutable", triggers)

        event_id, _ = self.record_event()
        connection = sqlite3.connect(self.database)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE camera_events SET source_path = '/changed' WHERE event_id = ?",
                    (event_id,),
                )
            connection.rollback()
        finally:
            connection.close()

    def test_schema_v3_database_is_backed_up_and_preserved(self):
        previous = self.root / "schema-v3.sqlite3"
        connection = sqlite3.connect(previous)
        try:
            for version in (1, 2, 3):
                migration = MIGRATION_BY_VERSION[version]
                for statement in migration.statements:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations "
                    "(version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
                    (version, migration.name, migration.checksum, utc_now()),
                )
                connection.execute(f"PRAGMA user_version = {version}")
            event_id = make_event_id("legacy-camera", "IN", "legacy-content")
            connection.execute(
                """
                INSERT INTO camera_events (
                    event_id, camera_id, log_type, source_sha256, source_name,
                    source_mtime, source_size, status, created_unix, updated_unix,
                    completed_at, error
                ) VALUES (?, 'legacy-camera', 'IN', 'legacy-content', 'old.jpg',
                          1, 10, 'processed', 1, 2,
                          '2026-08-13T00:00:00Z', '')
                """,
                (event_id,),
            )
            connection.commit()
        finally:
            connection.close()

        migrated = RuntimeState(previous, backup_dir=self.root / "v3-backups")
        self.assertEqual(migrated.migration_status()["schema_version"], 4)
        self.assertIsNotNone(migrated.last_migration_backup)
        event = migrated.get_event(event_id)
        self.assertEqual(event["source_name"], "old.jpg")
        self.assertEqual(event["source_path"], "")
        self.assertEqual(event["retention_path"], "")

    def test_operator_cli_does_not_auto_migrate_and_rejects_database_symlink(self):
        previous = self.root / "operator-v3.sqlite3"
        connection = sqlite3.connect(previous)
        try:
            for version in (1, 2, 3):
                migration = MIGRATION_BY_VERSION[version]
                for statement in migration.statements:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations "
                    "(version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
                    (version, migration.name, migration.checksum, utc_now()),
                )
                connection.execute(f"PRAGMA user_version = {version}")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(EventAdminError, "must be migrated"):
            mutable_event_state(previous)
        connection = sqlite3.connect(previous)
        try:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 3)
        finally:
            connection.close()

        link = self.root / "runtime-link.sqlite3"
        try:
            link.symlink_to(self.database)
        except OSError:
            self.skipTest("symbolic links are not supported")
        with self.assertRaisesRegex(EventAdminError, "symbolic link"):
            ReadOnlyEventState(link)
        with self.assertRaisesRegex(EventAdminError, "symbolic link"):
            mutable_event_state(link)

    def test_reprocess_filename_reserves_space_for_hidden_utf8_stage(self):
        name = "موظف" * 100 + ".jpg"
        result = _visible_image_name(
            {"event_id": "a" * 64, "source_name": name}
        )
        stage = operator_staging_path(self.upload_dir / result)
        self.assertLessEqual(len(stage.name.encode("utf-8")), MAX_FILESYSTEM_NAME_BYTES)
        self.assertIn("a" * 64, result)
        self.assertTrue(result.endswith(".jpg"))

    def test_read_only_list_inspect_and_explain_redact_sensitive_data(self):
        event_id, _ = self.record_event(
            receipt_detail={
                "signature": "sensitive-signature",
                "nested": {"token": "sensitive-token"},
                "message": "Authorization: Bearer sensitive-value",
            }
        )
        self.state.record_recognition_decision(
            event_id=event_id,
            face_index=1,
            face_count=1,
            bbox=[1, 2, 3, 4],
            face_width=2,
            face_height=2,
            detection_score=0.9,
            best_employee="HR-0001",
            best_score=0.8,
            runner_up_score=0.4,
            score_margin=0.4,
            accepted=False,
            reason_code="unknown_employee",
            retention_state="not_retained",
        )
        reader = ReadOnlyEventState(self.database)
        rows = reader.list_events(employee="HR-0001", state="rejected")
        self.assertEqual([row["event_id"] for row in rows], [event_id])
        inspected = reader.inspect_event(event_id)
        serialized = json.dumps(inspected, ensure_ascii=False)
        self.assertNotIn("sensitive-signature", serialized)
        self.assertNotIn("sensitive-token", serialized)
        self.assertNotIn("sensitive-value", serialized)
        self.assertIn("<redacted>", serialized)
        self.assertEqual(inspected["source_path"], "event.jpg")
        explained = reader.explain_event(event_id)
        self.assertEqual(explained["event"]["event_id"], event_id)
        self.assertEqual(explained["decisions"][0]["employee"], "HR-0001")
        self.assertNotIn("receipt_json", json.dumps(explained))

    def test_reprocess_uses_operator_lease_then_allows_watcher(self):
        event_id, media = self.record_event()
        target = self.upload_dir / "reprocess.jpg"
        result = self.state.request_event_reprocess(
            event_id,
            actor="operator@example.com",
            reason="Reviewed source evidence and approved a safe retry",
            media_path=str(target),
            action="quarantine_requeued",
            publish_lease_seconds=120,
            now=1000.0,
        )
        event = self.state.get_event(event_id)
        self.assertEqual(event["lifecycle_state"], "received")
        self.assertEqual(event["retention_path"], str(target))
        self.assertEqual(event["lease_owner"], f"operator:{result['action_id']}")
        blocked = self.state.acquire_event_lease(
            event_id, owner="watcher-2", lease_seconds=60, now=1001.0
        )
        self.assertFalse(blocked.accepted)
        self.assertEqual(blocked.reason, "active_lease")
        self.state.complete_event_reprocess_publish(
            event_id,
            action_id=result["action_id"],
            media_path=str(target),
            now=1002.0,
        )
        acquired = self.state.acquire_event_lease(
            event_id, owner="watcher-2", lease_seconds=60, now=1003.0
        )
        self.assertTrue(acquired.accepted)
        event = self.state.get_event(event_id)
        self.assertEqual(event["operator_actions"][-1]["action"], "quarantine_requeued")
        self.assertEqual(event["transitions"][-2]["reason_code"], "operator_action")
        self.assertTrue(media.exists())

    def test_operator_timestamps_must_be_finite_and_publication_lease_current(self):
        event_id, _ = self.record_event()
        with self.assertRaisesRegex(
            EventOperationValidationError, "finite non-negative"
        ):
            self.state.request_event_reprocess(
                event_id,
                actor="operator@example.com",
                reason="Reviewed source evidence and approved a safe retry",
                media_path=str(self.upload_dir / "reprocess.jpg"),
                now=float("nan"),
            )

        result = self.state.request_event_reprocess(
            event_id,
            actor="operator@example.com",
            reason="Reviewed source evidence and approved a safe retry",
            media_path=str(self.upload_dir / "reprocess.jpg"),
            now=1000.0,
            publish_lease_seconds=30,
        )
        with self.assertRaisesRegex(
            EventOperationValidationError, "publication lease has expired"
        ):
            self.state.complete_event_reprocess_publish(
                event_id,
                action_id=result["action_id"],
                media_path=str(self.upload_dir / "reprocess.jpg"),
                now=1031.0,
            )

    def test_reprocess_refuses_active_lease_and_delivery_boundary(self):
        event_id, _ = self.record_event()
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "UPDATE camera_events SET lease_owner='worker', lease_expires_unix=2000 "
                "WHERE event_id=?",
                (event_id,),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(EventOperationValidationError, "active processing lease"):
            self.state.request_event_reprocess(
                event_id,
                actor="operator",
                reason="Retry was requested after a complete review",
                media_path=str(self.quarantine_dir / "event.jpg"),
                now=1000.0,
            )

        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "UPDATE camera_events SET lease_owner='', lease_expires_unix=0, "
                "delivery_started_at='2026-08-14T00:00:00Z' WHERE event_id=?",
                (event_id,),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(EventOperationValidationError, "delivery boundary"):
            self.state.request_event_reprocess(
                event_id,
                actor="operator",
                reason="Retry was requested after a complete review",
                media_path=str(self.quarantine_dir / "event.jpg"),
                now=1000.0,
            )

    def test_uncertain_dismissal_requires_acknowledgement_and_releases_policy(self):
        event_id, _ = self.record_event(lifecycle="failed", reason="generic_failed")
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "UPDATE camera_events SET lifecycle_state='uncertain', status='uncertain', "
                "processing_phase='terminal', delivery_started_at='2026-08-14T00:00:01Z' "
                "WHERE event_id=?",
                (event_id,),
            )
            scope = attendance_policy_scope_key(
                "HR-0001", "IN", "Baghdad", "directional-v1"
            )
            connection.execute(
                """
                INSERT INTO attendance_policy_state (
                    scope_key, employee, branch, direction, policy_version,
                    reservation_event_id, reservation_decision_id,
                    reservation_effective_at, reservation_effective_unix,
                    reservation_state, reservation_expires_unix, updated_at
                ) VALUES (?, 'HR-0001', 'Baghdad', 'IN', 'directional-v1',
                          ?, ?, '2026-08-14T00:00:00Z', 1,
                          'uncertain', 0, '2026-08-14T00:00:00Z')
                """,
                (scope, event_id, "c" * 64),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(EventOperationValidationError, "ERPNext verification"):
            self.state.dismiss_event(
                event_id,
                actor="supervisor@example.com",
                reason="ERPNext outcome has not yet been verified",
            )
        result = self.state.dismiss_event(
            event_id,
            actor="supervisor@example.com",
            reason="ERPNext was checked and the local event should be dismissed",
            acknowledge_delivery_checked=True,
        )
        self.assertEqual(result["state"], "dismissed")
        event = self.state.get_event(event_id)
        self.assertEqual(event["lifecycle_state"], "dismissed")
        self.assertEqual(event["operator_actions"][-1]["action"], "dismissed")
        connection = sqlite3.connect(self.database)
        try:
            row = connection.execute(
                "SELECT reservation_state, reservation_event_id "
                "FROM attendance_policy_state WHERE scope_key=?",
                (scope,),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(row, ("none", ""))

    def test_full_quarantine_requeue_moves_verified_content_and_audits(self):
        event_id, media = self.record_event(
            receipt_state="verified",
            receipt_verified=True,
            receipt_detail={"verified": True},
        )
        receipt_path(media).write_text('{"verified":true}\n', encoding="utf-8")
        result = reprocess_event(
            self.state,
            event_id=event_id,
            actor="operator@example.com",
            reason="Quarantine evidence was reviewed and approved for reprocessing",
            config_path=self.config,
            media_path=media,
            action="quarantine_requeued",
        )
        published = Path(result["published_path"])
        self.assertTrue(published.is_file())
        self.assertTrue(receipt_path(published).is_file())
        self.assertFalse(media.exists())
        self.assertFalse(receipt_path(media).exists())
        self.assertFalse(operator_staging_path(published).exists())
        event = self.state.get_event(event_id)
        self.assertEqual(event["lifecycle_state"], "received")
        self.assertEqual(event["retention_path"], str(published))
        self.assertEqual(event["lease_owner"], "")
        self.assertEqual(event["operator_actions"][-1]["action"], "quarantine_requeued")
        digest, size = file_sha256(published)
        self.assertEqual(digest, event["source_sha256"])
        self.assertEqual(size, event["source_size"])

    def test_media_hash_mismatch_and_symlink_are_rejected(self):
        event_id, media = self.record_event()
        event = self.state.get_event(event_id)
        media.write_bytes(b"altered-img")
        with self.assertRaisesRegex(Exception, "SHA-256"):
            verify_event_media(event, media)

        media.write_bytes(b"event-image")
        link = self.root / "media-link.jpg"
        try:
            link.symlink_to(media)
        except OSError:
            self.skipTest("symbolic links are not supported")
        with self.assertRaisesRegex(Exception, "symbolic-link"):
            verify_event_media(event, link)

    def test_confirmed_denied_mutation_is_audited_and_delivery_commands_absent(self):
        event_id, media = self.record_event()
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "UPDATE camera_events SET lease_owner='active-worker', "
                "lease_expires_unix=9999999999 WHERE event_id=?",
                (event_id,),
            )
            connection.commit()
        finally:
            connection.close()

        denied = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).with_name("event_admin.py")),
                "reprocess",
                event_id,
                "--database",
                str(self.database),
                "--config",
                str(self.config),
                "--media-path",
                str(media),
                "--actor",
                "operator@example.com",
                "--reason",
                "Reviewed the event but another worker still owns it",
                "--confirm",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("active processing lease", denied.stderr)
        self.assertTrue(media.is_file())
        audit = self.state.recent_audit()[0]
        self.assertEqual(audit["action"], "event_reprocess_denied")
        self.assertEqual(audit["detail"]["event_id"], event_id)

        help_result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).with_name("event_admin.py")),
                "--help",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertNotIn("delivery-retry", help_result.stdout)
        self.assertNotIn("delivery-cancel", help_result.stdout)

    def test_read_only_cli_and_confirmation_guard(self):
        event_id, media = self.record_event()
        before_digest, before_size = file_sha256(self.database)
        command = [
            sys.executable,
            str(Path(__file__).with_name("event_admin.py")),
            "explain",
            event_id,
            "--database",
            str(self.database),
        ]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["explain"]["event"]["event_id"], event_id)
        self.assertNotIn(str(self.root), completed.stdout)
        after_digest, after_size = file_sha256(self.database)
        self.assertEqual((after_digest, after_size), (before_digest, before_size))

        denied = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).with_name("event_admin.py")),
                "reprocess",
                event_id,
                "--database",
                str(self.database),
                "--config",
                str(self.config),
                "--media-path",
                str(media),
                "--actor",
                "operator@example.com",
                "--reason",
                "A complete review was performed",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("requires --confirm", denied.stderr)
        self.assertEqual(self.state.get_event(event_id)["lifecycle_state"], "rejected")


if __name__ == "__main__":
    unittest.main()
