from . import __version__ as app_version

app_name = "face_attendance_idempotency"
app_title = "Face Attendance Idempotency"
app_publisher = "Face Attendance maintainers"
app_description = "Atomic Employee Checkin delivery idempotency bridge"
app_email = ""
app_license = "MIT"

required_apps = ["erpnext"]

after_install = "face_attendance_idempotency.install.after_install"
after_migrate = "face_attendance_idempotency.install.after_migrate"

doc_events = {
    "Employee Checkin": {
        "before_validate": (
            "face_attendance_idempotency.install."
            "normalize_employee_checkin_delivery_id"
        )
    }
}
