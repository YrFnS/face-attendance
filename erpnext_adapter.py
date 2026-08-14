"""Explicit ERPNext delivery transport adapters.

Phase 2 keeps transport choice separate from recognition and queue state.  The
adapters in this module are intentionally stateless: durable retries,
idempotency, reconciliation, and dead-letter handling belong to the local
delivery outbox.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import requests

from data_contract import (
    validate_employee_id,
    validate_erp_docname,
    validate_log_type,
)


class ERPNextAdapterError(RuntimeError):
    """Base class for transport-independent ERPNext adapter failures."""


class ERPNextAdapterConfigurationError(ERPNextAdapterError, ValueError):
    """Raised when ERPNext transport configuration is incomplete or unsafe."""


@dataclass(frozen=True)
class EmployeeCheckinRequest:
    employee: str
    log_type: str
    event_time: str

    @classmethod
    def build(cls, employee, log_type, event_time=None):
        return cls(
            validate_employee_id(employee),
            validate_log_type(log_type),
            erp_event_time(event_time),
        )


@dataclass(frozen=True)
class EmployeeCheckinResult:
    docname: str
    transport: str


@dataclass(frozen=True)
class PrivateAttachmentResult:
    file_docname: str
    file_url: str
    transport: str


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


def select_erpnext_transport(cfg):
    """Return ``rest`` or ``bench`` using an explicit, validated policy.

    Existing installations without ``erpnext_transport`` retain the old
    compatibility selection: REST is chosen only when all three REST fields are
    configured; otherwise the local bench transport is selected.
    """

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

    @abstractmethod
    def create_employee_checkin(
        self,
        request: EmployeeCheckinRequest,
        image_path=None,
    ) -> EmployeeCheckinResult:
        raise NotImplementedError

    @abstractmethod
    def attach_private_file(
        self,
        docname: str,
        image_path,
    ) -> PrivateAttachmentResult:
        raise NotImplementedError


class RESTERPNextAdapter(ERPNextAdapter):
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

    def _headers(self, *, json_request=True):
        headers = {
            "Authorization": f"token {self.api_key}:{self.api_secret}",
        }
        if json_request:
            headers["Content-Type"] = "application/json"
        return headers

    def create_employee_checkin(self, request, image_path=None):
        if not isinstance(request, EmployeeCheckinRequest):
            raise ERPNextAdapterConfigurationError(
                "request must be an EmployeeCheckinRequest"
            )
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

        if image_path is not None:
            raise ERPNextAdapterConfigurationError(
                "private attachments must be delivered through attach_private_file"
            )
        return EmployeeCheckinResult(docname, self.transport)

    def attach_private_file(self, docname, image_path):
        docname = validate_erp_docname(docname)
        image_path = Path(image_path)
        if image_path.is_symlink() or not image_path.is_file():
            raise ERPNextAdapterConfigurationError(
                "attachment source must be a regular non-symbolic-link file"
            )
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
                    "file": (
                        image_path.name,
                        handle,
                        "image/jpeg",
                    )
                },
                timeout=self.timeout_seconds,
            )
        upload.raise_for_status()
        try:
            payload = upload.json()
            message = payload.get("message") or {}
            if not isinstance(message, dict):
                raise TypeError("message must be an object")
            file_docname = str(message.get("name") or "").strip()
            file_url = str(message.get("file_url") or "").strip()
            if not file_docname:
                raise ValueError("missing File document name")
            file_docname = validate_erp_docname(file_docname)
            if len(file_url) > 2048:
                raise ValueError("file_url is too long")
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise ERPNextAdapterError(
                "ERPNext attachment response did not contain a valid File document"
            ) from exc
        return PrivateAttachmentResult(file_docname, file_url, self.transport)


class BenchERPNextAdapter(ERPNextAdapter):
    transport = "bench"

    def __init__(
        self,
        *,
        execute: Callable[[str, dict[str, Any]], Any],
        attach: Callable[[str, Path], Any] | None = None,
        attachment_error_handler: Callable[[Exception], Any] | None = None,
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

    def create_employee_checkin(self, request, image_path=None):
        if not isinstance(request, EmployeeCheckinRequest):
            raise ERPNextAdapterConfigurationError(
                "request must be an EmployeeCheckinRequest"
            )
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

        if image_path is not None:
            raise ERPNextAdapterConfigurationError(
                "private attachments must be delivered through attach_private_file"
            )
        return EmployeeCheckinResult(docname, self.transport)

    def attach_private_file(self, docname, image_path):
        docname = validate_erp_docname(docname)
        if self.attach is None:
            raise ERPNextAdapterConfigurationError(
                "bench attachment callback is required"
            )
        image_path = Path(image_path)
        if image_path.is_symlink() or not image_path.is_file():
            raise ERPNextAdapterConfigurationError(
                "attachment source must be a regular non-symbolic-link file"
            )
        result = self.attach(docname, image_path)
        file_docname = ""
        file_url = ""
        if isinstance(result, dict):
            file_docname = str(result.get("name") or "").strip()
            file_url = str(result.get("file_url") or "").strip()
        elif isinstance(result, str):
            file_docname = result.strip()
        elif result is not None:
            raise ERPNextAdapterError(
                "bench attachment callback returned an invalid result"
            )
        if file_docname:
            file_docname = validate_erp_docname(file_docname)
        if len(file_url) > 2048:
            raise ERPNextAdapterError("bench attachment file_url is too long")
        return PrivateAttachmentResult(file_docname, file_url, self.transport)


def build_erpnext_adapter(
    cfg,
    *,
    rest_session=None,
    bench_execute=None,
    bench_attach=None,
    bench_attachment_error_handler=None,
):
    transport = select_erpnext_transport(cfg)
    if transport == "rest":
        return RESTERPNextAdapter(
            base_url=cfg.get("frappe_url"),
            api_key=cfg.get("frappe_api_key"),
            api_secret=cfg.get("frappe_api_secret"),
            allow_insecure=bool(cfg.get("allow_insecure_frappe_url", False)),
            session=rest_session,
            timeout_seconds=cfg.get("erpnext_request_timeout_seconds", 30),
        )
    return BenchERPNextAdapter(
        execute=bench_execute,
        attach=bench_attach,
        attachment_error_handler=bench_attachment_error_handler,
    )
