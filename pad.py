import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import urlparse

import cv2
import requests


PLACEHOLDERS = {"", "CHANGE_ME", "REPLACE_ME", "CHANGEME", "TODO"}
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+:-]{0,127}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


class PADConfigError(ValueError):
    pass


@dataclass(frozen=True)
class PADResult:
    passed: bool
    score: float | None
    provider: str
    reason: str = ""
    evidence_id: str = ""
    model: str = ""
    binding_id: str = ""
    crop_sha256: str = ""
    face_index: int = 0
    face_count: int = 0
    skipped: bool = False


def _text(value):
    return str(value or "").strip()


def _is_placeholder(value):
    return _text(value).upper() in PLACEHOLDERS


def _is_local_url(parsed):
    return parsed.hostname in {"localhost", "127.0.0.1", "::1"}


def _metadata_text(value, field, *, required=False, max_chars=256, pattern=None):
    if value is None:
        text = ""
    elif not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    else:
        text = unicodedata.normalize("NFC", value)
    if text != text.strip():
        raise ValueError(f"{field} must not contain surrounding whitespace")
    if required and not text:
        raise ValueError(f"{field} is required")
    if len(text) > int(max_chars):
        raise ValueError(f"{field} exceeds {int(max_chars)} characters")
    for character in text:
        if unicodedata.category(character) in {"Cc", "Cf", "Cs"}:
            raise ValueError(f"{field} contains a control or formatting character")
    if text and pattern is not None and not pattern.fullmatch(text):
        raise ValueError(f"{field} has an invalid format")
    return text


def _configured_models(cfg):
    value = cfg.get("pad_allowed_models", [])
    if value in (None, ""):
        return ()
    if not isinstance(value, list):
        raise ValueError("pad_allowed_models must be a JSON array")
    models = []
    seen = set()
    for index, item in enumerate(value):
        model = _metadata_text(
            item,
            f"pad_allowed_models[{index}]",
            required=True,
            max_chars=128,
            pattern=IDENTIFIER_RE,
        )
        if _is_placeholder(model):
            raise ValueError(f"pad_allowed_models[{index}] must not be a placeholder")
        if model in seen:
            raise ValueError(f"duplicate PAD model in allowlist: {model}")
        seen.add(model)
        models.append(model)
    return tuple(models)


def _strict_positive_int(value, field, *, default, maximum):
    if value is None:
        value = default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    if value < 1 or value > int(maximum):
        raise ValueError(f"{field} must be between 1 and {int(maximum)}")
    return value


def _read_limited_json(response, max_bytes):
    media_type = _text(response.headers.get("Content-Type")).lower().split(";", 1)[0]
    if media_type != "application/json" and not media_type.endswith("+json"):
        raise ValueError(
            f"PAD service must return application/json, received {media_type or '<missing>'}"
        )
    max_bytes = max(1024, int(max_bytes))
    length = _text(response.headers.get("Content-Length"))
    if length:
        try:
            if int(length) > max_bytes:
                raise ValueError(f"PAD response exceeds {max_bytes} bytes")
        except ValueError as exc:
            if "exceeds" in str(exc):
                raise
            raise ValueError("PAD service returned an invalid Content-Length") from exc
    chunks = []
    total = 0
    iterator = response.iter_content(chunk_size=16 * 1024)
    for chunk in iterator:
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(f"PAD response exceeds {max_bytes} bytes")
        chunks.append(chunk)
    try:
        payload = json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"PAD service returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("PAD response must be a JSON object")
    return payload


def configuration_issues(cfg):
    issues = []
    production = bool(cfg.get("production_mode", False))
    provider = _text(cfg.get("pad_provider") or "disabled").lower()
    required = bool(cfg.get("pad_required", False))
    fail_closed = bool(cfg.get("pad_fail_closed", True))
    if provider not in {"disabled", "http"}:
        issues.append(f"unsupported pad_provider: {provider}")
        return issues
    if required and provider == "disabled":
        issues.append("pad_required is true but pad_provider is disabled")
    if required and not fail_closed:
        issues.append("pad_required requires pad_fail_closed=true")
    if not isinstance(cfg.get("pad_require_single_face", True), bool):
        issues.append("pad_require_single_face must be a boolean")
    try:
        _strict_positive_int(
            cfg.get("pad_max_faces_per_event", 8),
            "pad_max_faces_per_event",
            default=8,
            maximum=32,
        )
    except ValueError as exc:
        issues.append(str(exc))
    try:
        score = float(cfg.get("pad_min_score", 0.8))
        if not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError
    except (TypeError, ValueError):
        issues.append("pad_min_score must be a finite value between 0 and 1")

    expected_provider = _text(cfg.get("pad_expected_provider"))
    if expected_provider:
        try:
            _metadata_text(
                expected_provider,
                "pad_expected_provider",
                required=True,
                max_chars=128,
                pattern=IDENTIFIER_RE,
            )
        except ValueError as exc:
            issues.append(str(exc))
    try:
        allowed_models = _configured_models(cfg)
    except ValueError as exc:
        allowed_models = ()
        issues.append(str(exc))

    if production:
        if _is_placeholder(expected_provider):
            issues.append(
                "pad_expected_provider must pin the approved PAD provider in production"
            )
        if not allowed_models:
            issues.append(
                "pad_allowed_models must allowlist at least one approved PAD model in production"
            )
        if not bool(cfg.get("pad_require_binding_echo", False)):
            issues.append("pad_require_binding_echo must be true in production")
        if not bool(cfg.get("pad_require_evidence_id", False)):
            issues.append("pad_require_evidence_id must be true in production")

    if provider == "http":
        url = _text(cfg.get("pad_http_url"))
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            issues.append("pad_http_url must be an absolute HTTP(S) URL")
        elif (
            parsed.scheme != "https"
            and not _is_local_url(parsed)
            and not bool(cfg.get("pad_allow_insecure_url", False))
        ):
            issues.append("pad_http_url must use HTTPS unless it is local or explicitly allowed")
        token = _text(cfg.get("pad_http_token"))
        allow_unauthenticated_local = bool(cfg.get("pad_allow_unauthenticated_local", False))
        if _is_placeholder(token) and not (
            parsed.scheme in {"http", "https"}
            and _is_local_url(parsed)
            and allow_unauthenticated_local
        ):
            issues.append("pad_http_token must be configured")
    return issues


def _context_int(context, field, default):
    value = context.get(field, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"PAD context {field} must be a positive integer")
    return value


def _context_bbox(context):
    value = context.get("bbox")
    if value in (None, []):
        return []
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError("PAD context bbox must contain four numbers")
    result = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(float(item)):
            raise ValueError("PAD context bbox must contain four finite numbers")
        result.append(int(round(float(item))))
    return result


def _binding_context(image_bytes, context):
    if context is None:
        context = {}
    if not isinstance(context, dict):
        raise ValueError("PAD context must be a JSON object")
    face_index = _context_int(context, "face_index", 1)
    face_count = _context_int(context, "face_count", 1)
    if face_index > face_count:
        raise ValueError("PAD context face_index exceeds face_count")
    crop_sha256 = hashlib.sha256(image_bytes).hexdigest()
    descriptor = {
        "event_id": _text(context.get("event_id")),
        "camera_id": _text(context.get("camera_id")),
        "log_type": _text(context.get("log_type")),
        "source_sha256": _text(context.get("source_sha256")),
        "face_index": face_index,
        "face_count": face_count,
        "bbox": _context_bbox(context),
        "crop_sha256": crop_sha256,
    }
    encoded = json.dumps(
        descriptor,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    binding_id = hashlib.sha256(encoded).hexdigest()
    request_context = dict(context)
    request_context.update(
        face_binding_id=binding_id,
        face_crop_sha256=crop_sha256,
        face_index=face_index,
        face_count=face_count,
        bbox=descriptor["bbox"],
    )
    return request_context, binding_id, crop_sha256, face_index, face_count


class PADGate:
    def __init__(self, cfg, session=requests):
        self.cfg = cfg
        self.session = session
        self.provider = _text(cfg.get("pad_provider") or "disabled").lower()
        self.required = bool(cfg.get("pad_required", False))
        self.fail_closed = bool(cfg.get("pad_fail_closed", True))
        self.min_score = float(cfg.get("pad_min_score", 0.8))
        self.production = bool(cfg.get("production_mode", False))
        self.expected_provider = _text(cfg.get("pad_expected_provider"))
        self.require_binding_echo = self.production or bool(
            cfg.get("pad_require_binding_echo", False)
        )
        self.require_evidence_id = self.production or bool(
            cfg.get("pad_require_evidence_id", False)
        )
        issues = configuration_issues(cfg)
        if issues:
            raise PADConfigError("; ".join(issues))
        self.allowed_models = _configured_models(cfg)
        self.max_faces = _strict_positive_int(
            cfg.get("pad_max_faces_per_event", 8),
            "pad_max_faces_per_event",
            default=8,
            maximum=32,
        )

    @property
    def enabled(self):
        return self.provider != "disabled"

    def _headers(self):
        headers = {"Accept": "application/json", "User-Agent": "face-attendance-pad/2"}
        token = _text(self.cfg.get("pad_http_token"))
        if not _is_placeholder(token):
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _error_result(
        self,
        reason,
        *,
        binding_id="",
        crop_sha256="",
        face_index=0,
        face_count=0,
    ):
        provider = self.expected_provider or self.provider
        if self.required or self.fail_closed:
            return PADResult(
                False,
                None,
                provider,
                reason=reason,
                binding_id=binding_id,
                crop_sha256=crop_sha256,
                face_index=face_index,
                face_count=face_count,
            )
        return PADResult(
            True,
            None,
            provider,
            reason=f"fail_open:{reason}",
            binding_id=binding_id,
            crop_sha256=crop_sha256,
            face_index=face_index,
            face_count=face_count,
            skipped=True,
        )

    def evaluate(self, face_image, context=None):
        if not self.enabled:
            if self.required:
                return PADResult(False, None, "disabled", reason="PAD is required")
            return PADResult(True, None, "disabled", reason="PAD disabled", skipped=True)
        if face_image is None or getattr(face_image, "size", 0) == 0:
            return self._error_result("empty face crop")

        success, encoded = cv2.imencode(
            ".jpg",
            face_image,
            [cv2.IMWRITE_JPEG_QUALITY, int(self.cfg.get("pad_jpeg_quality", 92))],
        )
        if not success:
            return self._error_result("could not encode face crop")
        image_bytes = encoded.tobytes()
        max_image_bytes = max(1024, int(self.cfg.get("pad_max_image_bytes", 2 * 1024 * 1024)))
        if len(image_bytes) > max_image_bytes:
            return self._error_result(f"PAD crop exceeds {max_image_bytes} bytes")

        try:
            (
                request_context,
                binding_id,
                crop_sha256,
                face_index,
                face_count,
            ) = _binding_context(image_bytes, context)
        except ValueError as exc:
            return self._error_result(f"invalid_context:{exc}")

        response = None
        try:
            response = self.session.post(
                _text(self.cfg.get("pad_http_url")),
                headers=self._headers(),
                data={
                    "context": json.dumps(
                        request_context,
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                },
                files={"image": ("face.jpg", image_bytes, "image/jpeg")},
                timeout=(
                    max(0.25, float(self.cfg.get("pad_connect_timeout_seconds", 2))),
                    max(0.5, float(self.cfg.get("pad_read_timeout_seconds", 5))),
                ),
                stream=True,
            )
            response.raise_for_status()
            payload = _read_limited_json(
                response, int(self.cfg.get("pad_max_response_bytes", 64 * 1024))
            )
            live_value = payload.get("live")
            if live_value is None:
                live_value = payload.get("passed")
            if live_value is None:
                live_value = payload.get("is_live")
            if not isinstance(live_value, bool):
                raise ValueError("PAD response must contain a boolean live/passed/is_live field")
            if "score" not in payload:
                raise ValueError("PAD response must contain an explicit score field")
            score = float(payload["score"])
            if not math.isfinite(score) or not 0 <= score <= 1:
                raise ValueError("PAD score must be finite and between 0 and 1")

            provider_id = _metadata_text(
                payload.get("provider") if "provider" in payload else payload.get("provider_id"),
                "PAD response provider",
                required=bool(self.expected_provider),
                max_chars=128,
                pattern=IDENTIFIER_RE,
            )
            if self.expected_provider and provider_id != self.expected_provider:
                raise ValueError(
                    f"PAD provider {provider_id or '<missing>'!r} is not the pinned provider"
                )
            model = _metadata_text(
                payload.get("model") if "model" in payload else payload.get("model_version"),
                "PAD response model",
                required=bool(self.allowed_models),
                max_chars=128,
                pattern=IDENTIFIER_RE,
            )
            if self.allowed_models and model not in self.allowed_models:
                raise ValueError(
                    f"PAD model {model or '<missing>'!r} is not in pad_allowed_models"
                )
            evidence_id = _metadata_text(
                payload.get("evidence_id"),
                "PAD response evidence_id",
                required=self.require_evidence_id,
                max_chars=256,
                pattern=IDENTIFIER_RE,
            )
            response_binding = _metadata_text(
                payload.get("face_binding_id")
                if "face_binding_id" in payload
                else payload.get("binding_id"),
                "PAD response face_binding_id",
                required=self.require_binding_echo,
                max_chars=64,
                pattern=HEX64_RE,
            )
            if response_binding and response_binding != binding_id:
                raise ValueError("PAD response face_binding_id does not match the evaluated face")

            passed = bool(live_value) and score >= self.min_score
            reason = _metadata_text(
                payload.get("reason"),
                "PAD response reason",
                required=False,
                max_chars=256,
            )
            if not passed and not reason:
                reason = "presentation_attack" if not live_value else "score_below_threshold"
            return PADResult(
                passed,
                score,
                provider_id or self.expected_provider or self.provider,
                reason=reason,
                evidence_id=evidence_id,
                model=model,
                binding_id=binding_id,
                crop_sha256=crop_sha256,
                face_index=face_index,
                face_count=face_count,
            )
        except (requests.RequestException, ValueError, TypeError) as exc:
            return self._error_result(
                f"provider_error:{exc}",
                binding_id=binding_id,
                crop_sha256=crop_sha256,
                face_index=face_index,
                face_count=face_count,
            )
        finally:
            if response is not None:
                response.close()
