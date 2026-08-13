import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np


SCHEMA_VERSION = 1


class GalleryError(ValueError):
    """Raised when an embedding gallery is missing, unsafe, or incompatible."""


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def norm(vector):
    array = np.asarray(vector, dtype=np.float32)
    if array.ndim != 1 or array.size == 0:
        raise GalleryError("embedding must be a non-empty one-dimensional vector")
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


def _clean_text(value, field, required=False):
    text = "" if value is None else str(value).strip()
    if required and not text:
        raise GalleryError(f"{field} is required")
    return text


def _gallery_checksum(payload):
    checksum_payload = dict(payload)
    checksum_payload.pop("checksum", None)
    encoded = json.dumps(
        checksum_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
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
):
    if not isinstance(payload, dict):
        raise GalleryError("embedding gallery must be a JSON object")

    try:
        schema_version = int(payload.get("schema_version"))
    except (TypeError, ValueError) as exc:
        raise GalleryError("schema_version must be an integer") from exc
    if schema_version != SCHEMA_VERSION:
        raise GalleryError(
            f"unsupported schema_version {schema_version}; expected {SCHEMA_VERSION}"
        )

    model = _clean_text(payload.get("model"), "model", required=True)
    expected_model = _clean_text(expected_model, "expected_model")
    if require_model_match and expected_model and model != expected_model:
        raise GalleryError(f"model mismatch: received {model}, expected {expected_model}")

    model_version = _clean_text(payload.get("model_version"), "model_version")
    expected_model_version = _clean_text(
        expected_model_version, "expected_model_version"
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

    branch = _clean_text(payload.get("branch"), "branch")
    expected_branch = _clean_text(expected_branch, "expected_branch")
    if expected_branch and branch != expected_branch:
        raise GalleryError(
            f"branch mismatch: received {branch!r}, expected {expected_branch!r}"
        )

    try:
        dimension = int(payload.get("dimension"))
    except (TypeError, ValueError) as exc:
        raise GalleryError("dimension must be an integer") from exc
    if dimension <= 0:
        raise GalleryError("dimension must be greater than zero")

    employees = payload.get("employees")
    if not isinstance(employees, list):
        raise GalleryError("employees must be a list")
    if not employees and not allow_empty:
        raise GalleryError("refusing to activate an empty embedding gallery")
    if len(employees) > int(max_employees):
        raise GalleryError(f"gallery exceeds max_employees={max_employees}")

    seen = set()
    sanitized_employees = []
    known = []
    embedding_count = 0

    for index, item in enumerate(employees):
        if not isinstance(item, dict):
            raise GalleryError(f"employees[{index}] must be an object")
        employee = _clean_text(
            item.get("employee") or item.get("person"),
            f"employees[{index}].employee",
            required=True,
        )
        if employee in seen:
            raise GalleryError(f"duplicate employee in gallery: {employee}")
        seen.add(employee)

        vectors = item.get("embeddings")
        if vectors is None and item.get("embedding") is not None:
            vectors = [item["embedding"]]
        if not isinstance(vectors, list) or not vectors:
            raise GalleryError(f"employee {employee} has no embeddings")
        if len(vectors) > int(max_embeddings_per_employee):
            raise GalleryError(
                f"employee {employee} exceeds max_embeddings_per_employee="
                f"{max_embeddings_per_employee}"
            )

        normalized = []
        for vector_index, vector in enumerate(vectors):
            try:
                clean_vector = norm(vector)
            except GalleryError as exc:
                raise GalleryError(
                    f"invalid embedding for {employee} at index {vector_index}: {exc}"
                ) from exc
            if clean_vector.size != dimension:
                raise GalleryError(
                    f"embedding dimension mismatch for {employee}: "
                    f"received {clean_vector.size}, expected {dimension}"
                )
            normalized.append(clean_vector)

        employee_name = _clean_text(item.get("employee_name"), "employee_name")
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
        embedding_count += len(normalized)

    sanitized = {
        "schema_version": SCHEMA_VERSION,
        "gallery_version": _clean_text(payload.get("gallery_version"), "gallery_version")
        or utc_now(),
        "generated_at": _clean_text(payload.get("generated_at"), "generated_at")
        or utc_now(),
        "model": model,
        "model_version": model_version,
        "dimension": dimension,
        "normalized": True,
        "branch": branch,
        "employees": sanitized_employees,
    }
    sanitized["checksum"] = _gallery_checksum(sanitized)

    metadata = {
        key: value for key, value in sanitized.items() if key != "employees"
    }
    metadata["employee_count"] = len(sanitized_employees)
    metadata["embedding_count"] = embedding_count
    return sanitized, known, metadata


def build_gallery_payload(
    employees,
    *,
    model,
    branch="",
    model_version="",
    gallery_version=None,
):
    employee_rows = []
    dimension = None
    for item in employees:
        vectors = item.get("embeddings") or [item.get("embedding")]
        normalized = [norm(vector) for vector in vectors if vector is not None]
        if not normalized:
            continue
        if dimension is None:
            dimension = int(normalized[0].size)
        employee_rows.append(
            {
                "employee": item["employee"],
                "employee_name": item.get("employee_name", ""),
                "embeddings": [vector.tolist() for vector in normalized],
            }
        )
    if dimension is None:
        raise GalleryError("cannot build a gallery without embeddings")
    return {
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
    )
    _atomic_write_text(
        path,
        json.dumps(sanitized, ensure_ascii=False, indent=2) + "\n",
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
):
    path = Path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GalleryError(f"embedding gallery not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
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
    )
    return known, metadata, sanitized


def gallery_signature(path):
    try:
        stat = Path(path).stat()
    except FileNotFoundError:
        return None
    return stat.st_mtime_ns, stat.st_size


def gallery_status(path, *, max_age_seconds=None):
    path = Path(path)
    if not path.exists():
        return {"available": False, "path": str(path), "error": "gallery not found"}
    try:
        _, metadata, _ = load_gallery(path, require_model_match=False)
        stat = path.stat()
        age_seconds = max(0, int(datetime.now().timestamp() - stat.st_mtime))
        max_age = int(max_age_seconds or 0)
        return {
            "available": True,
            "path": str(path),
            "updated_at": datetime.fromtimestamp(
                stat.st_mtime, timezone.utc
            ).isoformat().replace("+00:00", "Z"),
            "age_seconds": age_seconds,
            "stale": bool(max_age and age_seconds > max_age),
            **metadata,
        }
    except (GalleryError, OSError) as exc:
        return {"available": False, "path": str(path), "error": str(exc)}


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
        json.dumps(current, ensure_ascii=False, indent=2) + "\n",
    )
    return current


def sync_gallery(cfg, gallery_path, status_path, session=None, sleep=None):
    """Compatibility wrapper for callers that have not moved to secure_sync yet.

    The network implementation lives only in secure_sync.py. Import lazily to
    avoid a circular import while secure_sync imports gallery validation helpers.
    """

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
    ):
        self.path = Path(path)
        self.expected_model = expected_model
        self.expected_model_version = expected_model_version
        self.expected_branch = expected_branch
        self.require_model_match = require_model_match
        self.require_model_version_match = require_model_version_match
        self.allow_empty = allow_empty
        self.signature = None
        self.known = []
        self.metadata = {}

    def reload(self, force=False):
        signature = gallery_signature(self.path)
        if not force and signature == self.signature and self.known:
            return self.known, self.metadata, False
        known, metadata, _ = load_gallery(
            self.path,
            expected_model=self.expected_model,
            expected_model_version=self.expected_model_version,
            expected_branch=self.expected_branch,
            require_model_match=self.require_model_match,
            require_model_version_match=self.require_model_version_match,
            allow_empty=self.allow_empty,
        )
        self.known = known
        self.metadata = metadata
        self.signature = gallery_signature(self.path)
        return self.known, self.metadata, True
