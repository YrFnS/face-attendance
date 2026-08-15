"""Authenticated atomic Employee Checkin create-or-get endpoint."""

from __future__ import annotations

from .contract import (
    BRANCH_FIELD,
    CAMERA_FIELD,
    CONTRACT_VERSION,
    DECISION_FIELD,
    DELIVERY_CONTRACT_FIELD,
    DELIVERY_FIELD,
    DELIVERY_PAYLOAD_CONTRACT_VERSION,
    DOCTYPE,
    EVENT_FIELD,
    UNIQUE_CONSTRAINT,
    ContractError,
    CreateRequest,
    DeliveryConflictError,
    DuplicateDeliveryId,
    capability_payload,
    create_or_get,
)
from .install import unique_constraint_columns, verify_schema


class FaceAttendanceContractUnavailable(Exception):
    http_status_code = 503


def _frappe():
    import frappe

    return frappe


def _require_permissions(frappe):
    if not frappe.has_permission(DOCTYPE, ptype="create"):
        raise frappe.PermissionError(
            f"Create permission is required for {DOCTYPE}"
        )
    if not frappe.has_permission(DOCTYPE, ptype="read"):
        raise frappe.PermissionError(
            f"Read permission is required for {DOCTYPE}"
        )


def _site_name(frappe):
    return str(getattr(frappe.local, "site", "") or "").strip()


def _database_type(frappe):
    return str(getattr(frappe.db, "db_type", "") or "").strip().lower()


def _current_capability(frappe):
    verify_schema(frappe)
    return capability_payload(
        _site_name(frappe),
        _database_type(frappe),
        unique_columns=unique_constraint_columns(frappe),
    )


def _is_unique_violation(frappe, exc):
    del frappe
    pending = [exc]
    seen = set()
    while pending:
        cursor = pending.pop()
        if cursor is None or id(cursor) in seen:
            continue
        seen.add(id(cursor))
        name = type(cursor).__name__.lower()
        args = getattr(cursor, "args", ())
        diagnostic = getattr(cursor, "diag", None)
        constraint = str(
            getattr(cursor, "constraint_name", "")
            or getattr(diagnostic, "constraint_name", "")
            or ""
        ).lower()
        message = " ".join(
            [str(cursor), constraint, *(str(value) for value in args)]
        ).lower()
        if "integrityerror" in name or "duplicate" in name or "unique" in name:
            if (
                DELIVERY_FIELD.lower() in message
                or UNIQUE_CONSTRAINT.lower() in message
            ):
                return True
        pending.extend(
            value for value in args if isinstance(value, BaseException)
        )
        pending.extend(
            [
                getattr(cursor, "__cause__", None),
                getattr(cursor, "__context__", None),
            ]
        )
    return False


class FrappeCheckinStore:
    def __init__(self, frappe):
        self.frappe = frappe

    def _record(self, row):
        if row is None:
            return None
        return {
            "name": row["name"],
            "delivery_id": row[DELIVERY_FIELD],
            "employee": row["employee"],
            "log_type": row["log_type"],
            "time": row["time"],
            "delivery_contract_version": row[DELIVERY_CONTRACT_FIELD],
            "event_id": row[EVENT_FIELD],
            "decision_id": row[DECISION_FIELD],
            "camera_id": row[CAMERA_FIELD],
            "branch": row[BRANCH_FIELD],
        }

    def find_for_update(self, delivery_id):
        rows = self.frappe.db.sql(
            f"""
            SELECT name, employee, log_type, time,
                   `{DELIVERY_FIELD}`, `{EVENT_FIELD}`, `{DECISION_FIELD}`,
                   `{DELIVERY_CONTRACT_FIELD}`, `{CAMERA_FIELD}`, `{BRANCH_FIELD}`
            FROM `tab{DOCTYPE}`
            WHERE `{DELIVERY_FIELD}` = %s
            FOR UPDATE
            """,
            (delivery_id,),
            as_dict=True,
        )
        return self._record(rows[0]) if rows else None

    def insert(self, request):
        savepoint = f"face_attendance_delivery_{request.delivery_id[:16]}"
        self.frappe.db.savepoint(savepoint)
        try:
            document = self.frappe.get_doc(request.document_fields())
            document.insert()
            self.frappe.db.release_savepoint(savepoint)
            return {"name": document.name}
        except Exception as exc:
            self.frappe.db.rollback(save_point=savepoint)
            self.frappe.db.release_savepoint(savepoint)
            if _is_unique_violation(self.frappe, exc):
                raise DuplicateDeliveryId() from exc
            raise


def _validate_destination(capability, *, expected_site, expected_contract, expected_fingerprint):
    expected_site = str(expected_site or "").strip()
    expected_contract = str(expected_contract or "").strip()
    expected_fingerprint = str(expected_fingerprint or "").strip().lower()
    if not expected_site or not expected_contract or not expected_fingerprint:
        raise ContractError(
            "expected_site, expected_contract_version, and expected_fingerprint are required"
        )
    if capability["site"] != expected_site:
        raise ContractError("ERPNext destination site does not match the request")
    if capability["contract_version"] != expected_contract:
        raise ContractError("ERPNext idempotency contract version does not match")
    if capability["fingerprint"] != expected_fingerprint:
        raise ContractError("ERPNext idempotency capability fingerprint does not match")


def _error_response(frappe, *, status, code, message):
    response = getattr(getattr(frappe, "local", None), "response", None)
    if isinstance(response, dict):
        response["http_status_code"] = int(status)
    return {
        "ok": False,
        "error_code": str(code),
        "message": str(message),
    }


def get_contract():
    frappe = _frappe()
    _require_permissions(frappe)
    try:
        return _current_capability(frappe)
    except Exception as exc:
        raise FaceAttendanceContractUnavailable(str(exc)) from exc


def create_or_get_employee_checkin(
    delivery_id=None,
    employee=None,
    log_type=None,
    time=None,
    delivery_contract_version=None,
    event_id=None,
    decision_id=None,
    camera_id=None,
    branch=None,
    expected_site=None,
    expected_contract_version=CONTRACT_VERSION,
    expected_fingerprint=None,
):
    frappe = _frappe()
    _require_permissions(frappe)
    try:
        capability = _current_capability(frappe)
        _validate_destination(
            capability,
            expected_site=expected_site,
            expected_contract=expected_contract_version,
            expected_fingerprint=expected_fingerprint,
        )
        request = CreateRequest.build(
            delivery_id=delivery_id,
            employee=employee,
            log_type=log_type,
            time=time,
            delivery_contract_version=delivery_contract_version,
            event_id=event_id,
            decision_id=decision_id,
            camera_id=camera_id,
            branch=branch,
        )
        result = create_or_get(FrappeCheckinStore(frappe), request)
    except DeliveryConflictError as exc:
        return _error_response(
            frappe,
            status=409,
            code="delivery_id_conflict",
            message=str(exc),
        )
    except ContractError as exc:
        return _error_response(
            frappe,
            status=422,
            code="validation_error",
            message=str(exc),
        )
    return {
        "ok": True,
        "name": result.name,
        "created": bool(result.created),
        "delivery_id": result.request.delivery_id,
        "contract_version": capability["contract_version"],
        "site": capability["site"],
        "fingerprint": capability["fingerprint"],
        "delivery_payload_contract_version": DELIVERY_PAYLOAD_CONTRACT_VERSION,
    }


# Frappe attaches whitelisting metadata to the wrapped functions.
try:
    import frappe as _frappe_module
except ImportError:  # unit tests import the pure package without Frappe installed
    _frappe_module = None

if _frappe_module is not None:
    get_contract = _frappe_module.whitelist()(get_contract)
    create_or_get_employee_checkin = _frappe_module.whitelist()(
        create_or_get_employee_checkin
    )
