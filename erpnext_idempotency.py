"""Verified ERPNext-side delivery idempotency contract.

The attendance node must not infer exactly-once delivery from local state alone.
This module validates the authenticated capability proof exposed by the
companion Frappe app and defines the runtime-state binding stored with each
outbound delivery job.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone

from event_identity import DEFAULT_DELIVERY_CONTRACT_VERSION


IDEMPOTENCY_APP_NAME = "face_attendance_idempotency"
IDEMPOTENCY_APP_VERSION = "1.0.0"
IDEMPOTENCY_CONTRACT_VERSION = (
    "face-attendance/erpnext-checkin-idempotency/v1"
)
DEFAULT_IDEMPOTENCY_CREATE_METHOD = (
    "face_attendance_idempotency.api.create_or_get_employee_checkin"
)
DEFAULT_IDEMPOTENCY_PROBE_METHOD = (
    "face_attendance_idempotency.api.get_contract"
)
EMPLOYEE_CHECKIN_DOCTYPE = "Employee Checkin"
DELIVERY_ID_FIELD = "custom_face_attendance_delivery_id"
EVENT_ID_FIELD = "custom_face_attendance_event_id"
DECISION_ID_FIELD = "custom_face_attendance_decision_id"
DELIVERY_CONTRACT_FIELD = "custom_face_attendance_contract_version"
CAMERA_ID_FIELD = "custom_face_attendance_camera_id"
BRANCH_FIELD = "custom_face_attendance_branch"
UNIQUE_CONSTRAINT_NAME = "unique_face_attendance_delivery_id"

HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
DOTTED_METHOD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$")


class ERPNextIdempotencyError(RuntimeError):
    """Base error for an invalid or unavailable ERPNext idempotency contract."""


class ERPNextIdempotencyConfigurationError(ERPNextIdempotencyError, ValueError):
    """Raised when local idempotency configuration is invalid."""


class ERPNextIdempotencyCapabilityError(ERPNextIdempotencyError):
    """Raised when the remote capability proof is missing or inconsistent."""


class ERPNextIdempotencyConflictError(ERPNextIdempotencyError):
    """Raised when one delivery ID is associated with conflicting payloads."""


def _text(value, field, *, required=True, max_chars=512):
    if not isinstance(value, str):
        raise ERPNextIdempotencyCapabilityError(f"{field} must be a string")
    text = unicodedata.normalize("NFC", value)
    if text != text.strip():
        raise ERPNextIdempotencyCapabilityError(
            f"{field} must not contain surrounding whitespace"
        )
    if required and not text:
        raise ERPNextIdempotencyCapabilityError(f"{field} is required")
    if len(text) > int(max_chars):
        raise ERPNextIdempotencyCapabilityError(
            f"{field} exceeds {int(max_chars)} characters"
        )
    for character in text:
        if unicodedata.category(character) in {"Cc", "Cf", "Cs"}:
            raise ERPNextIdempotencyCapabilityError(
                f"{field} contains a control or formatting character"
            )
    return text


def _identifier(value, field):
    text = _text(value, field, max_chars=64).lower()
    if not HEX64_RE.fullmatch(text):
        raise ERPNextIdempotencyCapabilityError(
            f"{field} must be a lowercase 64-character SHA-256 identifier"
        )
    return text


def _method(value, field):
    text = _text(value, field, max_chars=256)
    if not DOTTED_METHOD_RE.fullmatch(text):
        raise ERPNextIdempotencyCapabilityError(
            f"{field} must be a dotted Python method path"
        )
    return text


def canonical_capability_payload(payload):
    """Return the exact capability fields covered by the proof fingerprint."""

    if not isinstance(payload, dict):
        raise ERPNextIdempotencyCapabilityError(
            "ERPNext idempotency capability must be an object"
        )
    unique_columns = payload.get("unique_columns")
    if not isinstance(unique_columns, (list, tuple)):
        raise ERPNextIdempotencyCapabilityError(
            "unique_columns must be an array"
        )
    normalized_columns = [
        _text(item, "unique column", max_chars=128) for item in unique_columns
    ]
    if normalized_columns != [DELIVERY_ID_FIELD]:
        raise ERPNextIdempotencyCapabilityError(
            "ERPNext unique constraint must cover only the delivery ID field"
        )
    unique_verified = payload.get("unique_verified")
    if unique_verified is not True:
        raise ERPNextIdempotencyCapabilityError(
            "ERPNext delivery ID uniqueness is not verified"
        )
    return {
        "app": _text(payload.get("app"), "app", max_chars=128),
        "app_version": _text(
            payload.get("app_version"), "app_version", max_chars=64
        ),
        "contract_version": _text(
            payload.get("contract_version"),
            "contract_version",
            max_chars=128,
        ),
        "delivery_payload_contract_version": _text(
            payload.get("delivery_payload_contract_version"),
            "delivery_payload_contract_version",
            max_chars=128,
        ),
        "site": _text(payload.get("site"), "site", max_chars=255),
        "database_type": _text(
            payload.get("database_type"), "database_type", max_chars=64
        ),
        "doctype": _text(payload.get("doctype"), "doctype", max_chars=128),
        "delivery_field": _text(
            payload.get("delivery_field"), "delivery_field", max_chars=128
        ),
        "event_field": _text(
            payload.get("event_field"), "event_field", max_chars=128
        ),
        "decision_field": _text(
            payload.get("decision_field"), "decision_field", max_chars=128
        ),
        "delivery_contract_field": _text(
            payload.get("delivery_contract_field"),
            "delivery_contract_field",
            max_chars=128,
        ),
        "camera_field": _text(
            payload.get("camera_field"), "camera_field", max_chars=128
        ),
        "branch_field": _text(
            payload.get("branch_field"), "branch_field", max_chars=128
        ),
        "unique_constraint": _text(
            payload.get("unique_constraint"),
            "unique_constraint",
            max_chars=128,
        ),
        "unique_columns": normalized_columns,
        "unique_verified": True,
        "create_method": _method(payload.get("create_method"), "create_method"),
        "probe_method": _method(payload.get("probe_method"), "probe_method"),
    }


def capability_fingerprint(payload):
    canonical = canonical_capability_payload(payload)
    encoded = json.dumps(
        canonical,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ERPNextIdempotencyCapability:
    app: str
    app_version: str
    contract_version: str
    delivery_payload_contract_version: str
    site: str
    database_type: str
    doctype: str
    delivery_field: str
    event_field: str
    decision_field: str
    delivery_contract_field: str
    camera_field: str
    branch_field: str
    unique_constraint: str
    unique_columns: tuple[str, ...]
    create_method: str
    probe_method: str
    fingerprint: str

    def to_job_binding(self):
        return {
            "erpnext_site": self.site,
            "erpnext_idempotency_contract": self.contract_version,
            "erpnext_idempotency_fingerprint": self.fingerprint,
            "erpnext_idempotency_create_method": self.create_method,
            "erpnext_idempotency_app": self.app,
            "erpnext_idempotency_app_version": self.app_version,
        }


def parse_capability(
    payload,
    *,
    expected_site="",
    expected_contract_version=IDEMPOTENCY_CONTRACT_VERSION,
    expected_fingerprint="",
    expected_create_method=DEFAULT_IDEMPOTENCY_CREATE_METHOD,
    expected_probe_method=DEFAULT_IDEMPOTENCY_PROBE_METHOD,
):
    canonical = canonical_capability_payload(payload)
    fingerprint = _identifier(payload.get("fingerprint"), "fingerprint")
    calculated = capability_fingerprint(canonical)
    if fingerprint != calculated:
        raise ERPNextIdempotencyCapabilityError(
            "ERPNext idempotency capability fingerprint is invalid"
        )

    expected_site = (
        _text(expected_site, "expected_site", max_chars=255)
        if expected_site
        else ""
    )
    expected_contract_version = _text(
        expected_contract_version,
        "expected_contract_version",
        max_chars=128,
    )
    expected_create_method = _method(
        expected_create_method, "expected_create_method"
    )
    expected_probe_method = _method(
        expected_probe_method, "expected_probe_method"
    )
    if expected_fingerprint:
        expected_fingerprint = _identifier(
            expected_fingerprint, "expected_fingerprint"
        )

    expected_constants = {
        "app": IDEMPOTENCY_APP_NAME,
        "app_version": IDEMPOTENCY_APP_VERSION,
        "contract_version": expected_contract_version,
        "delivery_payload_contract_version": DEFAULT_DELIVERY_CONTRACT_VERSION,
        "doctype": EMPLOYEE_CHECKIN_DOCTYPE,
        "delivery_field": DELIVERY_ID_FIELD,
        "event_field": EVENT_ID_FIELD,
        "decision_field": DECISION_ID_FIELD,
        "delivery_contract_field": DELIVERY_CONTRACT_FIELD,
        "camera_field": CAMERA_ID_FIELD,
        "branch_field": BRANCH_FIELD,
        "unique_constraint": UNIQUE_CONSTRAINT_NAME,
        "create_method": expected_create_method,
        "probe_method": expected_probe_method,
    }
    for field, expected in expected_constants.items():
        if canonical[field] != expected:
            raise ERPNextIdempotencyCapabilityError(
                f"ERPNext idempotency {field} does not match the approved contract"
            )
    if expected_site and canonical["site"] != expected_site:
        raise ERPNextIdempotencyCapabilityError(
            "ERPNext site does not match erpnext_expected_site"
        )
    if expected_fingerprint and fingerprint != expected_fingerprint:
        raise ERPNextIdempotencyCapabilityError(
            "ERPNext capability fingerprint does not match the approved value"
        )

    return ERPNextIdempotencyCapability(
        app=canonical["app"],
        app_version=canonical["app_version"],
        contract_version=canonical["contract_version"],
        delivery_payload_contract_version=canonical[
            "delivery_payload_contract_version"
        ],
        site=canonical["site"],
        database_type=canonical["database_type"],
        doctype=canonical["doctype"],
        delivery_field=canonical["delivery_field"],
        event_field=canonical["event_field"],
        decision_field=canonical["decision_field"],
        delivery_contract_field=canonical["delivery_contract_field"],
        camera_field=canonical["camera_field"],
        branch_field=canonical["branch_field"],
        unique_constraint=canonical["unique_constraint"],
        unique_columns=tuple(canonical["unique_columns"]),
        create_method=canonical["create_method"],
        probe_method=canonical["probe_method"],
        fingerprint=fingerprint,
    )


def idempotency_configuration_issues(cfg):
    if not isinstance(cfg, dict):
        return ["ERPNext idempotency configuration must be a JSON object"]
    issues = []
    required = cfg.get("erpnext_idempotency_required", False)
    if not isinstance(required, bool):
        issues.append("erpnext_idempotency_required must be a boolean")
        required = False

    mode = str(cfg.get("delivery_mode") or "synchronous").strip().lower()
    production_delivery = bool(cfg.get("production_mode", False))
    if production_delivery and not required:
        issues.append(
            "production ERPNext delivery requires erpnext_idempotency_required=true"
        )

    contract = str(
        cfg.get("erpnext_idempotency_contract_version")
        or IDEMPOTENCY_CONTRACT_VERSION
    ).strip()
    if contract != IDEMPOTENCY_CONTRACT_VERSION:
        issues.append(
            "erpnext_idempotency_contract_version must match the supported contract"
        )

    create_method = str(
        cfg.get("erpnext_idempotency_create_method")
        or DEFAULT_IDEMPOTENCY_CREATE_METHOD
    ).strip()
    probe_method = str(
        cfg.get("erpnext_idempotency_probe_method")
        or DEFAULT_IDEMPOTENCY_PROBE_METHOD
    ).strip()
    if create_method != DEFAULT_IDEMPOTENCY_CREATE_METHOD:
        issues.append(
            "erpnext_idempotency_create_method must match the supported bridge method"
        )
    if probe_method != DEFAULT_IDEMPOTENCY_PROBE_METHOD:
        issues.append(
            "erpnext_idempotency_probe_method must match the supported bridge method"
        )

    expected_site = cfg.get("erpnext_expected_site", "")
    if not isinstance(expected_site, str) or expected_site != expected_site.strip():
        issues.append("erpnext_expected_site must be a trimmed string")
    elif len(expected_site) > 255:
        issues.append("erpnext_expected_site exceeds 255 characters")
    elif (required or production_delivery) and not expected_site:
        issues.append("erpnext_expected_site is required for idempotent delivery")

    fingerprint = cfg.get("erpnext_expected_idempotency_fingerprint", "")
    if not isinstance(fingerprint, str):
        issues.append(
            "erpnext_expected_idempotency_fingerprint must be a string"
        )
    else:
        fingerprint = fingerprint.strip().lower()
        if fingerprint and not HEX64_RE.fullmatch(fingerprint):
            issues.append(
                "erpnext_expected_idempotency_fingerprint must be lowercase SHA-256"
            )
        elif (required or production_delivery) and not fingerprint:
            issues.append(
                "erpnext_expected_idempotency_fingerprint is required for idempotent delivery"
            )

    cache_seconds = cfg.get("erpnext_idempotency_probe_cache_seconds", 300)
    if (
        isinstance(cache_seconds, bool)
        or not isinstance(cache_seconds, (int, float))
        or float(cache_seconds) < 1
        or float(cache_seconds) > 3600
    ):
        issues.append(
            "erpnext_idempotency_probe_cache_seconds must be between 1 and 3600"
        )
    return issues


IDEMPOTENCY_SCHEMA_STATEMENTS = (
    "ALTER TABLE delivery_jobs ADD COLUMN erpnext_site TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE delivery_jobs ADD COLUMN erpnext_idempotency_contract TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE delivery_jobs ADD COLUMN erpnext_idempotency_fingerprint TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE delivery_jobs ADD COLUMN erpnext_idempotency_create_method TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE delivery_jobs ADD COLUMN erpnext_idempotency_app TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE delivery_jobs ADD COLUMN erpnext_idempotency_app_version TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE delivery_jobs ADD COLUMN erpnext_idempotency_verified_at TEXT NOT NULL DEFAULT ''",
    """
    CREATE INDEX delivery_jobs_erpnext_contract
    ON delivery_jobs(
        erpnext_site,
        erpnext_idempotency_contract,
        erpnext_idempotency_fingerprint
    )
    """,
    """
    CREATE TRIGGER delivery_jobs_erpnext_contract_immutable
    BEFORE UPDATE OF
        erpnext_site,
        erpnext_idempotency_contract,
        erpnext_idempotency_fingerprint,
        erpnext_idempotency_create_method,
        erpnext_idempotency_app,
        erpnext_idempotency_app_version
    ON delivery_jobs
    WHEN OLD.erpnext_idempotency_fingerprint <> '' AND (
        NEW.erpnext_site <> OLD.erpnext_site
        OR NEW.erpnext_idempotency_contract <> OLD.erpnext_idempotency_contract
        OR NEW.erpnext_idempotency_fingerprint <> OLD.erpnext_idempotency_fingerprint
        OR NEW.erpnext_idempotency_create_method <> OLD.erpnext_idempotency_create_method
        OR NEW.erpnext_idempotency_app <> OLD.erpnext_idempotency_app
        OR NEW.erpnext_idempotency_app_version <> OLD.erpnext_idempotency_app_version
    )
    BEGIN
        SELECT RAISE(ABORT, 'ERPNext idempotency contract binding is immutable');
    END
    """,
    """
    CREATE TRIGGER delivery_jobs_submission_requires_verified_idempotency
    BEFORE UPDATE OF submission_started_at ON delivery_jobs
    WHEN NEW.submission_started_at <> ''
      AND NEW.erpnext_idempotency_contract <> ''
      AND (
          NEW.erpnext_site = ''
          OR NEW.erpnext_idempotency_fingerprint = ''
          OR NEW.erpnext_idempotency_create_method = ''
          OR NEW.erpnext_idempotency_verified_at = ''
      )
    BEGIN
        SELECT RAISE(
            ABORT,
            'idempotent submission requires a complete verified ERPNext contract'
        );
    END
    """,
)

IDEMPOTENCY_REQUIRED_TABLE_COLUMNS = {
    "delivery_jobs": {
        "erpnext_site": ("TEXT", True, 0),
        "erpnext_idempotency_contract": ("TEXT", True, 0),
        "erpnext_idempotency_fingerprint": ("TEXT", True, 0),
        "erpnext_idempotency_create_method": ("TEXT", True, 0),
        "erpnext_idempotency_app": ("TEXT", True, 0),
        "erpnext_idempotency_app_version": ("TEXT", True, 0),
        "erpnext_idempotency_verified_at": ("TEXT", True, 0),
    }
}
IDEMPOTENCY_REQUIRED_INDEXES = {
    "delivery_jobs_erpnext_contract": (
        False,
        (
            "erpnext_site",
            "erpnext_idempotency_contract",
            "erpnext_idempotency_fingerprint",
        ),
    )
}
IDEMPOTENCY_REQUIRED_TRIGGERS = frozenset(
    {
        "delivery_jobs_erpnext_contract_immutable",
        "delivery_jobs_submission_requires_verified_idempotency",
    }
)


def job_row_has_verified_idempotency(row):
    """Return whether a delivery row is bound to the supported remote contract."""

    try:
        return bool(
            row["erpnext_site"]
            and row["delivery_contract_version"]
            == DEFAULT_DELIVERY_CONTRACT_VERSION
            and row["erpnext_idempotency_contract"]
            == IDEMPOTENCY_CONTRACT_VERSION
            and HEX64_RE.fullmatch(
                str(row["erpnext_idempotency_fingerprint"] or "").lower()
            )
            and row["erpnext_idempotency_create_method"]
            == DEFAULT_IDEMPOTENCY_CREATE_METHOD
            and row["erpnext_idempotency_app"] == IDEMPOTENCY_APP_NAME
            and row["erpnext_idempotency_app_version"]
            and row["erpnext_idempotency_verified_at"]
        )
    except (KeyError, TypeError):
        return False


class ERPNextIdempotencyMixin:
    def bind_delivery_job_idempotency_contract_by_lease(
        self,
        delivery_id,
        *,
        owner,
        capability,
        now=None,
    ):
        if not isinstance(capability, ERPNextIdempotencyCapability):
            raise ERPNextIdempotencyCapabilityError(
                "capability must be an ERPNextIdempotencyCapability"
            )
        if (
            capability.delivery_payload_contract_version
            != DEFAULT_DELIVERY_CONTRACT_VERSION
        ):
            raise ERPNextIdempotencyCapabilityError(
                "ERPNext capability does not support the active delivery payload contract"
            )
        now = float(now if now is not None else datetime.now(timezone.utc).timestamp())
        if not math.isfinite(now) or now < 0:
            raise ERPNextIdempotencyCapabilityError("now must be finite")
        stamp = datetime.fromtimestamp(now, timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = self._current_delivery_lease_tx(
                connection,
                delivery_id=delivery_id,
                owner=owner,
                now=now,
            )
            if (
                str(row["delivery_contract_version"] or "")
                != capability.delivery_payload_contract_version
            ):
                raise ERPNextIdempotencyConflictError(
                    "delivery job payload contract does not match the verified ERPNext capability"
                )
            binding = capability.to_job_binding()
            existing_fingerprint = str(
                row["erpnext_idempotency_fingerprint"] or ""
            )
            if existing_fingerprint:
                comparisons = {
                    "erpnext_site": binding["erpnext_site"],
                    "erpnext_idempotency_contract": binding[
                        "erpnext_idempotency_contract"
                    ],
                    "erpnext_idempotency_fingerprint": binding[
                        "erpnext_idempotency_fingerprint"
                    ],
                    "erpnext_idempotency_create_method": binding[
                        "erpnext_idempotency_create_method"
                    ],
                    "erpnext_idempotency_app": binding[
                        "erpnext_idempotency_app"
                    ],
                    "erpnext_idempotency_app_version": binding[
                        "erpnext_idempotency_app_version"
                    ],
                }
                mismatches = [
                    field
                    for field, value in comparisons.items()
                    if str(row[field] or "") != str(value)
                ]
                if mismatches:
                    raise ERPNextIdempotencyConflictError(
                        "delivery job is already bound to a different ERPNext "
                        "idempotency contract: " + ", ".join(sorted(mismatches))
                    )
                connection.execute(
                    """
                    UPDATE delivery_jobs
                    SET erpnext_idempotency_verified_at = ?, updated_at = ?
                    WHERE delivery_id = ?
                    """,
                    (stamp, stamp, row["delivery_id"]),
                )
            else:
                connection.execute(
                    """
                    UPDATE delivery_jobs
                    SET erpnext_site = ?,
                        erpnext_idempotency_contract = ?,
                        erpnext_idempotency_fingerprint = ?,
                        erpnext_idempotency_create_method = ?,
                        erpnext_idempotency_app = ?,
                        erpnext_idempotency_app_version = ?,
                        erpnext_idempotency_verified_at = ?,
                        updated_at = ?
                    WHERE delivery_id = ?
                    """,
                    (
                        binding["erpnext_site"],
                        binding["erpnext_idempotency_contract"],
                        binding["erpnext_idempotency_fingerprint"],
                        binding["erpnext_idempotency_create_method"],
                        binding["erpnext_idempotency_app"],
                        binding["erpnext_idempotency_app_version"],
                        stamp,
                        stamp,
                        row["delivery_id"],
                    ),
                )
            current = connection.execute(
                "SELECT * FROM delivery_jobs WHERE delivery_id = ?",
                (row["delivery_id"],),
            ).fetchone()
            if not job_row_has_verified_idempotency(current):
                raise ERPNextIdempotencyCapabilityError(
                    "delivery job did not retain a complete ERPNext contract binding"
                )
            connection.commit()
            return dict(current)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
