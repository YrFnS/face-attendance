"""Versioned identifier semantics and long-lived replay tombstones.

The raw image SHA-256 is content evidence, not an event, capture, decision, or
remote-delivery identifier. Every derived identifier is domain-separated and
versioned so that equal input strings cannot be mistaken for another ID type.
"""

import hashlib
import json
import math
import re
import unicodedata


HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

CONTENT_HASH_ALGORITHM = "sha256"
LEGACY_CONTENT_HASH_ALGORITHM = "legacy-source-key-v1"
CAPTURE_ID_SCHEME = "face-attendance-capture-v2"
EVENT_ID_SCHEME = "face-attendance-event-v2"
DECISION_ID_SCHEME = "face-attendance-decision-v2"
DELIVERY_ID_SCHEME = "face-attendance-delivery-v1"
DEFAULT_DELIVERY_CONTRACT_VERSION = "erpnext-employee-checkin-v1"

LEGACY_CAPTURE_ID_SCHEME = "legacy-capture-v1"
LEGACY_EVENT_ID_SCHEME = "legacy-event-v1"
LEGACY_DECISION_ID_SCHEME = "legacy-decision-v1"


class EventIdentityError(ValueError):
    pass


def _text(value, field, *, required=True, max_chars=1024):
    if not isinstance(value, str):
        raise EventIdentityError(f"{field} must be a string")
    text = unicodedata.normalize("NFC", value)
    if text != text.strip():
        raise EventIdentityError(f"{field} must not contain surrounding whitespace")
    if required and not text:
        raise EventIdentityError(f"{field} is required")
    if len(text) > int(max_chars):
        raise EventIdentityError(f"{field} exceeds {int(max_chars)} characters")
    for character in text:
        if unicodedata.category(character) in {"Cc", "Cf", "Cs"}:
            raise EventIdentityError(
                f"{field} contains a control or formatting character"
            )
    return text


def _identifier(value, field):
    text = _text(value, field, max_chars=64).lower()
    if not HEX64_RE.fullmatch(text):
        raise EventIdentityError(
            f"{field} must be a lowercase 64-character SHA-256 identifier"
        )
    return text


def _nonnegative_int(value, field):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EventIdentityError(f"{field} must be a non-negative integer")
    return value


def _nonnegative_float(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EventIdentityError(f"{field} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise EventIdentityError(f"{field} must be finite and non-negative")
    return result


def _domain_hash(scheme, parts):
    scheme = _text(scheme, "identifier scheme", max_chars=128)
    payload = json.dumps(
        {"scheme": scheme, "parts": list(parts)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def content_sha256(data):
    """Return SHA-256 for the exact uploaded bytes, without decoding or resizing."""

    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise EventIdentityError("content bytes must be bytes-like")
    return hashlib.sha256(bytes(data)).hexdigest()


def normalize_content_sha256(value, *, allow_legacy=False):
    text = _text(value, "content_sha256", max_chars=128).lower()
    if not allow_legacy and not HEX64_RE.fullmatch(text):
        raise EventIdentityError(
            "content_sha256 must be a lowercase 64-character SHA-256 digest"
        )
    return text


def make_capture_id(
    camera_id,
    source_sha256,
    source_name,
    source_size,
    source_mtime,
):
    """Identify one local capture envelope, not the image content itself.

    The envelope combines the bound camera, raw content hash, immutable basename,
    size, and a microsecond-normalized source timestamp. It is useful for audit
    and capture-level idempotency, but is not proof that a physical camera made a
    new observation; the independently authenticated source receipt remains the
    source-attribution boundary.
    """

    camera_id = _text(camera_id, "camera_id", max_chars=128)
    source_sha256 = normalize_content_sha256(source_sha256, allow_legacy=True)
    source_name = _text(source_name, "source_name", max_chars=1024)
    source_size = _nonnegative_int(source_size, "source_size")
    source_mtime_us = int(
        round(_nonnegative_float(source_mtime, "source_mtime") * 1_000_000)
    )
    return _domain_hash(
        CAPTURE_ID_SCHEME,
        (camera_id, source_sha256, source_name, source_size, source_mtime_us),
    )


def make_event_id(camera_id, direction, source_sha256):
    """Return the camera-scoped content idempotency key for one direction."""

    camera_id = _text(camera_id, "camera_id", max_chars=128)
    direction = _text(direction, "direction", max_chars=16).upper()
    if direction not in {"IN", "OUT"}:
        raise EventIdentityError("direction must be IN or OUT")
    source_sha256 = normalize_content_sha256(source_sha256, allow_legacy=True)
    return _domain_hash(EVENT_ID_SCHEME, (camera_id, direction, source_sha256))


def make_recognition_decision_id(event_id, face_index, decision_version=1):
    """Identify one face decision in one numbered processing attempt."""

    event_id = _identifier(event_id, "event_id")
    face_index = _nonnegative_int(face_index, "face_index")
    decision_version = _nonnegative_int(decision_version, "decision_version")
    if face_index < 1:
        raise EventIdentityError("face_index must be at least 1")
    if decision_version < 1 or decision_version > 1_000_000:
        raise EventIdentityError(
            "decision_version must be between 1 and 1000000"
        )
    return _domain_hash(
        DECISION_ID_SCHEME,
        (event_id, face_index, decision_version),
    )


def make_delivery_id(
    decision_id,
    delivery_contract_version=DEFAULT_DELIVERY_CONTRACT_VERSION,
):
    """Identify one future ERPNext delivery for exactly one accepted decision.

    Multiple accepted faces from one capture share capture/event context but get
    different decision IDs and therefore different delivery IDs.
    """

    decision_id = _identifier(decision_id, "decision_id")
    contract = _text(
        delivery_contract_version,
        "delivery_contract_version",
        max_chars=128,
    )
    return _domain_hash(DELIVERY_ID_SCHEME, (contract, decision_id))


def identifier_semantics():
    return {
        "content_hash": {
            "algorithm": CONTENT_HASH_ALGORITHM,
            "meaning": "SHA-256 of the exact uploaded image bytes",
            "security_boundary": "content evidence and replay comparison",
        },
        "capture_id": {
            "scheme": CAPTURE_ID_SCHEME,
            "meaning": "one local camera upload envelope",
            "security_boundary": "audit identity; not physical-camera proof",
        },
        "event_id": {
            "scheme": EVENT_ID_SCHEME,
            "meaning": "camera and direction scoped content idempotency key",
            "security_boundary": "detailed event replay prevention",
        },
        "decision_id": {
            "scheme": DECISION_ID_SCHEME,
            "meaning": "one face index in one numbered event processing attempt",
            "security_boundary": "immutable recognition evidence",
        },
        "delivery_id": {
            "scheme": DELIVERY_ID_SCHEME,
            "contract_version": DEFAULT_DELIVERY_CONTRACT_VERSION,
            "meaning": "one ERPNext delivery for one accepted recognition decision",
            "security_boundary": "future server-enforced delivery idempotency",
        },
    }



IDENTITY_SCHEMA_STATEMENTS = (
    f"ALTER TABLE camera_events ADD COLUMN event_id_scheme TEXT NOT NULL DEFAULT '{LEGACY_EVENT_ID_SCHEME}'",
    f"ALTER TABLE camera_events ADD COLUMN capture_id_scheme TEXT NOT NULL DEFAULT '{LEGACY_CAPTURE_ID_SCHEME}'",
    f"ALTER TABLE camera_events ADD COLUMN content_hash_algorithm TEXT NOT NULL DEFAULT '{LEGACY_CONTENT_HASH_ALGORITHM}'",
    f"ALTER TABLE recognition_decisions ADD COLUMN decision_id_scheme TEXT NOT NULL DEFAULT '{LEGACY_DECISION_ID_SCHEME}'",
    "ALTER TABLE recognition_decisions ADD COLUMN delivery_id TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE recognition_decisions ADD COLUMN delivery_id_scheme TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE recognition_decisions ADD COLUMN delivery_contract_version TEXT NOT NULL DEFAULT ''",
    """
    CREATE TABLE event_tombstones (
        event_id TEXT PRIMARY KEY,
        event_id_scheme TEXT NOT NULL,
        capture_id TEXT NOT NULL,
        capture_id_scheme TEXT NOT NULL,
        camera_id TEXT NOT NULL,
        log_type TEXT NOT NULL CHECK(log_type IN ('IN', 'OUT')),
        source_sha256 TEXT NOT NULL,
        content_hash_algorithm TEXT NOT NULL,
        first_received_at TEXT NOT NULL,
        first_received_unix REAL NOT NULL
    )
    """,
    """
    INSERT INTO event_tombstones (
        event_id, event_id_scheme, capture_id, capture_id_scheme,
        camera_id, log_type, source_sha256, content_hash_algorithm,
        first_received_at, first_received_unix
    )
    SELECT
        event_id,
        event_id_scheme,
        CASE WHEN capture_id = '' THEN event_id ELSE capture_id END,
        capture_id_scheme,
        camera_id,
        log_type,
        source_sha256,
        content_hash_algorithm,
        received_at,
        received_unix
    FROM camera_events
    """,
    "CREATE UNIQUE INDEX event_tombstones_camera_hash ON event_tombstones(camera_id, source_sha256)",
    "CREATE UNIQUE INDEX event_tombstones_capture_id ON event_tombstones(capture_id)",
    "CREATE INDEX event_tombstones_first_received ON event_tombstones(first_received_unix)",
    """
    CREATE UNIQUE INDEX recognition_decisions_delivery_id
    ON recognition_decisions(delivery_id)
    WHERE delivery_id <> ''
    """,
    """
    CREATE TRIGGER event_tombstones_no_update
    BEFORE UPDATE ON event_tombstones
    BEGIN
        SELECT RAISE(ABORT, 'event tombstones are immutable');
    END
    """,
    """
    CREATE TRIGGER event_tombstones_no_delete
    BEFORE DELETE ON event_tombstones
    BEGIN
        SELECT RAISE(ABORT, 'event tombstones are permanent replay records');
    END
    """,
)


IDENTITY_REQUIRED_TABLE_COLUMNS = {
    "camera_events": {
        "event_id_scheme": ("TEXT", True, 0),
        "capture_id_scheme": ("TEXT", True, 0),
        "content_hash_algorithm": ("TEXT", True, 0),
    },
    "recognition_decisions": {
        "decision_id_scheme": ("TEXT", True, 0),
        "delivery_id": ("TEXT", True, 0),
        "delivery_id_scheme": ("TEXT", True, 0),
        "delivery_contract_version": ("TEXT", True, 0),
    },
    "event_tombstones": {
        "event_id": ("TEXT", False, 1),
        "event_id_scheme": ("TEXT", True, 0),
        "capture_id": ("TEXT", True, 0),
        "capture_id_scheme": ("TEXT", True, 0),
        "camera_id": ("TEXT", True, 0),
        "log_type": ("TEXT", True, 0),
        "source_sha256": ("TEXT", True, 0),
        "content_hash_algorithm": ("TEXT", True, 0),
        "first_received_at": ("TEXT", True, 0),
        "first_received_unix": ("REAL", True, 0),
    },
}

IDENTITY_REQUIRED_INDEXES = {
    "event_tombstones_camera_hash": (
        True,
        ("camera_id", "source_sha256"),
    ),
    "event_tombstones_capture_id": (True, ("capture_id",)),
    "event_tombstones_first_received": (False, ("first_received_unix",)),
    "recognition_decisions_delivery_id": (True, ("delivery_id",)),
}

IDENTITY_REQUIRED_TRIGGERS = frozenset(
    {
        "event_tombstones_no_update",
        "event_tombstones_no_delete",
    }
)
