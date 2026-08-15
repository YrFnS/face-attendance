"""Pure contract helpers shared by the Frappe bridge and its unit tests."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone

from . import __version__


APP_NAME = "face_attendance_idempotency"
APP_VERSION = __version__
CONTRACT_VERSION = "face-attendance/erpnext-checkin-idempotency/v1"
DELIVERY_PAYLOAD_CONTRACT_VERSION = "erpnext-employee-checkin-v1"
CREATE_METHOD = "face_attendance_idempotency.api.create_or_get_employee_checkin"
PROBE_METHOD = "face_attendance_idempotency.api.get_contract"
DOCTYPE = "Employee Checkin"
DELIVERY_FIELD = "custom_face_attendance_delivery_id"
EVENT_FIELD = "custom_face_attendance_event_id"
DECISION_FIELD = "custom_face_attendance_decision_id"
DELIVERY_CONTRACT_FIELD = "custom_face_attendance_contract_version"
CAMERA_FIELD = "custom_face_attendance_camera_id"
BRANCH_FIELD = "custom_face_attendance_branch"
UNIQUE_CONSTRAINT = "unique_face_attendance_delivery_id"
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


class ContractError(ValueError):
    pass


class DeliveryConflictError(ContractError):
    pass


class DuplicateDeliveryId(RuntimeError):
    """Raised by a storage adapter after the database unique key wins."""


def text(value, field, *, required=True, max_chars=512):
    if not isinstance(value, str):
        raise ContractError(f"{field} must be a string")
    result = unicodedata.normalize("NFC", value)
    if result != result.strip():
        raise ContractError(f"{field} must not contain surrounding whitespace")
    if required and not result:
        raise ContractError(f"{field} is required")
    if len(result) > int(max_chars):
        raise ContractError(f"{field} exceeds {int(max_chars)} characters")
    for character in result:
        if unicodedata.category(character) in {"Cc", "Cf", "Cs"}:
            raise ContractError(
                f"{field} contains a control or formatting character"
            )
    return result


def identifier(value, field):
    result = text(value, field, max_chars=64).lower()
    if not HEX64_RE.fullmatch(result):
        raise ContractError(
            f"{field} must be a lowercase 64-character SHA-256 identifier"
        )
    return result


def normalize_time(value):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        candidate = value.strip()
        if candidate != value or not candidate:
            raise ContractError("time must be a non-empty trimmed timestamp")
        candidate = candidate[:-1] + "+00:00" if candidate.endswith("Z") else candidate
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError as exc:
            raise ContractError("time must be a valid timestamp") from exc
    else:
        raise ContractError("time must be a timestamp string")
    if parsed.tzinfo is not None and parsed.utcoffset() is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


@dataclass(frozen=True)
class CreateRequest:
    delivery_id: str
    employee: str
    log_type: str
    time: str
    delivery_contract_version: str
    event_id: str
    decision_id: str
    camera_id: str
    branch: str

    @classmethod
    def build(
        cls,
        *,
        delivery_id,
        employee,
        log_type,
        time,
        delivery_contract_version,
        event_id,
        decision_id,
        camera_id,
        branch,
    ):
        direction = text(log_type, "log_type", max_chars=16).upper()
        if direction not in {"IN", "OUT"}:
            raise ContractError("log_type must be IN or OUT")
        request = cls(
            identifier(delivery_id, "delivery_id"),
            text(employee, "employee", max_chars=180),
            direction,
            normalize_time(time),
            text(
                delivery_contract_version,
                "delivery_contract_version",
                max_chars=128,
            ),
            identifier(event_id, "event_id"),
            identifier(decision_id, "decision_id"),
            text(camera_id, "camera_id", max_chars=128),
            text(branch, "branch", max_chars=128),
        )
        if request.delivery_contract_version != DELIVERY_PAYLOAD_CONTRACT_VERSION:
            raise ContractError(
                "delivery_contract_version does not match the supported payload contract"
            )
        return request

    def document_fields(self):
        return {
            "doctype": DOCTYPE,
            "employee": self.employee,
            "log_type": self.log_type,
            "time": self.time,
            DELIVERY_FIELD: self.delivery_id,
            EVENT_FIELD: self.event_id,
            DECISION_FIELD: self.decision_id,
            DELIVERY_CONTRACT_FIELD: self.delivery_contract_version,
            CAMERA_FIELD: self.camera_id,
            BRANCH_FIELD: self.branch,
        }

    def immutable_payload(self):
        return {
            "delivery_id": self.delivery_id,
            "employee": self.employee,
            "log_type": self.log_type,
            "time": self.time,
            "delivery_contract_version": self.delivery_contract_version,
            "event_id": self.event_id,
            "decision_id": self.decision_id,
            "camera_id": self.camera_id,
            "branch": self.branch,
        }


@dataclass(frozen=True)
class CreateResult:
    name: str
    created: bool
    request: CreateRequest


def capability_payload(site, database_type, *, unique_columns=(DELIVERY_FIELD,)):
    columns = [text(item, "unique column", max_chars=128) for item in unique_columns]
    payload = {
        "app": APP_NAME,
        "app_version": APP_VERSION,
        "contract_version": CONTRACT_VERSION,
        "delivery_payload_contract_version": DELIVERY_PAYLOAD_CONTRACT_VERSION,
        "site": text(site, "site", max_chars=255),
        "database_type": text(database_type, "database_type", max_chars=64),
        "doctype": DOCTYPE,
        "delivery_field": DELIVERY_FIELD,
        "event_field": EVENT_FIELD,
        "decision_field": DECISION_FIELD,
        "delivery_contract_field": DELIVERY_CONTRACT_FIELD,
        "camera_field": CAMERA_FIELD,
        "branch_field": BRANCH_FIELD,
        "unique_constraint": UNIQUE_CONSTRAINT,
        "unique_columns": columns,
        "unique_verified": columns == [DELIVERY_FIELD],
        "create_method": CREATE_METHOD,
        "probe_method": PROBE_METHOD,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    payload["fingerprint"] = hashlib.sha256(encoded).hexdigest()
    return payload


def normalize_existing(record):
    if not isinstance(record, dict):
        raise ContractError("existing Employee Checkin must be an object")
    return {
        "delivery_id": identifier(record.get("delivery_id"), "delivery_id"),
        "employee": text(record.get("employee"), "employee", max_chars=180),
        "log_type": text(record.get("log_type"), "log_type", max_chars=16).upper(),
        "time": normalize_time(record.get("time")),
        "delivery_contract_version": text(
            record.get("delivery_contract_version"),
            "delivery_contract_version",
            max_chars=128,
        ),
        "event_id": identifier(record.get("event_id"), "event_id"),
        "decision_id": identifier(record.get("decision_id"), "decision_id"),
        "camera_id": text(record.get("camera_id"), "camera_id", max_chars=128),
        "branch": text(record.get("branch"), "branch", max_chars=128),
    }


def assert_existing_matches(request, existing):
    normalized = normalize_existing(existing)
    expected = request.immutable_payload()
    mismatches = [
        field for field, value in expected.items() if normalized.get(field) != value
    ]
    if mismatches:
        raise DeliveryConflictError(
            "delivery_id is already bound to conflicting fields: "
            + ", ".join(sorted(mismatches))
        )


def create_or_get(store, request):
    """Attempt insert first; let the database unique constraint resolve races."""

    if not isinstance(request, CreateRequest):
        raise ContractError("request must be a CreateRequest")
    try:
        inserted = store.insert(request)
    except DuplicateDeliveryId:
        existing = store.find_for_update(request.delivery_id)
        if existing is None:
            raise ContractError(
                "delivery_id uniqueness conflict occurred without a readable winner"
            )
        assert_existing_matches(request, existing)
        return CreateResult(
            text(existing.get("name"), "Employee Checkin name", max_chars=140),
            False,
            request,
        )
    return CreateResult(
        text(inserted.get("name"), "Employee Checkin name", max_chars=140),
        True,
        request,
    )
