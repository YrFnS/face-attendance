"""Strict data contracts for gallery metadata and employee identifiers.

The helpers in this module are deliberately dependency-free so the same rules
can be applied before filesystem, URL, log, synchronization, and ERPNext use.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote


class GalleryError(ValueError):
    """Raised when gallery or employee data violates the runtime contract."""


MAX_EMPLOYEE_ID_CHARS = 128
MAX_EMPLOYEE_ID_BYTES = 180
MAX_EMPLOYEE_NAME_CHARS = 256
MAX_EMPLOYEE_NAME_BYTES = 1024
MAX_GALLERY_TEXT_CHARS = 256
MAX_GALLERY_TEXT_BYTES = 1024
MAX_GALLERY_TOKEN_CHARS = 128
MAX_URL_CHARS = 2048
MAX_HEADER_CHARS = 512
MAX_EMBEDDING_DIMENSION = 4096
MAX_GALLERY_EMPLOYEES = 100_000
MAX_EMBEDDINGS_PER_EMPLOYEE = 1_000
MAX_TOTAL_EMBEDDINGS = 500_000
MAX_EMBEDDING_ABS_VALUE = 1_000_000.0
MAX_RELEASE_SEQUENCE = (1 << 63) - 1
EMPLOYEE_STORAGE_PREFIX = "e~"

_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+~-]*$")
_SAFE_EMPLOYEE_COMPONENT_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,127}$"
)
_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_BIDI_CONTROL_NAMES = {
    "LEFT-TO-RIGHT EMBEDDING",
    "RIGHT-TO-LEFT EMBEDDING",
    "POP DIRECTIONAL FORMATTING",
    "LEFT-TO-RIGHT OVERRIDE",
    "RIGHT-TO-LEFT OVERRIDE",
    "LEFT-TO-RIGHT ISOLATE",
    "RIGHT-TO-LEFT ISOLATE",
    "FIRST STRONG ISOLATE",
    "POP DIRECTIONAL ISOLATE",
    "LEFT-TO-RIGHT MARK",
    "RIGHT-TO-LEFT MARK",
    "ARABIC LETTER MARK",
}


def _unsafe_character(character: str) -> bool:
    category = unicodedata.category(character)
    if category.startswith("C"):
        return True
    return unicodedata.name(character, "") in _BIDI_CONTROL_NAMES


def _normalize_text(
    value,
    field: str,
    *,
    required: bool,
    max_chars: int,
    max_bytes: int,
    trim: bool = False,
) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise GalleryError(f"{field} must be a string")
    text = unicodedata.normalize("NFC", value)
    if trim:
        text = text.strip()
    elif text != text.strip():
        raise GalleryError(f"{field} must not have leading or trailing whitespace")
    if required and not text:
        raise GalleryError(f"{field} is required")
    if not text:
        return ""
    if len(text) > int(max_chars):
        raise GalleryError(f"{field} exceeds {int(max_chars)} characters")
    encoded = text.encode("utf-8")
    if len(encoded) > int(max_bytes):
        raise GalleryError(f"{field} exceeds {int(max_bytes)} UTF-8 bytes")
    for character in text:
        if _unsafe_character(character):
            raise GalleryError(f"{field} contains a control or formatting character")
    return text


def validate_display_text(
    value,
    field: str,
    *,
    required: bool = False,
    max_chars: int = MAX_GALLERY_TEXT_CHARS,
    max_bytes: int = MAX_GALLERY_TEXT_BYTES,
) -> str:
    return _normalize_text(
        value,
        field,
        required=required,
        max_chars=max_chars,
        max_bytes=max_bytes,
    )


def validate_token(
    value,
    field: str,
    *,
    required: bool = False,
    max_chars: int = MAX_GALLERY_TOKEN_CHARS,
) -> str:
    text = _normalize_text(
        value,
        field,
        required=required,
        max_chars=max_chars,
        max_bytes=max_chars,
    )
    if text and not _TOKEN_RE.fullmatch(text):
        raise GalleryError(
            f"{field} may contain only ASCII letters, digits, '.', '_', ':', "
            "'@', '+', '~', and '-'"
        )
    return text


def validate_gallery_label(
    value,
    field: str,
    *,
    required: bool = False,
    max_chars: int = 128,
) -> str:
    text = _normalize_text(
        value,
        field,
        required=required,
        max_chars=max_chars,
        max_bytes=max_chars * 4,
    )
    if any(character in text for character in "\r\n\x00"):
        raise GalleryError(f"{field} contains an unsafe character")
    return text


def validate_employee_id(value, field: str = "employee") -> str:
    text = _normalize_text(
        value,
        field,
        required=True,
        max_chars=MAX_EMPLOYEE_ID_CHARS,
        max_bytes=MAX_EMPLOYEE_ID_BYTES,
    )
    if text in {".", ".."}:
        raise GalleryError(f"{field} is not a valid employee identifier")
    if not text[0].isalnum():
        raise GalleryError(f"{field} must start with a Unicode letter or digit")
    for character in text:
        category = unicodedata.category(character)
        if not (
            character.isalnum()
            or category.startswith("M")
            or character in "._@-"
        ):
            raise GalleryError(
                f"{field} contains an unsupported character {character!r}"
            )
    return text


def validate_employee_name(value, field: str = "employee_name") -> str:
    return validate_display_text(
        value,
        field,
        required=False,
        max_chars=MAX_EMPLOYEE_NAME_CHARS,
        max_bytes=MAX_EMPLOYEE_NAME_BYTES,
    )


def _raw_employee_component_allowed(employee: str) -> bool:
    if employee.startswith(EMPLOYEE_STORAGE_PREFIX):
        return False
    if not _SAFE_EMPLOYEE_COMPONENT_RE.fullmatch(employee):
        return False
    stem = employee.split(".", 1)[0].upper()
    if stem in _WINDOWS_RESERVED:
        return False
    return len(employee.encode("ascii")) <= 128


def employee_storage_component(employee) -> str:
    employee = validate_employee_id(employee)
    if _raw_employee_component_allowed(employee):
        return employee
    encoded = base64.urlsafe_b64encode(employee.encode("utf-8")).decode("ascii")
    component = EMPLOYEE_STORAGE_PREFIX + encoded.rstrip("=")
    if len(component.encode("ascii")) > 240:
        raise GalleryError("encoded employee directory name exceeds filesystem limits")
    return component


def employee_id_from_storage_component(component) -> str:
    component = _normalize_text(
        component,
        "employee directory",
        required=True,
        max_chars=240,
        max_bytes=240,
    )
    if "/" in component or "\\" in component:
        raise GalleryError("employee directory must be one path component")
    if component.startswith(EMPLOYEE_STORAGE_PREFIX):
        encoded = component[len(EMPLOYEE_STORAGE_PREFIX) :]
        decoded = decode_base64url(
            encoded,
            field="employee directory",
            max_decoded_bytes=MAX_EMPLOYEE_ID_BYTES,
        )
        try:
            employee = decoded.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GalleryError("employee directory is not valid UTF-8") from exc
        employee = validate_employee_id(employee)
        if employee_storage_component(employee) != component:
            raise GalleryError("employee directory is not canonically encoded")
        return employee
    employee = validate_employee_id(component)
    if employee_storage_component(employee) != component:
        raise GalleryError("employee directory must use canonical encoding")
    return employee


def employee_directory(root, employee) -> Path:
    root = Path(root).expanduser().resolve()
    component = employee_storage_component(employee)
    candidate = root / component
    if candidate.parent != root:
        raise GalleryError("employee directory escapes the configured root")
    if candidate.exists() and candidate.is_symlink():
        raise GalleryError("employee directory must not be a symbolic link")
    return candidate


def employee_filename_token(employee) -> str:
    employee = validate_employee_id(employee)
    if _raw_employee_component_allowed(employee) and len(employee) <= 64:
        return employee
    digest = hashlib.sha256(employee.encode("utf-8")).hexdigest()[:24]
    return f"employee-{digest}"


def filename_token(value, field: str = "filename token", max_chars: int = 48) -> str:
    text = _normalize_text(
        value,
        field,
        required=True,
        max_chars=max_chars,
        max_bytes=max_chars,
    )
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", text):
        raise GalleryError(
            f"{field} may contain only ASCII letters, digits, '.', '_', and '-'"
        )
    if text in {".", ".."} or text.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
        raise GalleryError(f"{field} is reserved")
    return text


def safe_log_message(value, *, max_chars: int = 4000) -> str:
    limit = max(1, int(max_chars))
    text = unicodedata.normalize("NFC", str(value))
    output = []
    length = 0
    for character in text:
        piece = (
            f"\\u{ord(character):04x}"
            if _unsafe_character(character) or character in "\r\n"
            else character
        )
        if length + len(piece) > limit:
            if length < limit:
                output.append("…"[: limit - length])
            break
        output.append(piece)
        length += len(piece)
    return "".join(output)


def safe_log_value(value, *, max_chars: int = 256) -> str:
    return safe_log_message(value, max_chars=max_chars)


def validate_log_type(value, field: str = "log_type") -> str:
    if not isinstance(value, str) or value != value.strip():
        raise GalleryError(f"{field} must be exactly IN or OUT")
    normalized = value.upper()
    if normalized not in {"IN", "OUT"}:
        raise GalleryError(f"{field} must be exactly IN or OUT")
    return normalized


def validate_erp_docname(value, field: str = "ERPNext document name") -> str:
    return _normalize_text(
        value,
        field,
        required=True,
        max_chars=140,
        max_bytes=560,
    )


def strict_int(value, field: str, *, minimum=None, maximum=None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise GalleryError(f"{field} must be an integer")
    if minimum is not None and value < int(minimum):
        raise GalleryError(f"{field} must be at least {int(minimum)}")
    if maximum is not None and value > int(maximum):
        raise GalleryError(f"{field} must be at most {int(maximum)}")
    return value


def strict_number(value, field: str, *, maximum_abs=MAX_EMBEDDING_ABS_VALUE) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GalleryError(f"{field} must be a JSON number")
    number = float(value)
    if not math.isfinite(number):
        raise GalleryError(f"{field} must be finite")
    if abs(number) > float(maximum_abs):
        raise GalleryError(
            f"{field} exceeds the absolute limit {float(maximum_abs):g}"
        )
    return number


def validate_embedding_vector(vector, *, field: str, dimension: int) -> list[float]:
    if not isinstance(vector, list):
        raise GalleryError(f"{field} must be a JSON array")
    if len(vector) != int(dimension):
        raise GalleryError(
            f"{field} dimension mismatch: received {len(vector)}, expected {int(dimension)}"
        )
    return [
        strict_number(value, f"{field}[{index}]")
        for index, value in enumerate(vector)
    ]


def validate_base64url_text(
    value,
    *,
    field: str,
    expected_decoded_bytes: int | None = None,
    max_chars: int = 4096,
) -> str:
    text = _normalize_text(
        value,
        field,
        required=True,
        max_chars=max_chars,
        max_bytes=max_chars,
    )
    if "=" in text or not _B64URL_RE.fullmatch(text):
        raise GalleryError(f"{field} must be unpadded URL-safe base64")
    decoded = decode_base64url(
        text,
        field=field,
        expected_decoded_bytes=expected_decoded_bytes,
    )
    canonical = base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=")
    if canonical != text:
        raise GalleryError(f"{field} must use canonical URL-safe base64")
    return text


def decode_base64url(
    value,
    *,
    field: str,
    expected_decoded_bytes: int | None = None,
    max_decoded_bytes: int = 4096,
) -> bytes:
    if not isinstance(value, str) or not value or not _B64URL_RE.fullmatch(value):
        raise GalleryError(f"{field} must be unpadded URL-safe base64")
    try:
        decoded = base64.b64decode(
            value.encode("ascii") + b"=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, binascii.Error) as exc:
        raise GalleryError(f"{field} is not valid base64") from exc
    if len(decoded) > int(max_decoded_bytes):
        raise GalleryError(f"{field} decodes to too many bytes")
    if expected_decoded_bytes is not None and len(decoded) != int(
        expected_decoded_bytes
    ):
        raise GalleryError(
            f"{field} must decode to {int(expected_decoded_bytes)} bytes"
        )
    return decoded


def validate_checksum(value, field: str = "checksum") -> str:
    text = _normalize_text(
        value,
        field,
        required=True,
        max_chars=64,
        max_bytes=64,
    )
    if not _HEX64_RE.fullmatch(text):
        raise GalleryError(f"{field} must be 64 lowercase hexadecimal characters")
    return text


def canonical_timestamp(value, field: str = "generated_at") -> str:
    text = _normalize_text(
        value,
        field,
        required=True,
        max_chars=40,
        max_bytes=40,
    )
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise GalleryError(f"{field} must be a valid RFC 3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GalleryError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def validate_url_text(value, field: str = "URL") -> str:
    return _normalize_text(
        value,
        field,
        required=True,
        max_chars=MAX_URL_CHARS,
        max_bytes=MAX_URL_CHARS,
    )


def validate_url_path(value, field: str = "URL path") -> str:
    path = _normalize_text(
        value,
        field,
        required=True,
        max_chars=512,
        max_bytes=512,
    )
    if not path.startswith("/"):
        raise GalleryError(f"{field} must start with '/'")
    if "#" in path or "?" in path or "\\" in path:
        raise GalleryError(
            f"{field} must not contain a query, fragment, or backslash"
        )
    if any(segment in {".", ".."} for segment in path.split("/")):
        raise GalleryError(f"{field} must not contain dot path segments")
    return path


def validate_header_value(value, field: str = "HTTP header") -> str:
    return _normalize_text(
        value,
        field,
        required=False,
        max_chars=MAX_HEADER_CHARS,
        max_bytes=MAX_HEADER_CHARS,
    )


def encode_query_value(value, field: str, *, max_chars: int = 128) -> str:
    text = validate_gallery_label(value, field, required=True, max_chars=max_chars)
    return quote(text, safe="")


def bounded_limit(value, field: str, default: int, hard_maximum: int) -> int:
    if value is None:
        value = default
    return strict_int(value, field, minimum=1, maximum=hard_maximum)



def strict_json_loads(data, *, field: str = "JSON"):
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise GalleryError(f"{field} contains duplicate key {key!r}")
            result[key] = value
        return result

    def reject_constant(value):
        raise GalleryError(f"{field} contains non-finite number {value}")

    try:
        return json.loads(
            data,
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
    except GalleryError:
        raise
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GalleryError(f"{field} is not valid JSON: {exc}") from exc

def compact_json(value, *, max_chars: int = 4000) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return safe_log_message(text, max_chars=max_chars)
