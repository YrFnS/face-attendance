import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from data_contract import (
    GalleryError,
    MAX_EMBEDDING_DIMENSION,
    MAX_EMBEDDINGS_PER_EMPLOYEE,
    MAX_GALLERY_EMPLOYEES,
    MAX_RELEASE_SEQUENCE,
    MAX_TOTAL_EMBEDDINGS,
    canonical_timestamp,
    strict_int,
    strict_json_loads,
    validate_base64url_text,
    validate_checksum,
    validate_display_text,
    validate_employee_id,
    validate_employee_name,
    validate_embedding_vector,
    validate_gallery_label,
    validate_token,
)


SCHEMA_VERSION = 1
_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "gallery_version",
        "generated_at",
        "model",
        "model_version",
        "dimension",
        "normalized",
        "branch",
        "employees",
        "release",
        "checksum",
    }
)
_EMPLOYEE_FIELDS = frozenset(
    {"employee", "person", "employee_name", "embeddings", "embedding"}
)
_RELEASE_FIELDS = frozenset(
    {"sequence", "publisher", "key_id", "algorithm", "signature"}
)


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def norm(vector):
    try:
        array = np.asarray(vector, dtype=np.float32)
    except (TypeError, ValueError) as exc:
        raise GalleryError("embedding must contain numeric values") from exc
    if array.ndim != 1 or array.size == 0:
        raise GalleryError("embedding must be a non-empty one-dimensional vector")
    if array.size > MAX_EMBEDDING_DIMENSION:
        raise GalleryError(
            f"embedding dimension exceeds hard maximum {MAX_EMBEDDING_DIMENSION}"
        )
    if not np.all(np.isfinite(array)):
        raise GalleryError("embedding contains NaN or infinite values")
    length = float(np.linalg.norm(array))
    if not np.isfinite(length) or length <= 1e-12:
        raise GalleryError("embedding has zero length")
    return array / length


def match_employee(known, embedding):
    embedding = norm(embedding)
    scores = []
    for item in known:
        vectors = item.get("embeddings") or [item["embedding"]]
        best = max(float(np.dot(norm(vector), embedding)) for vector in vectors)
        scores.append((best, item["employee"]))
    scores.sort(reverse=True)
    best_score, employee = scores[0] if scores else (0.0, None)
    second_score = scores[1][0] if len(scores) > 1 else 0.0
    return best_score, employee, best_score - second_score


def _unexpected_fields(value, allowed, field):
    unexpected = sorted(set(value) - set(allowed))
    if unexpected:
        raise GalleryError(
            f"{field} contains unsupported field(s): {', '.join(unexpected)}"
        )


def _limit(value, field, default, hard_maximum):
    if value is None:
        value = default
    return strict_int(value, field, minimum=1, maximum=hard_maximum)


def _employee_value(item, index):
    has_employee = "employee" in item and item.get("employee") is not None
    has_person = "person" in item and item.get("person") is not None
    if has_employee and has_person:
        employee = validate_employee_id(
            item.get("employee"), f"employees[{index}].employee"
        )
        person = validate_employee_id(
            item.get("person"), f"employees[{index}].person"
        )
        if employee != person:
            raise GalleryError(
                f"employees[{index}] has conflicting employee and person values"
            )
        return employee
    value = item.get("employee") if has_employee else item.get("person")
    return validate_employee_id(value, f"employees[{index}].employee")


def _vectors_value(item, index, employee):
    has_many = "embeddings" in item and item.get("embeddings") is not None
    has_one = "embedding" in item and item.get("embedding") is not None
    if has_many and has_one:
        raise GalleryError(
            f"employees[{index}] must not contain both embedding and embeddings"
        )
    vectors = item.get("embeddings") if has_many else None
    if vectors is None and has_one:
        vectors = [item.get("embedding")]
    if not isinstance(vectors, list) or not vectors:
        raise GalleryError(f"employee {employee} has no embeddings")
    return vectors


def _sanitize_release(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise GalleryError("release must be a JSON object")
    _unexpected_fields(value, _RELEASE_FIELDS, "release")
    sequence = strict_int(
        value.get("sequence"),
        "release.sequence",
        minimum=1,
        maximum=MAX_RELEASE_SEQUENCE,
    )
    publisher = validate_token(
        value.get("publisher"), "release.publisher", required=True
    )
    key_id = validate_token(value.get("key_id"), "release.key_id", required=True)
    algorithm = validate_token(
        value.get("algorithm"), "release.algorithm", required=True, max_chars=32
    ).lower()
    signature = validate_base64url_text(
        value.get("signature"),
        field="release.signature",
        expected_decoded_bytes=64,
        max_chars=128,
    )
    return {
        "sequence": sequence,
        "publisher": publisher,
        "key_id": key_id,
        "algorithm": algorithm,
        "signature": signature,
    }


def _gallery_checksum(payload):
    checksum_payload = dict(payload)
    checksum_payload.pop("checksum", None)
    encoded = json.dumps(
        checksum_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_gallery(
    payload,
    *,
    expected_model=None,
    expected_model_version=None,
    expected_branch=None,
    require_model_match=True,
    require_model_version_match=False,
    allow_empty=False,
    max_employees=10000,
    max_embeddings_per_employee=50,
    max_dimension=MAX_EMBEDDING_DIMENSION,
    max_total_embeddings=MAX_TOTAL_EMBEDDINGS,
):
    if not isinstance(payload, dict):
        raise GalleryError("embedding gallery must be a JSON object")
    _unexpected_fields(payload, _TOP_LEVEL_FIELDS, "embedding gallery")

    schema_version = strict_int(
        payload.get("schema_version"),
        "schema_version",
        minimum=1,
        maximum=SCHEMA_VERSION,
    )
    if schema_version != SCHEMA_VERSION:
        raise GalleryError(
            f"unsupported schema_version {schema_version}; expected {SCHEMA_VERSION}"
        )

    model = validate_token(payload.get("model"), "model", required=True)
    expected_model = validate_token(
        expected_model, "expected_model", required=False
    )
    if require_model_match and expected_model and model != expected_model:
        raise GalleryError(f"model mismatch: received {model}, expected {expected_model}")

    model_version = validate_token(
        payload.get("model_version"), "model_version", required=False
    )
    expected_model_version = validate_token(
        expected_model_version, "expected_model_version", required=False
    )
    if (
        require_model_version_match
        and expected_model_version
        and model_version != expected_model_version
    ):
        raise GalleryError(
            "model version mismatch: "
            f"received {model_version or '<missing>'}, expected {expected_model_version}"
        )

    branch = validate_gallery_label(
        payload.get("branch"), "branch", required=False, max_chars=128
    )
    expected_branch = validate_gallery_label(
        expected_branch, "expected_branch", required=False, max_chars=128
    )
    if expected_branch and branch != expected_branch:
        raise GalleryError(
            f"branch mismatch: received {branch!r}, expected {expected_branch!r}"
        )

    generated_at = canonical_timestamp(payload.get("generated_at"), "generated_at")
    gallery_version = validate_token(
        payload.get("gallery_version"),
        "gallery_version",
        required=False,
        max_chars=128,
    )
    if not gallery_version:
        gallery_version = generated_at

    hard_dimension = _limit(
        max_dimension,
        "max_dimension",
        MAX_EMBEDDING_DIMENSION,
        MAX_EMBEDDING_DIMENSION,
    )
    dimension = strict_int(
        payload.get("dimension"),
        "dimension",
        minimum=1,
        maximum=hard_dimension,
    )

    normalized_value = payload.get("normalized")
    if normalized_value is not None and not isinstance(normalized_value, bool):
        raise GalleryError("normalized must be a boolean")

    employee_limit = _limit(
        max_employees,
        "max_employees",
        10000,
        MAX_GALLERY_EMPLOYEES,
    )
    employee_embedding_limit = _limit(
        max_embeddings_per_employee,
        "max_embeddings_per_employee",
        50,
        MAX_EMBEDDINGS_PER_EMPLOYEE,
    )
    total_embedding_limit = _limit(
        max_total_embeddings,
        "max_total_embeddings",
        MAX_TOTAL_EMBEDDINGS,
        MAX_TOTAL_EMBEDDINGS,
    )

    employees = payload.get("employees")
    if not isinstance(employees, list):
        raise GalleryError("employees must be a list")
    if not employees and not allow_empty:
        raise GalleryError("refusing to activate an empty embedding gallery")
    if len(employees) > employee_limit:
        raise GalleryError(f"gallery exceeds max_employees={employee_limit}")

    seen = set()
    sanitized_employees = []
    known = []
    embedding_count = 0

    for index, item in enumerate(employees):
        if not isinstance(item, dict):
            raise GalleryError(f"employees[{index}] must be an object")
        _unexpected_fields(item, _EMPLOYEE_FIELDS, f"employees[{index}]")
        employee = _employee_value(item, index)
        if employee in seen:
            raise GalleryError(f"duplicate employee in gallery: {employee}")
        seen.add(employee)

        vectors = _vectors_value(item, index, employee)
        if len(vectors) > employee_embedding_limit:
            raise GalleryError(
                f"employee {employee} exceeds max_embeddings_per_employee="
                f"{employee_embedding_limit}"
            )
        embedding_count += len(vectors)
        if embedding_count > total_embedding_limit:
            raise GalleryError(
                f"gallery exceeds max_total_embeddings={total_embedding_limit}"
            )

        normalized = []
        for vector_index, vector in enumerate(vectors):
            field = f"employees[{index}].embeddings[{vector_index}]"
            clean_values = validate_embedding_vector(
                vector, field=field, dimension=dimension
            )
            try:
                clean_vector = norm(clean_values)
            except GalleryError as exc:
                raise GalleryError(
                    f"invalid embedding for {employee} at index {vector_index}: {exc}"
                ) from exc
            normalized.append(clean_vector)

        employee_name = validate_employee_name(
            item.get("employee_name"), f"employees[{index}].employee_name"
        )
        sanitized_item = {
            "employee": employee,
            "embeddings": [vector.tolist() for vector in normalized],
        }
        if employee_name:
            sanitized_item["employee_name"] = employee_name
        sanitized_employees.append(sanitized_item)
        known.append(
            {
                "employee": employee,
                "employee_name": employee_name,
                "embedding": norm(np.mean(normalized, axis=0)),
                "embeddings": normalized,
            }
        )

    sanitized = {
        "schema_version": SCHEMA_VERSION,
        "gallery_version": gallery_version,
        "generated_at": generated_at,
        "model": model,
        "model_version": model_version,
        "dimension": dimension,
        "normalized": True,
        "branch": branch,
        "employees": sanitized_employees,
    }
    release = _sanitize_release(payload.get("release"))
    if release is not None:
        sanitized["release"] = release
    calculated_checksum = _gallery_checksum(sanitized)
    if "checksum" in payload and payload.get("checksum") is not None:
        supplied_checksum = validate_checksum(payload.get("checksum"), "checksum")
        if supplied_checksum != calculated_checksum:
            raise GalleryError("embedding gallery checksum does not match its content")
    sanitized["checksum"] = calculated_checksum

    metadata = {
        key: value for key, value in sanitized.items() if key != "employees"
    }
    metadata["employee_count"] = len(sanitized_employees)
    metadata["embedding_count"] = embedding_count
    if release is not None:
        metadata.update(
            release_sequence=release["sequence"],
            release_publisher=release["publisher"],
            release_key_id=release["key_id"],
            release_algorithm=release["algorithm"],
        )
    return sanitized, known, metadata


def _local_vector(vector, *, field):
    if isinstance(vector, np.ndarray):
        vector = vector.tolist()
    elif isinstance(vector, tuple):
        vector = list(vector)
    if not isinstance(vector, list):
        raise GalleryError(f"{field} must be a one-dimensional vector")
    dimension = len(vector)
    if dimension <= 0 or dimension > MAX_EMBEDDING_DIMENSION:
        raise GalleryError(
            f"{field} dimension must be between 1 and {MAX_EMBEDDING_DIMENSION}"
        )
    clean = validate_embedding_vector(vector, field=field, dimension=dimension)
    return norm(clean)


def build_gallery_payload(
    employees,
    *,
    model,
    branch="",
    model_version="",
    gallery_version=None,
):
    if not isinstance(employees, (list, tuple)):
        raise GalleryError("employees must be a list or tuple")
    model = validate_token(model, "model", required=True)
    model_version = validate_token(
        model_version, "model_version", required=False
    )
    branch = validate_gallery_label(branch, "branch", required=False, max_chars=128)
    if gallery_version is not None:
        gallery_version = validate_token(
            gallery_version, "gallery_version", required=True, max_chars=128
        )

    employee_rows = []
    dimension = None
    for index, item in enumerate(employees):
        if not isinstance(item, dict):
            raise GalleryError(f"employees[{index}] must be an object")
        employee = validate_employee_id(
            item.get("employee") or item.get("person"),
            f"employees[{index}].employee",
        )
        employee_name = validate_employee_name(
            item.get("employee_name"), f"employees[{index}].employee_name"
        )
        vectors = item.get("embeddings")
        if vectors is None and item.get("embedding") is not None:
            vectors = [item.get("embedding")]
        if not isinstance(vectors, (list, tuple)) or not vectors:
            continue
        normalized = [
            _local_vector(
                vector,
                field=f"employees[{index}].embeddings[{vector_index}]",
            )
            for vector_index, vector in enumerate(vectors)
            if vector is not None
        ]
        if not normalized:
            continue
        current_dimension = int(normalized[0].size)
        if dimension is None:
            dimension = current_dimension
        if current_dimension != dimension or any(
            int(vector.size) != dimension for vector in normalized
        ):
            raise GalleryError(
                f"embedding dimension mismatch for employee {employee}"
            )
        row = {
            "employee": employee,
            "embeddings": [vector.tolist() for vector in normalized],
        }
        if employee_name:
            row["employee_name"] = employee_name
        employee_rows.append(row)
    if dimension is None:
        raise GalleryError("cannot build a gallery without embeddings")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "gallery_version": gallery_version or utc_now(),
        "generated_at": utc_now(),
        "model": model,
        "model_version": model_version,
        "dimension": dimension,
        "normalized": True,
        "branch": branch,
        "employees": employee_rows,
    }
    return validate_gallery(payload)[0]


def _atomic_write_text(path, text, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temp_path, mode)
        except OSError:
            pass
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def write_gallery_atomic(
    path,
    payload,
    *,
    expected_model=None,
    expected_model_version=None,
    expected_branch=None,
    require_model_match=True,
    require_model_version_match=False,
    allow_empty=False,
    max_employees=10000,
    max_embeddings_per_employee=50,
    max_dimension=MAX_EMBEDDING_DIMENSION,
    max_total_embeddings=MAX_TOTAL_EMBEDDINGS,
):
    sanitized, known, metadata = validate_gallery(
        payload,
        expected_model=expected_model,
        expected_model_version=expected_model_version,
        expected_branch=expected_branch,
        require_model_match=require_model_match,
        require_model_version_match=require_model_version_match,
        allow_empty=allow_empty,
        max_employees=max_employees,
        max_embeddings_per_employee=max_embeddings_per_employee,
        max_dimension=max_dimension,
        max_total_embeddings=max_total_embeddings,
    )
    _atomic_write_text(
        path,
        json.dumps(
            sanitized, ensure_ascii=False, indent=2, allow_nan=False
        )
        + "\n",
    )
    return known, metadata


def load_gallery(
    path,
    *,
    expected_model=None,
    expected_model_version=None,
    expected_branch=None,
    require_model_match=True,
    require_model_version_match=False,
    allow_empty=False,
    max_employees=10000,
    max_embeddings_per_employee=50,
    max_dimension=MAX_EMBEDDING_DIMENSION,
    max_total_embeddings=MAX_TOTAL_EMBEDDINGS,
):
    path = Path(path)
    try:
        payload = strict_json_loads(
            path.read_text(encoding="utf-8"),
            field="embedding gallery",
        )
    except FileNotFoundError as exc:
        raise GalleryError(f"embedding gallery not found: {path}") from exc
    except (OSError, GalleryError) as exc:
        raise GalleryError(f"could not read embedding gallery {path}: {exc}") from exc
    sanitized, known, metadata = validate_gallery(
        payload,
        expected_model=expected_model,
        expected_model_version=expected_model_version,
        expected_branch=expected_branch,
        require_model_match=require_model_match,
        require_model_version_match=require_model_version_match,
        allow_empty=allow_empty,
        max_employees=max_employees,
        max_embeddings_per_employee=max_embeddings_per_employee,
        max_dimension=max_dimension,
        max_total_embeddings=max_total_embeddings,
    )
    return known, metadata, sanitized


def gallery_signature(path):
    try:
        stat = Path(path).stat()
    except FileNotFoundError:
        return None
    return stat.st_mtime_ns, stat.st_size

def read_sync_status(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def write_sync_status(path, **values):
    current = read_sync_status(path)
    current.update(values)
    _atomic_write_text(
        path,
        json.dumps(
            current, ensure_ascii=False, indent=2, allow_nan=False
        )
        + "\n",
    )
    return current


def _runtime_release_context(path):
    path = Path(path)
    config_path = path.parent / "config.json"
    if not config_path.is_file():
        return None
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(cfg, dict):
        return None
    status = read_sync_status(path.parent / "embedding_sync_status.json")
    return cfg, status


def _validate_runtime_release(path, sanitized):
    context = _runtime_release_context(path)
    if context is None:
        return None
    cfg, status = context
    from gallery_release import configured_source_url, validate_installed_release

    return validate_installed_release(
        sanitized,
        cfg,
        status,
        source_url=configured_source_url(cfg),
    )


def gallery_status(path, *, max_age_seconds=None):
    path = Path(path)
    if not path.exists():
        return {"available": False, "path": str(path), "error": "gallery not found"}
    try:
        _, metadata, sanitized = load_gallery(path, require_model_match=False)
        release = _validate_runtime_release(path, sanitized)
        context = _runtime_release_context(path)
        if context is not None:
            cfg, _ = context
            cfg = dict(cfg)
            if max_age_seconds is not None:
                cfg["embedding_max_age_seconds"] = int(max_age_seconds)
            from runtime_policy import gallery_freshness_status

            freshness = gallery_freshness_status(
                cfg,
                metadata.get("generated_at"),
                path=path,
            )
        else:
            stat = path.stat()
            age_seconds = max(
                0, int(datetime.now().timestamp() - stat.st_mtime)
            )
            max_age = int(max_age_seconds or 0)
            freshness = {
                "path": str(path),
                "updated_at": datetime.fromtimestamp(
                    stat.st_mtime, timezone.utc
                ).isoformat().replace("+00:00", "Z"),
                "age_seconds": age_seconds,
                "stale": bool(max_age and age_seconds > max_age),
                "policy_valid": True,
                "error": "",
            }
        result = {
            "available": True,
            **freshness,
            **metadata,
        }
        if release is not None:
            result["release_validation"] = release
        return result
    except (GalleryError, OSError) as exc:
        return {"available": False, "path": str(path), "error": str(exc)}


def sync_gallery(cfg, gallery_path, status_path, session=None, sleep=None):
    """Compatibility wrapper for callers that have not moved to secure_sync yet."""

    from secure_sync import sync_gallery as secure_sync_gallery

    kwargs = {}
    if session is not None:
        kwargs["session"] = session
    if sleep is not None:
        kwargs["sleep"] = sleep
    return secure_sync_gallery(cfg, gallery_path, status_path, **kwargs)


class GalleryReloader:
    def __init__(
        self,
        path,
        *,
        expected_model=None,
        expected_model_version=None,
        expected_branch=None,
        require_model_match=True,
        require_model_version_match=False,
        allow_empty=False,
        max_employees=10000,
        max_embeddings_per_employee=50,
        max_dimension=MAX_EMBEDDING_DIMENSION,
        max_total_embeddings=MAX_TOTAL_EMBEDDINGS,
    ):
        self.path = Path(path)
        self.expected_model = expected_model
        self.expected_model_version = expected_model_version
        self.expected_branch = expected_branch
        self.require_model_match = require_model_match
        self.require_model_version_match = require_model_version_match
        self.allow_empty = allow_empty
        self.max_employees = int(max_employees)
        self.max_embeddings_per_employee = int(max_embeddings_per_employee)
        self.max_dimension = int(max_dimension)
        self.max_total_embeddings = int(max_total_embeddings)
        self.signature = None
        self.known = []
        self.metadata = {}
        self.generated_at = ""
        self.updated_unix = 0.0

    def reload(self, force=False):
        signature = gallery_signature(self.path)
        if not force and signature == self.signature and self.known:
            return self.known, self.metadata, False
        known, metadata, sanitized = load_gallery(
            self.path,
            expected_model=self.expected_model,
            expected_model_version=self.expected_model_version,
            expected_branch=self.expected_branch,
            require_model_match=self.require_model_match,
            require_model_version_match=self.require_model_version_match,
            allow_empty=self.allow_empty,
            max_employees=self.max_employees,
            max_embeddings_per_employee=self.max_embeddings_per_employee,
            max_dimension=self.max_dimension,
            max_total_embeddings=self.max_total_embeddings,
        )
        release = _validate_runtime_release(self.path, sanitized)
        if release is not None:
            metadata = dict(metadata)
            metadata["release_validation"] = release
        from gallery_release import parse_generated_at

        self.generated_at = str(metadata.get("generated_at") or "")
        self.updated_unix = parse_generated_at(self.generated_at).timestamp()
        self.known = known
        self.metadata = metadata
        self.signature = gallery_signature(self.path)
        return self.known, self.metadata, True
