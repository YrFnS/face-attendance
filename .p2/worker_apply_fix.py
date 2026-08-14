from pathlib import Path

path = Path(__file__).resolve().parent.parent / "processing_recovery.py"
source = path.read_text(encoding="utf-8")
needle = '''                    reserved_job = connection.execute(
                        """
                        SELECT state, next_attempt_unix, lease_expires_unix
                        FROM delivery_jobs WHERE decision_id = ?
                        """,
                        (row["reservation_decision_id"],),
                    ).fetchone()
'''
replacement = '''                    reserved_event = connection.execute(
                        """
                        SELECT lifecycle_state, processing_phase,
                               delivery_started_at, lease_expires_unix
                        FROM camera_events WHERE event_id = ?
                        """,
                        (row["reservation_event_id"],),
                    ).fetchone()
                    if (
                        reserved_event is not None
                        and reserved_event["lifecycle_state"]
                        not in TERMINAL_EVENT_STATES
                        and (
                            reserved_event["processing_phase"]
                            == "delivery_in_progress"
                            or bool(reserved_event["delivery_started_at"])
                        )
                    ):
                        connection.execute(
                            """
                            UPDATE attendance_policy_state
                            SET reservation_state = 'uncertain',
                                reservation_expires_unix = 0,
                                updated_at = ?
                            WHERE scope_key = ?
                              AND reservation_state = 'pending'
                            """,
                            (utc_now(), scope_key),
                        )
                        connection.commit()
                        return PolicyReservation(
                            False,
                            scope_key,
                            "uncertain_reservation",
                            existing_event_id=row["reservation_event_id"],
                            existing_decision_id=row["reservation_decision_id"],
                        )
                    reserved_job = connection.execute(
                        """
                        SELECT state, next_attempt_unix, lease_expires_unix
                        FROM delivery_jobs WHERE decision_id = ?
                        """,
                        (row["reservation_decision_id"],),
                    ).fetchone()
'''
if source.count(needle) != 1:
    raise SystemExit("P2-03 delivery-boundary precedence patch did not match")
path.write_text(source.replace(needle, replacement, 1), encoding="utf-8")
