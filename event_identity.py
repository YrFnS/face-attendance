"""Versioned identifier semantics for camera captures and ERPNext delivery.

The functions in this module deliberately preserve the identifiers already
written by the Phase 1 event ledger.  The contract makes the boundaries
explicit before Phase 2 introduces durable delivery jobs:

* ``source_sha256`` is the digest of the exact uploaded bytes.
* ``capture_id`` fingerprints one observed camera upload using source metadata.
* ``event_id`` remains the existing camera/direction/content-scoped local key.
* ``decision_id`` identifies one face decision in one processing generation.
* ``delivery_id`` is a stable, domain-separated idempotency key per decision.

A delivery retry must reuse the same delivery ID.  A new recognition decision
must receive a different decision ID and therefore a different delivery ID.
"""

import hashlib
import math
import re
import unicodedata


IDENTITY_CONTRACT_VERSION = "face-attendance-identifiers/v1"
CONTENT_HASH_ALGORITHM = "sha256-exact-upload-bytes"
CAPTURE_ID_ALGORITHM = (
    "sha256(camera_id NUL source_sha256 NUL source_name NUL "
    "source_size NUL source_mtime_6dp)"
)
EVENT_ID_ALGORITHM = "sha256(camera_id NUL direction NUL source_sha256)"
DECISION_ID_ALGORITHM = "sha256(event_id NUL face_index NUL decision_version)"
DELIVERY_ID_ALGORITHM = (
    "sha256(face-attendance/delivery/v1 NUL recognition_decision_id)"
)
DELIVERY_ID_DOMAIN = "face-attendance/delivery/v1"
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


class EventIdentityError(ValueError):
    pass


def _component(value, field, *, max_chars):
    if value is None:
        raise EventIdentityError(f"{field} is required")
    text = str(value)
    if not text:
        raise EventIdentityError(f"{field} is required")
    if len(text) > int(max_chars):
        raise EventIdentityError(f"{field} exceeds {int(max_chars)} characters")
    for character in text:
        if character == "\x00" or unicodedata.category(character) in {
            "Cc",
            "Cf",
            "Cs",
        }:
            raise EventIdentityError(
                f"{field} contains a control or formatting character"
            )
    return text


def _positive_integer(value, field, *, maximum):
    if isinstance(value, bool) or not isinstance(value, int):
        raise EventIdentityError(f"{field} must be an integer")
    if value < 1 or value > int(maximum):
        raise EventIdentityError(
            f"{field} must be between 1 and {int(maximum)}"
        )
    return value


def _sha256_identifier(value, field):
    text = _component(value, field, max_chars=64).lower()
    if not HEX64_RE.fullmatch(text):
        raise EventIdentityError(
            f"{field} must be a lowercase 64-character SHA-256 identifier"
        )
    return text


def make_event_id(camera_id, direction, source_sha256):
    """Return the existing local event key.

    This intentionally remains content-scoped for compatibility.  It is not the
    future ERPNext delivery idempotency key and must not be used as one.
    """

    camera_id = _component(camera_id, "camera_id", max_chars=128)
    direction = _component(direction, "direction", max_chars=16)
    if direction not in {"IN", "OUT"}:
        raise EventIdentityError("direction must be IN or OUT")
    source_sha256 = _component(
        source_sha256, "source_sha256", max_chars=128
    )
    payload = "\0".join((camera_id, direction, source_sha256))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def make_capture_id(
    camera_id,
    source_sha256,
    source_name,
    source_size,
    source_mtime,
):
    """Fingerprint one observed upload without replacing the content hash."""

    camera_id = _component(camera_id, "camera_id", max_chars=128)
    source_sha256 = _component(
        source_sha256, "source_sha256", max_chars=128
    )
    source_name = _component(source_name, "source_name", max_chars=1024)
    if isinstance(source_size, bool):
        raise EventIdentityError("source_size must be an integer")
    try:
        source_size = int(source_size)
    except (TypeError, ValueError) as exc:
        raise EventIdentityError("source_size must be an integer") from exc
    if source_size < 0:
        raise EventIdentityError("source_size must not be negative")
    if isinstance(source_mtime, bool):
        raise EventIdentityError("source_mtime must be numeric")
    try:
        source_mtime = float(source_mtime)
    except (TypeError, ValueError) as exc:
        raise EventIdentityError("source_mtime must be numeric") from exc
    if not math.isfinite(source_mtime) or source_mtime < 0:
        raise EventIdentityError(
            "source_mtime must be a finite non-negative number"
        )
    payload = "\0".join(
        (
            camera_id,
            source_sha256,
            source_name,
            str(source_size),
            f"{source_mtime:.6f}",
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def make_recognition_decision_id(event_id, face_index, decision_version=1):
    event_id = _sha256_identifier(event_id, "event_id")
    face_index = _positive_integer(
        face_index, "face_index", maximum=1_000_000
    )
    decision_version = _positive_integer(
        decision_version,
        "decision_version",
        maximum=1_000_000,
    )
    payload = "\0".join(
        (event_id, str(face_index), str(decision_version))
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def make_delivery_id(decision_id):
    """Return the stable ERPNext idempotency key for one decision."""

    decision_id = _sha256_identifier(decision_id, "decision_id")
    payload = "\0".join((DELIVERY_ID_DOMAIN, decision_id))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def identity_contract():
    return {
        "version": IDENTITY_CONTRACT_VERSION,
        "content_hash": {
            "field": "source_sha256",
            "algorithm": CONTENT_HASH_ALGORITHM,
            "scope": "exact uploaded bytes across every camera",
            "erpnext_idempotency_key": False,
        },
        "capture_id": {
            "algorithm": CAPTURE_ID_ALGORITHM,
            "scope": "one observed upload fingerprint",
            "erpnext_idempotency_key": False,
        },
        "event_id": {
            "algorithm": EVENT_ID_ALGORITHM,
            "scope": "local camera/direction/content compatibility key",
            "erpnext_idempotency_key": False,
        },
        "recognition_decision_id": {
            "algorithm": DECISION_ID_ALGORITHM,
            "scope": "one face in one processing generation",
            "erpnext_idempotency_key": False,
        },
        "delivery_id": {
            "algorithm": DELIVERY_ID_ALGORITHM,
            "scope": "one immutable recognition decision across all retries",
            "erpnext_idempotency_key": True,
        },
    }
