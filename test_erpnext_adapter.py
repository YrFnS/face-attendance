import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import requests

if "insightface.app" not in sys.modules:
    insightface = types.ModuleType("insightface")
    insightface_app = types.ModuleType("insightface.app")
    insightface_app.FaceAnalysis = object
    insightface.app = insightface_app
    sys.modules["insightface"] = insightface
    sys.modules["insightface.app"] = insightface_app

import face_attendance

from erpnext_adapter import (
    BenchERPNextAdapter,
    EmployeeCheckinRequest,
    ERPNextAdapterConfigurationError,
    RESTERPNextAdapter,
    build_erpnext_adapter,
    erp_event_time,
    select_erpnext_transport,
)


class FakeResponse:
    def __init__(self, payload=None, error=None):
        self.payload = payload or {}
        self.error = error

    def raise_for_status(self):
        if self.error:
            raise self.error

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class ERPNextAdapterTests(unittest.TestCase):
    def test_transport_selection_is_explicit_and_backward_compatible(self):
        self.assertEqual(
            select_erpnext_transport({"erpnext_transport": "bench"}),
            "bench",
        )
        self.assertEqual(
            select_erpnext_transport(
                {
                    "erpnext_transport": "rest",
                    "frappe_url": "https://erp.example.com",
                    "frappe_api_key": "key",
                    "frappe_api_secret": "secret",
                }
            ),
            "rest",
        )
        with self.assertRaisesRegex(
            ERPNextAdapterConfigurationError,
            "rest transport requires",
        ):
            select_erpnext_transport(
                {
                    "erpnext_transport": "rest",
                    "frappe_url": "https://erp.example.com",
                }
            )
        self.assertEqual(
            select_erpnext_transport(
                {
                    "frappe_url": "https://erp.example.com",
                    "frappe_api_key": "key",
                    "frappe_api_secret": "secret",
                }
            ),
            "rest",
        )
        self.assertEqual(
            select_erpnext_transport(
                {
                    "frappe_url": "https://erp.example.com",
                    "frappe_api_key": "",
                    "frappe_api_secret": "secret",
                }
            ),
            "bench",
        )

    def test_event_time_is_normalized_to_utc(self):
        self.assertEqual(
            erp_event_time("2026-08-14T03:30:45+03:00"),
            "2026-08-14 00:30:45",
        )
        with self.assertRaisesRegex(
            ERPNextAdapterConfigurationError,
            "timezone",
        ):
            erp_event_time("2026-08-14T00:30:45")

    def test_rest_adapter_creates_checkin_and_private_attachment(self):
        session = FakeSession(
            [
                FakeResponse({"data": {"name": "CHK-0001"}}),
                FakeResponse({"message": {"name": "FILE-0001"}}),
            ]
        )
        adapter = RESTERPNextAdapter(
            base_url="https://erp.example.com",
            api_key="api-key",
            api_secret="api-secret",
            session=session,
            timeout_seconds=12,
        )
        request = EmployeeCheckinRequest.build(
            "HR-0001",
            "IN",
            "2026-08-14T00:00:00Z",
        )
        with tempfile.TemporaryDirectory() as temp:
            image = Path(temp) / "checkin.jpg"
            image.write_bytes(b"jpeg")
            result = adapter.create_employee_checkin(request)
            attachment = adapter.attach_private_file(result.docname, image)

        self.assertEqual(result.docname, "CHK-0001")
        self.assertEqual(attachment.file_docname, "FILE-0001")
        self.assertEqual(result.transport, "rest")
        self.assertEqual(len(session.calls), 2)
        create_url, create = session.calls[0]
        self.assertTrue(create_url.endswith("/api/resource/Employee%20Checkin"))
        self.assertEqual(
            create["json"],
            {
                "employee": "HR-0001",
                "log_type": "IN",
                "time": "2026-08-14 00:00:00",
            },
        )
        self.assertEqual(create["timeout"], 12.0)
        self.assertEqual(
            create["headers"]["Authorization"],
            "token api-key:api-secret",
        )
        upload_url, upload = session.calls[1]
        self.assertTrue(upload_url.endswith("/api/method/upload_file"))
        self.assertEqual(upload["data"]["docname"], "CHK-0001")
        self.assertEqual(upload["data"]["is_private"], "1")

    def test_rest_adapter_rejects_insecure_url_by_default(self):
        with self.assertRaisesRegex(
            ERPNextAdapterConfigurationError,
            "must use HTTPS",
        ):
            RESTERPNextAdapter(
                base_url="http://erp.example.com",
                api_key="api-key",
                api_secret="api-secret",
            )

    def test_bench_adapter_is_independently_injectable(self):
        calls = []
        attachments = []

        def execute(method, kwargs):
            calls.append((method, kwargs))
            return {"name": "CHK-BENCH-1"}

        adapter = BenchERPNextAdapter(
            execute=execute,
            attach=lambda docname, path: attachments.append(
                (docname, path.name)
            ),
        )
        request = EmployeeCheckinRequest.build(
            "HR-0002",
            "OUT",
            "2026-08-14T01:00:00Z",
        )
        with tempfile.TemporaryDirectory() as temp:
            image = Path(temp) / "out.jpg"
            image.write_bytes(b"jpeg")
            result = adapter.create_employee_checkin(request)
            attachment = adapter.attach_private_file(result.docname, image)

        self.assertEqual(result.docname, "CHK-BENCH-1")
        self.assertEqual(result.transport, "bench")
        self.assertEqual(calls[0][0], "frappe.client.insert")
        self.assertEqual(
            calls[0][1]["doc"],
            {
                "doctype": "Employee Checkin",
                "employee": "HR-0002",
                "log_type": "OUT",
                "time": "2026-08-14 01:00:00",
            },
        )
        self.assertEqual(attachments, [("CHK-BENCH-1", "out.jpg")])

    def test_synchronous_attachment_failure_does_not_fail_created_checkin(self):
        class Adapter:
            def create_employee_checkin(self, request, image_path=None):
                self.request = request
                return type(
                    "Result",
                    (),
                    {"docname": "CHK-SAFE-1", "transport": "rest", "created": True},
                )()

            def attach_private_file(self, docname, image_path):
                raise requests.ReadTimeout("attachment response lost")

        with tempfile.TemporaryDirectory() as temp:
            image = Path(temp) / "crop.jpg"
            image.write_bytes(b"jpeg")
            with (
                mock.patch.object(
                    face_attendance,
                    "load_config",
                    return_value={
                        "frappe_url": "https://erp.example.com",
                        "frappe_api_key": "key",
                        "frappe_api_secret": "secret",
                    },
                ),
                mock.patch.object(
                    face_attendance,
                    "build_erpnext_adapter",
                    return_value=Adapter(),
                ),
                mock.patch.object(face_attendance, "log") as logger,
            ):
                docname = face_attendance.create_checkin_api(
                    "HR-0001",
                    "IN",
                    image,
                    "2026-08-14T00:00:00Z",
                )
        self.assertEqual(docname, "CHK-SAFE-1")
        self.assertTrue(
            any(
                "attachment failed after delivery" in str(call)
                for call in logger.call_args_list
            )
        )

    def test_factory_requires_bench_callback(self):
        with self.assertRaisesRegex(
            ERPNextAdapterConfigurationError,
            "bench execute callback",
        ):
            build_erpnext_adapter({"erpnext_transport": "bench"})


if __name__ == "__main__":
    unittest.main()
