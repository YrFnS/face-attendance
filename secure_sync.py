import json
import random
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

from embedding_gallery import (
    GalleryError,
    load_gallery,
    read_sync_status,
    validate_gallery,
    write_gallery_atomic,
    write_sync_status,
)
from gallery_release import (
    record_acceptance,
    release_policy_issues,
    release_scope,
    scope_state,
    scoped_etag,
    validate_installed_release,
    validate_release,
)
from runtime_policy import (
    effective_gallery_options,
    enforce_gallery_freshness,
)


DEFAULT_ENDPOINT = "/api/faces/embeddings"
PLACEHOLDER_TOKENS = {"", "CHANGE_ME", "REPLACE_ME", "CHANGEME"}
REDIRECT_STATUSES = {301, 302, 303, 307, 308}


def utc_now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _text(value):
    return str(value or "").strip()


def _local_url(parsed):
    return parsed.hostname in {"localhost", "127.0.0.1", "::1"}


def _effective_port(parsed):
    if parsed.port is not None:
        return parsed.port
    return 443 if parsed.scheme == "https" else 80


def _origin(parsed):
    return parsed.scheme, (parsed.hostname or "").lower(), _effective_port(parsed)


def _validate_http_url(url, cfg, *, field):
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise GalleryError(f"{field} must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password:
        raise GalleryError(f"{field} must not contain embedded credentials")
    if (
        parsed.scheme != "https"
        and not _local_url(parsed)
        and not bool(cfg.get("allow_insecure_central_url", False))
    ):
        raise GalleryError(
            f"{field} must use HTTPS; allow insecure HTTP only on a trusted VPN/LAN"
        )
    return parsed


def _request_headers(cfg, release_state, *, conditional=True):
    token = _text(cfg.get("central_api_token"))
    if token.upper() in PLACEHOLDER_TOKENS:
        token = ""
    if not token and not bool(cfg.get("allow_unauthenticated_embedding_sync", False)):
        raise GalleryError(
            "central_api_token must be configured with a non-placeholder value"
        )
    headers = {
        "Accept": "application/json",
        "User-Agent": "face-attendance-embedding-sync/3",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    etag = _text(release_state.get("etag"))
    if conditional and etag:
        headers["If-None-Match"] = etag
    return headers


def _validate_source(cfg):
    central_url = _text(cfg.get("central_url"))
    if not central_url:
        raise GalleryError("central_url is not configured")
    _validate_http_url(central_url, cfg, field="central_url")
    endpoint = _text(cfg.get("embedding_gallery_path")) or DEFAULT_ENDPOINT
    url = urljoin(central_url.rstrip("/") + "/", endpoint.lstrip("/"))
    _validate_http_url(url, cfg, field="embedding gallery URL")
    return url


def _validate_redirect(current_url, location, cfg):
    location = _text(location)
    if not location:
        raise GalleryError("embedding sync redirect is missing a Location header")
    target_url = urljoin(current_url, location)
    current = _validate_http_url(current_url, cfg, field="embedding gallery URL")
    target = urlparse(target_url)
    if target.scheme not in {"http", "https"} or not target.netloc:
        raise GalleryError("embedding redirect URL must be an absolute HTTP(S) URL")
    if target.username or target.password:
        raise GalleryError("embedding redirect URL must not contain embedded credentials")
    if current.scheme == "https" and target.scheme != "https":
        raise GalleryError("embedding sync refused an HTTPS-to-HTTP redirect")
    target = _validate_http_url(target_url, cfg, field="embedding redirect URL")
    if _origin(current) != _origin(target):
        raise GalleryError("embedding sync refused a cross-origin redirect")
    return target_url


def _timeouts(cfg):
    connect = max(0.25, float(cfg.get("embedding_connect_timeout_seconds", 5)))
    read = max(1.0, float(cfg.get("embedding_read_timeout_seconds", 30)))
    return connect, read


def _request_with_validated_redirects(
    session,
    url,
    *,
    headers,
    params,
    timeout,
    cfg,
):
    current_url = url
    max_redirects = min(10, max(0, int(cfg.get("embedding_max_redirects", 3))))
    redirects = 0

    while True:
        response = session.get(
            current_url,
            headers=headers,
            params=params if redirects == 0 else None,
            timeout=timeout,
            stream=True,
            allow_redirects=False,
        )
        if response.status_code in REDIRECT_STATUSES:
            if redirects >= max_redirects:
                response.close()
                raise GalleryError(
                    f"embedding sync exceeded maximum redirects ({max_redirects})"
                )
            try:
                next_url = _validate_redirect(
                    current_url, response.headers.get("Location"), cfg
                )
            except Exception:
                response.close()
                raise
            response.close()
            current_url = next_url
            redirects += 1
            continue
        if 300 <= response.status_code < 400 and response.status_code != 304:
            response.close()
            raise GalleryError(
                f"embedding sync received unsupported redirect status "
                f"{response.status_code}"
            )
        return response, current_url


def _read_limited_json(response, max_bytes):
    content_type = _text(response.headers.get("Content-Type")).lower()
    media_type = content_type.split(";", 1)[0].strip()
    if media_type != "application/json" and not media_type.endswith("+json"):
        raise GalleryError(
            f"embedding sync expected application/json, received {media_type or '<missing>'}"
        )

    max_bytes = max(1024, int(max_bytes))
    length = _text(response.headers.get("Content-Length"))
    if length:
        try:
            if int(length) > max_bytes:
                raise GalleryError(
                    f"embedding gallery exceeds max response size of {max_bytes} bytes"
                )
        except ValueError as exc:
            raise GalleryError("invalid Content-Length from embedding server") from exc

    chunks = []
    total = 0
    iterator = (
        response.iter_content(chunk_size=64 * 1024)
        if hasattr(response, "iter_content")
        else [getattr(response, "content", b"")]
    )
    for chunk in iterator:
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise GalleryError(
                f"embedding gallery exceeds max response size of {max_bytes} bytes"
            )
        chunks.append(chunk)
    try:
        return json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GalleryError(f"embedding sync returned invalid JSON: {exc}") from exc


def _gallery_options(cfg):
    return effective_gallery_options(cfg)


def _local_gallery(gallery_path, cfg):
    _, metadata, sanitized = load_gallery(gallery_path, **_gallery_options(cfg))
    return metadata, sanitized


def _result_from_metadata(
    metadata,
    release_info,
    *,
    url,
    attempted_at,
    changed,
    etag,
    scope_id,
    not_modified=False,
):
    return {
        "ok": True,
        "changed": bool(changed),
        "not_modified": bool(not_modified),
        "attempted_at": attempted_at,
        "last_success_at": utc_now(),
        "source_url": url,
        "release_scope_id": scope_id,
        "branch": metadata.get("branch"),
        "gallery_version": metadata.get("gallery_version"),
        "checksum": metadata.get("checksum"),
        "etag": etag,
        "model": metadata.get("model"),
        "model_version": metadata.get("model_version"),
        "dimension": metadata.get("dimension"),
        "employee_count": metadata.get("employee_count"),
        "embedding_count": metadata.get("embedding_count"),
        "release_verified": bool(release_info.get("verified")),
        "release_sequence": release_info.get("sequence"),
        "release_publisher": release_info.get("publisher", ""),
        "release_key_id": release_info.get("key_id", ""),
        "release_generated_at": release_info.get("generated_at", ""),
        "error": "",
    }


def _write_success_status(
    status_path,
    status,
    scope_id,
    descriptor,
    release_info,
    result,
):
    scopes = record_acceptance(
        status,
        scope_id,
        descriptor,
        release_info,
        etag=result.get("etag"),
        accepted_at=result.get("last_success_at"),
        history_limit=int(result.get("history_limit") or 32),
    )
    clean = dict(result)
    clean.pop("history_limit", None)
    return write_sync_status(
        status_path,
        release_scopes=scopes,
        **clean,
    )


def sync_gallery(cfg, gallery_path, status_path, session=requests, sleep=time.sleep):
    attempted_at = utc_now()
    gallery_options = _gallery_options(cfg)
    release_issues = release_policy_issues(cfg)
    if release_issues:
        raise GalleryError(
            "embedding release policy is invalid: "
            + "; ".join(message for _, message in release_issues)
        )
    requested_url = _validate_source(cfg)
    resolved_url = requested_url
    gallery_path = Path(gallery_path)
    status = read_sync_status(status_path)
    scope_id, descriptor = release_scope(requested_url, cfg)
    previous_release = scope_state(status, scope_id)
    branch = _text(cfg.get("branch_name"))
    params = {"branch": branch} if branch else None
    retries = max(0, int(cfg.get("embedding_sync_retries", 2)))
    retry_base = max(0.1, float(cfg.get("embedding_retry_base_seconds", 1.0)))
    max_bytes = int(cfg.get("embedding_max_response_bytes", 50 * 1024 * 1024))
    response = None

    try:
        for attempt in range(retries + 1):
            try:
                response, resolved_url = _request_with_validated_redirects(
                    session,
                    requested_url,
                    headers=_request_headers(
                        cfg,
                        previous_release,
                        conditional=gallery_path.exists(),
                    ),
                    params=params,
                    timeout=_timeouts(cfg),
                    cfg=cfg,
                )
                if response.status_code == 304:
                    if not previous_release:
                        raise GalleryError(
                            "embedding server returned 304 without matching scoped release state"
                        )
                    metadata, sanitized = _local_gallery(gallery_path, cfg)
                    release_info = validate_installed_release(
                        sanitized,
                        cfg,
                        status,
                        source_url=requested_url,
                    )
                    freshness = enforce_gallery_freshness(
                        cfg,
                        metadata.get("generated_at"),
                        path=gallery_path,
                    )
                    etag = _text(response.headers.get("ETag")) or scoped_etag(
                        status, scope_id
                    )
                    result = _result_from_metadata(
                        metadata,
                        release_info,
                        url=resolved_url,
                        attempted_at=attempted_at,
                        changed=False,
                        etag=etag,
                        scope_id=scope_id,
                        not_modified=True,
                    )
                    result["gallery_age_seconds"] = freshness["age_seconds"]
                    result["gallery_stale"] = freshness["stale"]
                    result["history_limit"] = int(
                        cfg.get("embedding_release_history_limit", 32)
                    )
                    _write_success_status(
                        status_path,
                        status,
                        scope_id,
                        descriptor,
                        release_info,
                        result,
                    )
                    result.pop("history_limit", None)
                    return result

                if response.status_code >= 500 and attempt < retries:
                    response.close()
                    response = None
                    delay = retry_base * (2**attempt) + random.uniform(0, retry_base)
                    sleep(delay)
                    continue

                response.raise_for_status()
                payload = _read_limited_json(response, max_bytes)
                if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
                    payload = payload["data"]

                sanitized, _, metadata = validate_gallery(
                    payload, **gallery_options
                )
                release_info = validate_release(
                    sanitized,
                    cfg,
                    previous_release,
                )
                freshness = enforce_gallery_freshness(
                    cfg,
                    metadata.get("generated_at"),
                    path=gallery_path,
                )
                try:
                    current_metadata, _ = _local_gallery(gallery_path, cfg)
                    current_checksum = current_metadata.get("checksum")
                except GalleryError:
                    current_checksum = None

                changed = current_checksum != metadata.get("checksum")
                if changed:
                    write_gallery_atomic(
                        gallery_path, sanitized, **gallery_options
                    )
                etag = _text(response.headers.get("ETag"))
                if not etag and metadata.get("checksum"):
                    etag = f'"{metadata["checksum"]}"'
                result = _result_from_metadata(
                    metadata,
                    release_info,
                    url=resolved_url,
                    attempted_at=attempted_at,
                    changed=changed,
                    etag=etag,
                    scope_id=scope_id,
                )
                result["gallery_age_seconds"] = freshness["age_seconds"]
                result["gallery_stale"] = freshness["stale"]
                result["history_limit"] = int(
                    cfg.get("embedding_release_history_limit", 32)
                )
                _write_success_status(
                    status_path,
                    status,
                    scope_id,
                    descriptor,
                    release_info,
                    result,
                )
                result.pop("history_limit", None)
                return result
            except requests.RequestException:
                if attempt >= retries:
                    raise
                delay = retry_base * (2**attempt) + random.uniform(0, retry_base)
                sleep(delay)
            finally:
                if response is not None:
                    response.close()
                    response = None
    except Exception as exc:
        write_sync_status(
            status_path,
            ok=False,
            attempted_at=attempted_at,
            source_url=resolved_url,
            release_scope_id=scope_id,
            error=str(exc),
        )
        if isinstance(exc, GalleryError):
            raise
        if isinstance(exc, requests.RequestException):
            raise GalleryError(f"embedding sync request failed: {exc}") from exc
        if isinstance(exc, (ValueError, TypeError)):
            raise GalleryError(f"embedding sync response is invalid: {exc}") from exc
        raise
