from datetime import datetime, timezone
from data_contract import (
    GalleryError,
    MAX_EMBEDDING_DIMENSION,
    MAX_EMBEDDINGS_PER_EMPLOYEE,
    MAX_GALLERY_EMPLOYEES,
    MAX_TOTAL_EMBEDDINGS,
    bounded_limit,
    strict_int,
    validate_gallery_label,
    validate_token,
)

def production_enabled(cfg):
    return bool(cfg.get("production_mode", False))

def is_placeholder(value):
    return not str(value or "").strip()

def gallery_policy_issues(cfg):
    if not production_enabled(cfg):
        return ()

    issues = []
    try:
        branch = validate_gallery_label(
            cfg.get("branch_name"), "branch_name", required=False, max_chars=128
        )
    except GalleryError as exc:
        branch = ""
        issues.append(("branch_name_invalid", str(exc)))
    try:
        model = validate_token(cfg.get("model"), "model", required=False)
    except GalleryError as exc:
        model = ""
        issues.append(("model_name_invalid", str(exc)))
    try:
        model_version = validate_token(
            cfg.get("model_version"), "model_version", required=False
        )
    except GalleryError as exc:
        model_version = ""
        issues.append(("model_version_invalid", str(exc)))

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
        strict_int(
            cfg.get("embedding_max_age_seconds", 86400),
            "embedding_max_age_seconds",
            minimum=1,
            maximum=(1 << 31) - 1,
        )
    except GalleryError as exc:
        issues.append(("gallery_max_age_invalid", str(exc)))

    try:
        bounded_limit(
            cfg.get("max_gallery_employees"),
            "max_gallery_employees",
            10000,
            MAX_GALLERY_EMPLOYEES,
        )
        bounded_limit(
            cfg.get("max_embeddings_per_employee"),
            "max_embeddings_per_employee",
            50,
            MAX_EMBEDDINGS_PER_EMPLOYEE,
        )
        bounded_limit(
            cfg.get("max_embedding_dimension"),
            "max_embedding_dimension",
            MAX_EMBEDDING_DIMENSION,
            MAX_EMBEDDING_DIMENSION,
        )
        bounded_limit(
            cfg.get("max_gallery_embeddings"),
            "max_gallery_embeddings",
            MAX_TOTAL_EMBEDDINGS,
            MAX_TOTAL_EMBEDDINGS,
        )
    except GalleryError as exc:
        issues.append(("gallery_limits_invalid", str(exc)))
    return tuple(issues)

def effective_gallery_options(cfg):
    issues = gallery_policy_issues(cfg)
    if issues:
        raise GalleryError(
            "strict production gallery policy is invalid: "
            + "; ".join(message for _, message in issues)
        )

    strict = production_enabled(cfg)
    model = validate_token(
        cfg.get("model") or "buffalo_l", "model", required=True
    )
    model_version = validate_token(
        cfg.get("model_version"), "model_version", required=False
    )
    branch = validate_gallery_label(
        cfg.get("branch_name"), "branch_name", required=False, max_chars=128
    )
    max_employees = bounded_limit(
        cfg.get("max_gallery_employees"),
        "max_gallery_employees",
        10000,
        MAX_GALLERY_EMPLOYEES,
    )
    max_embeddings = bounded_limit(
        cfg.get("max_embeddings_per_employee"),
        "max_embeddings_per_employee",
        50,
        MAX_EMBEDDINGS_PER_EMPLOYEE,
    )
    max_dimension = bounded_limit(
        cfg.get("max_embedding_dimension"),
        "max_embedding_dimension",
        MAX_EMBEDDING_DIMENSION,
        MAX_EMBEDDING_DIMENSION,
    )
    max_total_embeddings = bounded_limit(
        cfg.get("max_gallery_embeddings"),
        "max_gallery_embeddings",
        MAX_TOTAL_EMBEDDINGS,
        MAX_TOTAL_EMBEDDINGS,
    )

    return {
        "expected_model": model,
        "expected_model_version": model_version,
        "expected_branch": branch,
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
        "max_dimension": max_dimension,
        "max_total_embeddings": max_total_embeddings,
    }

def gallery_freshness_status(cfg, generated_at, *, path="", now=None):
    generated = _as_utc_datetime(generated_at)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)

    max_age_value = cfg.get("embedding_max_age_seconds", 86400)
    minimum = 1 if production_enabled(cfg) else 0
    max_age = strict_int(
        max_age_value,
        "embedding_max_age_seconds",
        minimum=minimum,
        maximum=(1 << 31) - 1,
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
