import json
import math
from dataclasses import dataclass
from urllib.parse import urlparse

import cv2
import requests


PLACEHOLDERS = {"", "CHANGE_ME", "REPLACE_ME", "CHANGEME", "TODO"}


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
    skipped: bool = False


def _text(value):
    return str(value or "").strip()


def _is_placeholder(value):
    return _text(value).upper() in PLACEHOLDERS


def _is_local_url(parsed):
    return parsed.hostname in {"localhost", "127.0.0.1", "::1"}


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
    try:
        score = float(cfg.get("pad_min_score", 0.8))
        if not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError
    except (TypeError, ValueError):
        issues.append("pad_min_score must be a finite value between 0 and 1")
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


class PADGate:
    def __init__(self, cfg, session=requests):
        self.cfg = cfg
        self.session = session
        self.provider = _text(cfg.get("pad_provider") or "disabled").lower()
        self.required = bool(cfg.get("pad_required", False))
        self.fail_closed = bool(cfg.get("pad_fail_closed", True))
        self.min_score = float(cfg.get("pad_min_score", 0.8))
        issues = configuration_issues(cfg)
        if issues:
            raise PADConfigError("; ".join(issues))

    @property
    def enabled(self):
        return self.provider != "disabled"

    def _headers(self):
        headers = {"Accept": "application/json", "User-Agent": "face-attendance-pad/1"}
        token = _text(self.cfg.get("pad_http_token"))
        if not _is_placeholder(token):
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _error_result(self, reason):
        if self.required or self.fail_closed:
            return PADResult(False, None, self.provider, reason=reason)
        return PADResult(
            True,
            None,
            self.provider,
            reason=f"fail_open:{reason}",
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

        response = None
        try:
            response = self.session.post(
                _text(self.cfg.get("pad_http_url")),
                headers=self._headers(),
                data={"context": json.dumps(context or {}, ensure_ascii=False, sort_keys=True)},
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
            passed = bool(live_value) and score >= self.min_score
            reason = _text(payload.get("reason"))
            if not passed and not reason:
                reason = "presentation_attack" if not live_value else "score_below_threshold"
            return PADResult(
                passed,
                score,
                self.provider,
                reason=reason,
                evidence_id=_text(payload.get("evidence_id")),
                model=_text(payload.get("model")),
            )
        except (requests.RequestException, ValueError, TypeError) as exc:
            return self._error_result(f"provider_error:{exc}")
        finally:
            if response is not None:
                response.close()
