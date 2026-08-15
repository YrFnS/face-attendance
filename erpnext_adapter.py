"""Explicit ERPNext delivery transport adapters.

The REST and local-bench implementations share one request contract. When the
P2-04 companion app is required, both transports probe the authenticated server
capability and call the same atomic create-or-get method keyed by delivery ID.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlsplit

import requests

from data_contract import (
    validate_employee_id,
    validate_erp_docname,
    validate_log_type,
)
from erpnext_idempotency import (
    DEFAULT_IDEMPOTENCY_CREATE_METHOD,
    DEFAULT_IDEMPOTENCY_PROBE_METHOD,
    IDEMPOTENCY_CONTRACT_VERSION,
    ERPNextIdempotencyCapability,
    ERPNextIdempotencyCapabilityError,
    ERPNextIdempotencyConflictError,
    parse_capability,
)
from event_identity import DEFAULT_DELIVERY_CONTRACT_VERSION, HEX64_RE


class ERPNextAdapterError(RuntimeError):
    """Base class for transport-independent ERPNext adapter failures."""


class ERPNextAdapterConfigurationError(ERPNextAdapterError, ValueError):
    """Raised when ERPNext transport configuration is incomplete or unsafe."""


class ERPNextAdapterContractError(ERPNextAdapterConfigurationError):
    """Raised when the live ERPNext idempotency capability is not approved."""


class ERPNextAdapterConflictError(ERPNextAdapterError):
    """Raised when a delivery ID is already bound to different immutable data."""


def _identifier(value, field):
    if not isinstance(value, str):
        raise ERPNextAdapterConfigurationError(f"{field} must be a string")
    text = value.strip().lower()
    if text != value or not HEX64_RE.fullmatch(text):
        raise ERPNextAdapterConfigurationError(
            f"{field} must be a lowercase 64-character SHA-256 identifier"
        )
    return text


def _metadata_text(value, field, *, max_chars=128):
    return _required_text(value, field, max_chars=max_chars)


@dataclass(frozen=True)
class EmployeeCheckinRequest:
    employee: str
    log_type: str
    event_time: str
    delivery_id: str = ""
    event_id: str = ""
    decision_id: str = ""
    camera_id: str = ""
    branch: str = ""
    delivery_contract_version: str = ""

    @property
    def idempotent(self):
        return bool(self.delivery_id)

    @classmethod
    def build(
        cls,
        employee,
        log_type,
        event_time=None,
        *,
        delivery_id="",
        event_id="",
        decision_id="",
        camera_id="",
        branch="",
        delivery_contract_version="",
    ):
        values = (
            delivery_id,
            event_id,
            decision_id,
            camera_id,
            branch,
            delivery_contract_version,
        )
        if any(value not in (None, "") for value in values):
            delivery_id = _identifier(delivery_id, "delivery_id")
            event_id = _identifier(event_id, "event_id")
            decision_id = _identifier(decision_id, "decision_id")
            camera_id = _metadata_text(camera_id, "camera_id", max_chars=128)
            branch = _metadata_text(branch, "branch", max_chars=128)
            delivery_contract_version = _metadata_text(
                delivery_contract_version or DEFAULT_DELIVERY_CONTRACT_VERSION,
                "delivery_contract_version",
                max_chars=128,
            )
        else:
            delivery_id = ""
            event_id = ""
            decision_id = ""
            camera_id = ""
            branch = ""
            delivery_contract_version = ""
        return cls(
            validate_employee_id(employee),
            validate_log_type(log_type),
            erp_event_time(event_time),
            delivery_id,
            event_id,
            decision_id,
            camera_id,
            branch,
            delivery_contract_version,
        )

    def idempotency_payload(self, capability):
        if not self.idempotent:
            raise ERPNextAdapterConfigurationError(
                "idempotency payload requires a delivery ID"
            )
        if not isinstance(capability, ERPNextIdempotencyCapability):
            raise ERPNextAdapterConfigurationError(
                "a verified ERPNext idempotency capability is required"
            )
        return {
            "delivery_id": self.delivery_id,
            "employee": self.employee,
            "log_type": self.log_type,
            "time": self.event_time,
            "delivery_contract_version": self.delivery_contract_version,
            "event_id": self.event_id,
            "decision_id": self.decision_id,
            "camera_id": self.camera_id,
            "branch": self.branch,
            "expected_site": capability.site,
            "expected_contract_version": capability.contract_version,
            "expected_fingerprint": capability.fingerprint,
        }


@dataclass(frozen=True)
class EmployeeCheckinResult:
    docname: str
    transport: str
    created: bool = True
    delivery_id: str = ""
    idempotency_verified: bool = False
    erpnext_site: str = ""
    idempotency_fingerprint: str = ""
    delivery_contract_version: str = ""


def erp_event_time(value=None):
    """Normalize an event time to ERPNext's UTC ``YYYY-mm-dd HH:MM:SS`` form."""

    if value in (None, ""):
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    if not isinstance(value, str):
        raise ERPNextAdapterConfigurationError(
            "event_time must be an RFC 3339 string"
        )
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ERPNextAdapterConfigurationError(
            "event_time must be an RFC 3339 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ERPNextAdapterConfigurationError(
            "event_time must include a timezone"
        )
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _required_text(value, field, *, max_chars=2048):
    if not isinstance(value, str):
        raise ERPNextAdapterConfigurationError(f"{field} must be a string")
    text = value.strip()
    if text != value or not text:
        raise ERPNextAdapterConfigurationError(
            f"{field} must be a non-empty trimmed string"
        )
    if len(text) > int(max_chars):
        raise ERPNextAdapterConfigurationError(
            f"{field} exceeds {int(max_chars)} characters"
        )
    return text


def _rest_base_url(value, *, allow_insecure=False):
    text = _required_text(value, "frappe_url", max_chars=2048).rstrip("/")
    parsed = urlsplit(text)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        raise ERPNextAdapterConfigurationError(
            "frappe_url must be an absolute HTTP(S) URL"
        )
    if parsed.username or parsed.password:
        raise ERPNextAdapterConfigurationError(
            "frappe_url must not contain embedded credentials"
        )
    if parsed.query or parsed.fragment:
        raise ERPNextAdapterConfigurationError(
            "frappe_url must not contain a query string or fragment"
        )
    if parsed.scheme != "https" and not bool(allow_insecure):
        raise ERPNextAdapterConfigurationError(
            "frappe_url must use HTTPS unless allow_insecure_frappe_url is true"
        )
    return text


def _idempotency_options(cfg):
    return {
        "idempotency_required": bool(
            cfg.get("erpnext_idempotency_required", False)
        ),
        "expected_site": str(cfg.get("erpnext_expected_site") or "").strip(),
        "expected_contract_version": str(
            cfg.get("erpnext_idempotency_contract_version")
            or IDEMPOTENCY_CONTRACT_VERSION
        ).strip(),
        "expected_fingerprint": str(
            cfg.get("erpnext_expected_idempotency_fingerprint") or ""
        ).strip().lower(),
        "idempotency_create_method": str(
            cfg.get("erpnext_idempotency_create_method")
            or DEFAULT_IDEMPOTENCY_CREATE_METHOD
        ).strip(),
        "idempotency_probe_method": str(
            cfg.get("erpnext_idempotency_probe_method")
            or DEFAULT_IDEMPOTENCY_PROBE_METHOD
        ).strip(),
    }


def select_erpnext_transport(cfg):
    """Return ``rest`` or ``bench`` using an explicit, validated policy."""

    if not isinstance(cfg, dict):
        raise ERPNextAdapterConfigurationError(
            "ERPNext configuration must be a mapping"
        )
    explicit = str(cfg.get("erpnext_transport") or "").strip().lower()
    if explicit == "api":
        explicit = "rest"
    if explicit and explicit not in {"rest", "bench"}:
        raise ERPNextAdapterConfigurationError(
            "erpnext_transport must be rest or bench"
        )

    rest_fields = (
        str(cfg.get("frappe_url") or "").strip(),
        str(cfg.get("frappe_api_key") or "").strip(),
        str(cfg.get("frappe_api_secret") or "").strip(),
    )
    if explicit == "rest":
        if not all(rest_fields):
            raise ERPNextAdapterConfigurationError(
                "rest transport requires frappe_url, frappe_api_key, "
                "and frappe_api_secret"
            )
        return "rest"
    if explicit == "bench":
        return "bench"
    return "rest" if all(rest_fields) else "bench"


class ERPNextAdapter(ABC):
    transport = ""

    @property
    def idempotency_verified(self):
        return False

    def verify_idempotency_contract(self, *, force=False):
        del force
        raise ERPNextAdapterContractError(
            "ERPNext adapter does not implement idempotency verification"
        )

    @abstractmethod
    def create_employee_checkin(
        self,
        request: EmployeeCheckinRequest,
        image_path=None,
    ) -> EmployeeCheckinResult:
        raise NotImplementedError


class _IdempotencyAdapterMixin:
    def _configure_idempotency(
        self,
        *,
        idempotency_required=False,
        expected_site="",
        expected_contract_version=IDEMPOTENCY_CONTRACT_VERSION,
        expected_fingerprint="",
        idempotency_create_method=DEFAULT_IDEMPOTENCY_CREATE_METHOD,
        idempotency_probe_method=DEFAULT_IDEMPOTENCY_PROBE_METHOD,
    ):
        self.idempotency_required = bool(idempotency_required)
        self.expected_site = str(expected_site or "").strip()
        self.expected_contract_version = str(
            expected_contract_version or IDEMPOTENCY_CONTRACT_VERSION
        ).strip()
        self.expected_fingerprint = str(expected_fingerprint or "").strip().lower()
        self.idempotency_create_method = _required_text(
            idempotency_create_method,
            "erpnext_idempotency_create_method",
            max_chars=256,
        )
        self.idempotency_probe_method = _required_text(
            idempotency_probe_method,
            "erpnext_idempotency_probe_method",
            max_chars=256,
        )
        self._idempotency_capability = None

    @property
    def idempotency_verified(self):
        return isinstance(
            self._idempotency_capability, ERPNextIdempotencyCapability
        )

    def _parse_capability(self, payload):
        try:
            capability = parse_capability(
                payload,
                expected_site=self.expected_site,
                expected_contract_version=self.expected_contract_version,
                expected_fingerprint=self.expected_fingerprint,
                expected_create_method=self.idempotency_create_method,
                expected_probe_method=self.idempotency_probe_method,
            )
        except ERPNextIdempotencyCapabilityError as exc:
            raise ERPNextAdapterContractError(str(exc)) from exc
        self._idempotency_capability = capability
        return capability

    def _validate_idempotent_result(self, payload, request, capability):
        if not isinstance(payload, dict):
            raise ERPNextAdapterError(
                "ERPNext idempotent response must be an object"
            )
        try:
            if payload.get("ok") is not True:
                raise ValueError("success response is missing ok=true")
            docname = validate_erp_docname(payload["name"])
            delivery_id = _identifier(payload["delivery_id"], "delivery_id")
            created = payload["created"]
            site = _required_text(payload["site"], "ERPNext response site", max_chars=255)
            contract = _required_text(
                payload["contract_version"],
                "ERPNext response contract version",
                max_chars=128,
            )
            fingerprint = _identifier(payload["fingerprint"], "fingerprint")
            delivery_contract = _required_text(
                payload["delivery_payload_contract_version"],
                "ERPNext response delivery payload contract",
                max_chars=128,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ERPNextAdapterError(
                "ERPNext idempotent response is incomplete or invalid"
            ) from exc
        if not isinstance(created, bool):
            raise ERPNextAdapterError(
                "ERPNext idempotent response created flag must be boolean"
            )
        if delivery_id != request.delivery_id:
            raise ERPNextAdapterConflictError(
                "ERPNext returned a different delivery ID"
            )
        if site != capability.site or contract != capability.contract_version:
            raise ERPNextAdapterContractError(
                "ERPNext response does not match the verified destination contract"
            )
        if fingerprint != capability.fingerprint:
            raise ERPNextAdapterContractError(
                "ERPNext response capability fingerprint changed during delivery"
            )
        if (
            delivery_contract != request.delivery_contract_version
            or delivery_contract
            != capability.delivery_payload_contract_version
        ):
            raise ERPNextAdapterContractError(
                "ERPNext response delivery payload contract changed during delivery"
            )
        return EmployeeCheckinResult(
            docname,
            self.transport,
            created=created,
            delivery_id=delivery_id,
            idempotency_verified=True,
            erpnext_site=site,
            idempotency_fingerprint=fingerprint,
            delivery_contract_version=delivery_contract,
        )

    @staticmethod
    def _raise_idempotent_error(payload):
        if not isinstance(payload, dict) or payload.get("ok") is not False:
            return
        code = str(payload.get("error_code") or "").strip()
        message = str(payload.get("message") or code or "ERPNext delivery failed")
        if code == "delivery_id_conflict":
            raise ERPNextAdapterConflictError(message)
        if code == "validation_error":
            raise ERPNextAdapterConfigurationError(message)
        raise ERPNextAdapterError(message)


class RESTERPNextAdapter(_IdempotencyAdapterMixin, ERPNextAdapter):
    transport = "rest"

    def __init__(
        self,
        *,
        base_url,
        api_key,
        api_secret,
        allow_insecure=False,
        session=None,
        timeout_seconds=30,
        **idempotency_options,
    ):
        self.base_url = _rest_base_url(
            base_url,
            allow_insecure=allow_insecure,
        )
        self.api_key = _required_text(api_key, "frappe_api_key", max_chars=512)
        self.api_secret = _required_text(
            api_secret,
            "frappe_api_secret",
            max_chars=2048,
        )
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or float(timeout_seconds) <= 0
            or float(timeout_seconds) > 300
        ):
            raise ERPNextAdapterConfigurationError(
                "ERPNext REST timeout must be between 0 and 300 seconds"
            )
        self.timeout_seconds = float(timeout_seconds)
        self.session = session or requests
        self._configure_idempotency(**idempotency_options)

    def _headers(self, *, json_request=True):
        headers = {
            "Authorization": f"token {self.api_key}:{self.api_secret}",
        }
        if json_request:
            headers["Content-Type"] = "application/json"
        return headers

    def _method_url(self, method):
        return f"{self.base_url}/api/method/{quote(method, safe='.')}"

    @staticmethod
    def _message(payload):
        if isinstance(payload, dict) and "message" in payload:
            return payload["message"]
        return payload

    def verify_idempotency_contract(self, *, force=False):
        if self.idempotency_verified and not force:
            return self._idempotency_capability
        response = self.session.post(
            self._method_url(self.idempotency_probe_method),
            headers=self._headers(),
            json={},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        try:
            payload = self._message(response.json())
        except (TypeError, ValueError) as exc:
            raise ERPNextAdapterContractError(
                "ERPNext idempotency probe did not return JSON"
            ) from exc
        return self._parse_capability(payload)

    def _upload_attachment(self, docname, image_path):
        image_path = Path(image_path)
        with image_path.open("rb") as handle:
            upload = self.session.post(
                f"{self.base_url}/api/method/upload_file",
                headers=self._headers(json_request=False),
                data={
                    "doctype": "Employee Checkin",
                    "docname": docname,
                    "is_private": "1",
                },
                files={
                    "file": (image_path.name, handle, "image/jpeg")
                },
                timeout=self.timeout_seconds,
            )
        upload.raise_for_status()

    def create_employee_checkin(self, request, image_path=None):
        if not isinstance(request, EmployeeCheckinRequest):
            raise ERPNextAdapterConfigurationError(
                "request must be an EmployeeCheckinRequest"
            )
        if request.idempotent or self.idempotency_required:
            if not request.idempotent:
                raise ERPNextAdapterConfigurationError(
                    "idempotent ERPNext delivery requires complete delivery metadata"
                )
            capability = self.verify_idempotency_contract()
            response = self.session.post(
                self._method_url(self.idempotency_create_method),
                headers=self._headers(),
                json=request.idempotency_payload(capability),
                timeout=self.timeout_seconds,
            )
            try:
                payload = self._message(response.json())
            except (TypeError, ValueError) as exc:
                response.raise_for_status()
                raise ERPNextAdapterError(
                    "ERPNext idempotent response did not return JSON"
                ) from exc
            self._raise_idempotent_error(payload)
            response.raise_for_status()
            result = self._validate_idempotent_result(
                payload, request, capability
            )
        else:
            response = self.session.post(
                f"{self.base_url}/api/resource/Employee%20Checkin",
                headers=self._headers(),
                json={
                    "employee": request.employee,
                    "log_type": request.log_type,
                    "time": request.event_time,
                },
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            try:
                payload = response.json()
                docname = validate_erp_docname(payload["data"]["name"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ERPNextAdapterError(
                    "ERPNext REST response did not contain a valid document name"
                ) from exc
            result = EmployeeCheckinResult(docname, self.transport)

        if image_path:
            self._upload_attachment(result.docname, image_path)
        return result


class BenchERPNextAdapter(_IdempotencyAdapterMixin, ERPNextAdapter):
    transport = "bench"

    def __init__(
        self,
        *,
        execute: Callable[[str, dict[str, Any]], Any],
        attach: Callable[[str, Path], Any] | None = None,
        attachment_error_handler: Callable[[Exception], Any] | None = None,
        **idempotency_options,
    ):
        if not callable(execute):
            raise ERPNextAdapterConfigurationError(
                "bench execute callback is required"
            )
        if attach is not None and not callable(attach):
            raise ERPNextAdapterConfigurationError(
                "bench attachment callback must be callable"
            )
        self.execute = execute
        self.attach = attach
        self.attachment_error_handler = attachment_error_handler
        self._configure_idempotency(**idempotency_options)

    def verify_idempotency_contract(self, *, force=False):
        if self.idempotency_verified and not force:
            return self._idempotency_capability
        payload = self.execute(self.idempotency_probe_method, {})
        return self._parse_capability(payload)

    def create_employee_checkin(self, request, image_path=None):
        if not isinstance(request, EmployeeCheckinRequest):
            raise ERPNextAdapterConfigurationError(
                "request must be an EmployeeCheckinRequest"
            )
        if request.idempotent or self.idempotency_required:
            if not request.idempotent:
                raise ERPNextAdapterConfigurationError(
                    "idempotent ERPNext delivery requires complete delivery metadata"
                )
            capability = self.verify_idempotency_contract()
            try:
                payload = self.execute(
                    self.idempotency_create_method,
                    request.idempotency_payload(capability),
                )
            except ERPNextIdempotencyConflictError as exc:
                raise ERPNextAdapterConflictError(str(exc)) from exc
            self._raise_idempotent_error(payload)
            result = self._validate_idempotent_result(
                payload, request, capability
            )
        else:
            inserted = self.execute(
                "frappe.client.insert",
                {
                    "doc": {
                        "doctype": "Employee Checkin",
                        "employee": request.employee,
                        "log_type": request.log_type,
                        "time": request.event_time,
                    }
                },
            )
            try:
                docname = validate_erp_docname(inserted["name"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ERPNextAdapterError(
                    "ERPNext bench response did not contain a valid document name"
                ) from exc
            result = EmployeeCheckinResult(docname, self.transport)

        if image_path and self.attach:
            try:
                self.attach(result.docname, Path(image_path))
            except Exception as exc:
                if self.attachment_error_handler is None:
                    raise
                self.attachment_error_handler(exc)
        return result


def build_erpnext_adapter(
    cfg,
    *,
    rest_session=None,
    bench_execute=None,
    bench_attach=None,
    bench_attachment_error_handler=None,
):
    transport = select_erpnext_transport(cfg)
    options = _idempotency_options(cfg)
    if transport == "rest":
        return RESTERPNextAdapter(
            base_url=cfg.get("frappe_url"),
            api_key=cfg.get("frappe_api_key"),
            api_secret=cfg.get("frappe_api_secret"),
            allow_insecure=bool(cfg.get("allow_insecure_frappe_url", False)),
            session=rest_session,
            timeout_seconds=cfg.get("erpnext_request_timeout_seconds", 30),
            **options,
        )
    return BenchERPNextAdapter(
        execute=bench_execute,
        attach=bench_attach,
        attachment_error_handler=bench_attachment_error_handler,
        **options,
    )
