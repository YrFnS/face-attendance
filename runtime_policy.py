import time
from datetime import datetime, timezone
from pathlib import Path

from embedding_gallery import GalleryError, load_gallery, read_sync_status
from gallery_release import (
    configured_source_url,
    parse_generated_at,
    release_policy_issues,
    validate_installed_release,
)
from model_manifest import is_placeholder


def _text(value):
    return str(value or "").strip()


def _positive_int(value, field, default):
    try:
        number = int(default if value is None else value)
    except (TypeError, ValueError) as exc:
        raise GalleryError(f"{field} must be an integer") from exc
    if number <= 0:
        raise GalleryError(f"{field} must be greater than zero")
    return number


def production_enabled(cfg):
    return bool(cfg.get("production_mode", False))


def gallery_policy_issues(cfg):
    if not production_enabled(cfg):
        return ()

    issues = []
    branch = _text(cfg.get("branch_name"))
    model = _text(cfg.get("model"))
    model_version = _text(cfg.get("model_version"))

    if is_placeholder(branch):
        issues.append(
            (
                "branch_name_missing",
                "branch_name must be a non-placeholder value in production",
            )
        )
    if is_placeholder(model):
        issues.append(
            (
                "model_name_missing",
                "model must be a non-placeholder value in production",
            )
        )
    if is_placeholder(model_version):
        issues.append(
            (
                "model_version_missing",
                "model_version must identify the approved runtime model in production",
            )
        )
    if not bool(cfg.get("require_model_match", True)):
        issues.append(
            (
                "model_match_not_required",
                "require_model_match must be true in production",
            )
        )
    if not bool(cfg.get("require_model_version_match", False)):
        issues.append(
            (
                "model_version_match_not_required",
                "require_model_version_match must be true in production",
            )
        )
    if bool(cfg.get("allow_empty_embedding_gallery", False)):
        issues.append(
            (
                "empty_gallery_allowed",
                "allow_empty_embedding_gallery must be false in production",
            )
        )
    if not bool(cfg.get("reject_stale_embedding_gallery", False)):
        issues.append(
            (
                "stale_gallery_allowed",
                "reject_stale_embedding_gallery must be true in production",
            )
        )
    try:
        max_age = int(cfg.get("embedding_max_age_seconds", 0))
    except (TypeError, ValueError):
        max_age = 0
    if max_age <= 0:
        issues.append(
            (
                "gallery_max_age_invalid",
                "embedding_max_age_seconds must be greater than zero in production",
            )
        )
    return tuple(issues)


def strict_profile_issues(cfg):
    if not production_enabled(cfg):
        return ()

    issues = list(gallery_policy_issues(cfg))
    issues.extend(release_policy_issues(cfg))
    boolean_requirements = (
        (
            "model_manifest_incomplete_allowed",
            "model_manifest_require_complete",
            True,
            "model_manifest_require_complete must be true in production",
        ),
        (
            "model_integrity_startup_disabled",
            "model_integrity_verify_on_start",
            True,
            "model_integrity_verify_on_start must be true in production",
        ),
        (
            "pad_face_binding_unsafe",
            "pad_require_single_face",
            True,
            "pad_require_single_face must be true until PAD is bound independently to every accepted face",
        ),
        (
            "inline_sync_enabled",
            "embedding_sync_inline_enabled",
            False,
            "embedding_sync_inline_enabled must be false in production",
        ),
        (
            "insecure_central_override_enabled",
            "allow_insecure_central_url",
            False,
            "allow_insecure_central_url must be false in production",
        ),
        (
            "unauthenticated_sync_override_enabled",
            "allow_unauthenticated_embedding_sync",
            False,
            "allow_unauthenticated_embedding_sync must be false in production",
        ),
        (
            "insecure_frappe_override_enabled",
            "allow_insecure_frappe_url",
            False,
            "allow_insecure_frappe_url must be false in production",
        ),
        (
            "insecure_pad_override_enabled",
            "pad_allow_insecure_url",
            False,
            "pad_allow_insecure_url must be false in production",
        ),
        (
            "unauthenticated_pad_override_enabled",
            "pad_allow_unauthenticated_local",
            False,
            "pad_allow_unauthenticated_local must be false in production",
        ),
    )
    for code, key, expected, message in boolean_requirements:
        if bool(cfg.get(key, not expected)) is not expected:
            issues.append((code, message))

    if bool(cfg.get("embedding_sync_enabled", True)):
        if not _text(cfg.get("central_url")):
            issues.append(
                (
                    "central_url_missing",
                    "central_url is required when embedding synchronization is enabled in production",
                )
            )
        if is_placeholder(cfg.get("central_api_token")):
            issues.append(
                (
                    "central_api_token_missing",
                    "central_api_token must be a non-placeholder value in production",
                )
            )
    return tuple(issues)


def effective_gallery_options(cfg):
    issues = gallery_policy_issues(cfg)
    if issues:
        raise GalleryError(
            "strict production gallery policy is invalid: "
            + "; ".join(message for _, message in issues)
        )

    strict = production_enabled(cfg)
    max_employees = _positive_int(
        cfg.get("max_gallery_employees"), "max_gallery_employees", 10000
    )
    max_embeddings = _positive_int(
        cfg.get("max_embeddings_per_employee"),
        "max_embeddings_per_employee",
        50,
    )

    return {
        "expected_model": _text(cfg.get("model") or "buffalo_l"),
        "expected_model_version": _text(cfg.get("model_version")),
        "expected_branch": _text(cfg.get("branch_name")),
        "require_model_match": (
            True if strict else bool(cfg.get("require_model_match", True))
        ),
        "require_model_version_match": (
            True
            if strict
            else bool(cfg.get("require_model_version_match", False))
        ),
        "allow_empty": (
            False
            if strict
            else bool(cfg.get("allow_empty_embedding_gallery", False))
        ),
        "max_employees": max_employees,
        "max_embeddings_per_employee": max_embeddings,
    }


def _as_utc_datetime(value):
    if isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise GalleryError("gallery timestamp must include a timezone")
        return parsed.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), timezone.utc)
    return parse_generated_at(value)


def gallery_freshness_status(cfg, generated_at, *, path="", now=None):
    generated = _as_utc_datetime(generated_at)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)

    try:
        max_age = int(cfg.get("embedding_max_age_seconds", 86400))
    except (TypeError, ValueError) as exc:
        raise GalleryError("embedding_max_age_seconds must be an integer") from exc
    if max_age <= 0 and production_enabled(cfg):
        raise GalleryError(
            "embedding_max_age_seconds must be greater than zero in production"
        )

    age_seconds = max(0, int((current - generated).total_seconds()))
    stale = bool(max_age > 0 and age_seconds > max_age)
    reject_stale = production_enabled(cfg) or bool(
        cfg.get("reject_stale_embedding_gallery", False)
    )
    policy_valid = not (stale and reject_stale)
    error = ""
    if not policy_valid:
        error = (
            f"embedding gallery is stale: age={age_seconds}s max={max_age}s"
        )
    generated_text = generated.isoformat().replace("+00:00", "Z")
    generated_unix = generated.timestamp()
    return {
        "path": str(path or ""),
        "generated_at": generated_text,
        "generated_unix": generated_unix,
        "updated_at": generated_text,
        "updated_unix": generated_unix,
        "age_seconds": age_seconds,
        "max_age_seconds": max_age,
        "stale": stale,
        "reject_stale": reject_stale,
        "policy_valid": policy_valid,
        "error": error,
    }


def enforce_gallery_freshness(cfg, generated_at, *, path="", now=None):
    status = gallery_freshness_status(
        cfg, generated_at, path=path, now=now
    )
    if not status["policy_valid"]:
        raise GalleryError(status["error"])
    return status


def _release_status_path(path, status_path=None):
    return (
        Path(status_path)
        if status_path is not None
        else Path(path).parent / "embedding_sync_status.json"
    )


def validate_runtime_release(
    cfg,
    path,
    payload,
    *,
    status_path=None,
    source_url=None,
    now=None,
):
    status = read_sync_status(_release_status_path(path, status_path))
    return validate_installed_release(
        payload,
        cfg,
        status,
        source_url=source_url or configured_source_url(cfg),
        now=now,
    )


def inspect_gallery(
    cfg,
    path,
    *,
    status_path=None,
    source_url=None,
    now=None,
):
    path = Path(path)
    try:
        options = effective_gallery_options(cfg)
        _, metadata, payload = load_gallery(path, **options)
        release = validate_runtime_release(
            cfg,
            path,
            payload,
            status_path=status_path,
            source_url=source_url,
            now=now,
        )
        freshness = gallery_freshness_status(
            cfg,
            metadata.get("generated_at"),
            path=path,
            now=now,
        )
    except (GalleryError, OSError, ValueError) as exc:
        return {
            "available": False,
            "policy_valid": False,
            "path": str(path),
            "error": str(exc),
        }

    return {
        "available": True,
        **freshness,
        **metadata,
        "release_validation": release,
    }


def load_runtime_gallery(
    cfg,
    path,
    *,
    status_path=None,
    source_url=None,
    now=None,
):
    path = Path(path)
    options = effective_gallery_options(cfg)
    known, metadata, payload = load_gallery(path, **options)
    release = validate_runtime_release(
        cfg,
        path,
        payload,
        status_path=status_path,
        source_url=source_url,
        now=now,
    )
    status = enforce_gallery_freshness(
        cfg,
        metadata.get("generated_at"),
        path=path,
        now=now,
    )
    metadata = dict(metadata)
    metadata["release_validation"] = release
    return known, metadata, payload, status
