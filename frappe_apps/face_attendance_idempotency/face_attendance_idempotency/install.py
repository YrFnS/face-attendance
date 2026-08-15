"""Install and verify the Employee Checkin delivery-ID schema."""

from __future__ import annotations

from .contract import (
    BRANCH_FIELD,
    CAMERA_FIELD,
    DECISION_FIELD,
    DELIVERY_CONTRACT_FIELD,
    DELIVERY_FIELD,
    DOCTYPE,
    EVENT_FIELD,
    UNIQUE_CONSTRAINT,
)


CUSTOM_FIELDS = {
    DOCTYPE: [
        {
            "fieldname": DELIVERY_FIELD,
            "label": "Face Attendance Delivery ID",
            "fieldtype": "Data",
            "hidden": 1,
            "read_only": 1,
            "no_copy": 1,
            "search_index": 1,
            "description": "Server-enforced idempotency key from face-attendance",
        },
        {
            "fieldname": EVENT_FIELD,
            "label": "Face Attendance Event ID",
            "fieldtype": "Data",
            "hidden": 1,
            "read_only": 1,
            "no_copy": 1,
            "search_index": 1,
        },
        {
            "fieldname": DECISION_FIELD,
            "label": "Face Attendance Decision ID",
            "fieldtype": "Data",
            "hidden": 1,
            "read_only": 1,
            "no_copy": 1,
            "search_index": 1,
        },
        {
            "fieldname": DELIVERY_CONTRACT_FIELD,
            "label": "Face Attendance Delivery Contract",
            "fieldtype": "Data",
            "hidden": 1,
            "read_only": 1,
            "no_copy": 1,
        },
        {
            "fieldname": CAMERA_FIELD,
            "label": "Face Attendance Camera ID",
            "fieldtype": "Data",
            "hidden": 1,
            "read_only": 1,
            "no_copy": 1,
        },
        {
            "fieldname": BRANCH_FIELD,
            "label": "Face Attendance Branch",
            "fieldtype": "Data",
            "hidden": 1,
            "read_only": 1,
            "no_copy": 1,
        },
    ]
}


def _frappe():
    import frappe

    return frappe


def _duplicate_delivery_ids(frappe):
    return frappe.db.sql(
        f"""
        SELECT `{DELIVERY_FIELD}` AS delivery_id, COUNT(*) AS duplicate_count
        FROM `tab{DOCTYPE}`
        WHERE `{DELIVERY_FIELD}` IS NOT NULL
          AND `{DELIVERY_FIELD}` <> ''
        GROUP BY `{DELIVERY_FIELD}`
        HAVING COUNT(*) > 1
        LIMIT 20
        """,
        as_dict=True,
    )


def _normalize_empty_delivery_ids(frappe):
    frappe.db.sql(
        f"""
        UPDATE `tab{DOCTYPE}`
        SET `{DELIVERY_FIELD}` = NULL
        WHERE `{DELIVERY_FIELD}` = ''
        """
    )


def normalize_employee_checkin_delivery_id(doc, method=None):
    del method
    if not getattr(doc, DELIVERY_FIELD, None):
        setattr(doc, DELIVERY_FIELD, None)


def unique_constraint_columns(frappe=None):
    frappe = frappe or _frappe()
    db_type = str(getattr(frappe.db, "db_type", "") or "").lower()
    table = f"tab{DOCTYPE}"
    if db_type in {"mariadb", "mysql"}:
        rows = frappe.db.sql(
            """
            SELECT CONSTRAINT_NAME AS constraint_name,
                   COLUMN_NAME AS column_name,
                   ORDINAL_POSITION AS ordinal_position
            FROM information_schema.KEY_COLUMN_USAGE
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = %s
              AND CONSTRAINT_NAME = %s
            ORDER BY ORDINAL_POSITION
            """,
            (table, UNIQUE_CONSTRAINT),
            as_dict=True,
        )
    elif db_type in {"postgres", "postgresql"}:
        rows = frappe.db.sql(
            """
            SELECT tc.constraint_name AS constraint_name,
                   kcu.column_name AS column_name,
                   kcu.ordinal_position AS ordinal_position
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            WHERE tc.table_schema = current_schema()
              AND tc.table_name = %s
              AND tc.constraint_type = 'UNIQUE'
              AND tc.constraint_name = %s
            ORDER BY kcu.ordinal_position
            """,
            (table, UNIQUE_CONSTRAINT),
            as_dict=True,
        )
    else:
        return []
    return [str(row["column_name"]) for row in rows]


def verify_schema(frappe=None):
    frappe = frappe or _frappe()
    if not frappe.db.has_column(DOCTYPE, DELIVERY_FIELD):
        raise RuntimeError(
            f"{DOCTYPE}.{DELIVERY_FIELD} database column is missing"
        )
    columns = unique_constraint_columns(frappe)
    if columns != [DELIVERY_FIELD]:
        raise RuntimeError(
            "Employee Checkin delivery ID unique constraint is missing or invalid"
        )
    return columns


def ensure_schema():
    frappe = _frappe()
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

    create_custom_fields(CUSTOM_FIELDS, update=True)
    frappe.clear_cache(doctype=DOCTYPE)
    _normalize_empty_delivery_ids(frappe)
    duplicates = _duplicate_delivery_ids(frappe)
    if duplicates:
        values = ", ".join(str(row["delivery_id"]) for row in duplicates[:5])
        frappe.throw(
            "Cannot install face-attendance idempotency because duplicate "
            f"delivery IDs already exist: {values}"
        )
    frappe.db.add_unique(
        DOCTYPE,
        DELIVERY_FIELD,
        constraint_name=UNIQUE_CONSTRAINT,
    )
    verify_schema(frappe)


def after_install():
    ensure_schema()


def after_migrate():
    ensure_schema()
