# Face Attendance Idempotency

This small Frappe/ERPNext v15 companion app provides the server-enforced
`face_attendance_delivery_id` contract required by the face-attendance edge
worker. It installs trace fields on **Employee Checkin**, creates a database
unique constraint for the delivery ID, exposes an authenticated capability
probe, and implements atomic create-or-get behavior.

Installation and verification are documented in the parent repository at
`docs/erpnext-idempotency.md`.
