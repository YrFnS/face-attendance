import argparse
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = 1
PLACEHOLDERS = {"", "CHANGE_ME", "REPLACE_ME", "CHANGEME", "TODO"}


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def is_placeholder(value):
    return str(value or "").strip().upper() in PLACEHOLDERS


def resolve_path(root, value, default):
    path = Path(value or default).expanduser()
    return path if path.is_absolute() else Path(root) / path


def default_model_directory(cfg, root=None):
    model = str(cfg.get("model") or "buffalo_l").strip()
    configured = str(cfg.get("model_directory") or "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute() and root is not None:
            path = Path(root) / path
        return path
    return Path.home() / ".insightface" / "models" / model


def insightface_root_for_model_directory(model_directory, model):
    model = str(model or "").strip()
    if is_placeholder(model):
        raise ValueError(
            "model must be a non-placeholder value before binding InsightFace"
        )
    configured = Path(model_directory).expanduser()
    if configured.is_symlink():
        raise ValueError("model_directory must not be a symbolic link")
    directory = configured.resolve()
    if directory.name != model or directory.parent.name != "models":
        raise ValueError(
            "model_directory must use InsightFace's root/models/<model> layout; "
            f"received {directory} for model {model!r}"
        )
    return directory.parent.parent


def runtime_model_binding(cfg, root=None):
    model = str(cfg.get("model") or "").strip()
    if is_placeholder(model):
        raise ValueError("model must be a non-placeholder value")
    model_directory = default_model_directory(cfg, root).expanduser().resolve()
    insightface_root = insightface_root_for_model_directory(
        model_directory, model
    )
    return {
        "model": model,
        "model_version": str(cfg.get("model_version") or "").strip(),
        "model_directory": str(model_directory),
        "insightface_root": str(insightface_root),
    }


def sha256_file(path, chunk_size=1024 * 1024):
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def _model_files(model_directory):
    model_directory = Path(model_directory).expanduser().resolve()
    files = []
    for path in sorted(model_directory.rglob("*")):
        if (
            not path.is_file()
            or path.is_symlink()
            or path.name.startswith(".")
        ):
            continue
        relative = path.relative_to(model_directory).as_posix()
        digest, size = sha256_file(path)
        files.append(
            {"path": relative, "sha256": digest, "size": size}
        )
    if not files:
        raise ValueError(
            f"no model files found under {model_directory}"
        )
    return files


def build_manifest(
    *,
    model_directory,
    model,
    model_version="",
    license_reference="",
):
    model = str(model or "").strip()
    if is_placeholder(model):
        raise ValueError("a non-placeholder model name is required")
    model_directory = Path(model_directory).expanduser().resolve()
    insightface_root_for_model_directory(model_directory, model)
    reference = str(license_reference or "").strip()
    if is_placeholder(reference):
        raise ValueError(
            "a non-placeholder license reference is required"
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "model": model,
        "model_version": str(model_version or "").strip(),
        "license_reference": reference,
        "model_directory": str(model_directory),
        "files": _model_files(model_directory),
    }


def write_manifest_atomic(path, manifest):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                manifest, handle, ensure_ascii=False, indent=2
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
        os.chmod(path, 0o600)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def verify_manifest(
    manifest_path,
    *,
    expected_model=None,
    expected_model_version=None,
    expected_model_directory=None,
    expected_license_reference=None,
    require_complete=True,
    verify_files=True,
):
    manifest_path = Path(manifest_path)
    errors = []
    warnings = []
    try:
        manifest = json.loads(
            manifest_path.read_text(encoding="utf-8")
        )
    except FileNotFoundError:
        return {
            "ok": False,
            "errors": [
                f"model manifest not found: {manifest_path}"
            ],
            "warnings": [],
            "manifest_path": str(manifest_path),
            "hashes_verified": bool(verify_files),
        }
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "errors": [
                f"could not read model manifest {manifest_path}: {exc}"
            ],
            "warnings": [],
            "manifest_path": str(manifest_path),
            "hashes_verified": bool(verify_files),
        }

    if not isinstance(manifest, dict):
        return {
            "ok": False,
            "errors": [
                "model manifest must be a JSON object"
            ],
            "warnings": [],
            "manifest_path": str(manifest_path),
            "hashes_verified": bool(verify_files),
        }

    if manifest.get("schema_version") != SCHEMA_VERSION:
        errors.append(
            "unsupported model manifest schema "
            f"{manifest.get('schema_version')!r}; "
            f"expected {SCHEMA_VERSION}"
        )

    model = str(manifest.get("model") or "").strip()
    version = str(
        manifest.get("model_version") or ""
    ).strip()
    reference = str(
        manifest.get("license_reference") or ""
    ).strip()
    if is_placeholder(model):
        errors.append(
            "model manifest has no usable model name"
        )
    if expected_model and model != str(expected_model).strip():
        errors.append(
            f"model manifest names {model!r}; "
            f"expected {expected_model!r}"
        )
    if (
        expected_model_version is not None
        and version
        != str(expected_model_version or "").strip()
    ):
        errors.append(
            f"model manifest version {version!r}; expected "
            f"{str(expected_model_version or '').strip()!r}"
        )
    if is_placeholder(reference):
        errors.append(
            "model manifest has no usable license_reference"
        )
    if (
        expected_license_reference
        and reference != str(expected_license_reference).strip()
    ):
        errors.append(
            "model manifest license_reference does not match config"
        )

    directory_value = str(
        manifest.get("model_directory") or ""
    ).strip()
    insightface_root = ""
    if not directory_value:
        errors.append(
            "model manifest has no model_directory"
        )
        model_directory = None
    else:
        configured_directory = Path(
            directory_value
        ).expanduser()
        if configured_directory.is_symlink():
            errors.append(
                "model manifest directory must not be a symbolic link"
            )
        model_directory = configured_directory.resolve()
        if expected_model_directory:
            expected = Path(
                expected_model_directory
            ).expanduser().resolve()
            if model_directory != expected:
                errors.append(
                    "model manifest directory "
                    f"{model_directory} does not match "
                    f"configured directory {expected}"
                )
        try:
            insightface_root = str(
                insightface_root_for_model_directory(
                    model_directory, model
                )
            )
        except ValueError as exc:
            errors.append(str(exc))
        if (
            not model_directory.exists()
            or not model_directory.is_dir()
        ):
            errors.append(
                f"model directory is unavailable: {model_directory}"
            )

    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        errors.append(
            "model manifest has no files"
        )
        entries = []

    listed = set()
    verified_count = 0
    if model_directory and model_directory.is_dir():
        for index, item in enumerate(entries):
            if not isinstance(item, dict):
                errors.append(
                    f"files[{index}] must be an object"
                )
                continue
            relative = (
                str(item.get("path") or "")
                .strip()
                .replace("\\", "/")
            )
            if (
                not relative
                or relative.startswith("/")
                or ".." in Path(relative).parts
            ):
                errors.append(
                    f"files[{index}] has an unsafe path"
                )
                continue
            if relative in listed:
                errors.append(
                    "duplicate model file in manifest: "
                    f"{relative}"
                )
                continue
            listed.add(relative)
            expected_hash = (
                str(item.get("sha256") or "")
                .strip()
                .lower()
            )
            try:
                expected_size = int(item.get("size"))
            except (TypeError, ValueError):
                errors.append(
                    f"invalid size for model file {relative}"
                )
                continue
            if expected_size < 0:
                errors.append(
                    f"invalid size for model file {relative}"
                )
                continue
            if (
                len(expected_hash) != 64
                or any(
                    char not in "0123456789abcdef"
                    for char in expected_hash
                )
            ):
                errors.append(
                    f"invalid SHA-256 for model file {relative}"
                )
                continue
            path = (
                model_directory / relative
            ).resolve()
            try:
                path.relative_to(model_directory)
            except ValueError:
                errors.append(
                    "model file escapes model directory: "
                    f"{relative}"
                )
                continue
            if not path.is_file() or path.is_symlink():
                errors.append(
                    f"model file missing or unsafe: {relative}"
                )
                continue
            if verify_files:
                actual_hash, actual_size = sha256_file(path)
                if actual_size != expected_size:
                    errors.append(
                        "model file size mismatch for "
                        f"{relative}: {actual_size} != "
                        f"{expected_size}"
                    )
                if actual_hash != expected_hash:
                    errors.append(
                        "model file SHA-256 mismatch for "
                        f"{relative}"
                    )
                if (
                    actual_size == expected_size
                    and actual_hash == expected_hash
                ):
                    verified_count += 1
            elif path.stat().st_size != expected_size:
                errors.append(
                    "model file size mismatch for "
                    f"{relative}: {path.stat().st_size} != "
                    f"{expected_size}"
                )

        if require_complete:
            actual = {
                path.relative_to(
                    model_directory
                ).as_posix()
                for path in model_directory.rglob("*")
                if path.is_file()
                and not path.is_symlink()
                and not path.name.startswith(".")
            }
            unlisted = sorted(actual - listed)
            missing = sorted(listed - actual)
            if unlisted:
                errors.append(
                    "unlisted model files are present: "
                    + ", ".join(unlisted[:10])
                )
            if missing:
                errors.append(
                    "listed model files are absent: "
                    + ", ".join(missing[:10])
                )

    if entries and not any(
        str(item.get("path", "")).lower().endswith(".onnx")
        for item in entries
        if isinstance(item, dict)
    ):
        warnings.append(
            "model manifest contains no ONNX files"
        )

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "manifest_path": str(manifest_path),
        "model": model,
        "model_version": version,
        "license_reference": reference,
        "model_directory": (
            str(model_directory)
            if model_directory
            else ""
        ),
        "insightface_root": insightface_root,
        "file_count": len(entries),
        "verified_file_count": verified_count,
        "hashes_verified": bool(verify_files),
    }


def load_config(path):
    try:
        data = json.loads(
            Path(path).read_text(encoding="utf-8")
        )
    except FileNotFoundError as exc:
        raise SystemExit(
            f"missing config: {path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"invalid JSON in {path}: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise SystemExit(
            "config must contain a JSON object"
        )
    return data


def main():
    parser = argparse.ArgumentParser(
        description="Create or verify a pinned face-model manifest."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parent
        / "config.json",
    )
    sub = parser.add_subparsers(
        dest="command", required=True
    )

    create = sub.add_parser("create")
    create.add_argument("--model-dir", type=Path)
    create.add_argument("--output", type=Path)
    create.add_argument("--license-reference")

    verify = sub.add_parser("verify")
    verify.add_argument("--manifest", type=Path)
    verify.add_argument(
        "--skip-hash",
        action="store_true",
        help="Validate metadata, paths, file sizes, and inventory without hashing files.",
    )
    args = parser.parse_args()
    cfg = load_config(args.config)
    root = args.config.resolve().parent
    model_dir = (
        args.model_dir
        if args.command == "create"
        and args.model_dir
        else default_model_directory(cfg, root)
    )
    manifest_path = (
        args.output
        if args.command == "create"
        and args.output
        else args.manifest
        if args.command == "verify"
        and args.manifest
        else resolve_path(
            root,
            cfg.get("model_manifest_path"),
            "model_manifest.json",
        )
    )

    if args.command == "create":
        reference = (
            args.license_reference
            or cfg.get("model_license_reference")
        )
        try:
            manifest = build_manifest(
                model_directory=model_dir,
                model=cfg.get("model"),
                model_version=cfg.get(
                    "model_version", ""
                ),
                license_reference=reference,
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        write_manifest_atomic(
            manifest_path, manifest
        )
        print(
            f"wrote {manifest_path}: "
            f"{len(manifest['files'])} file(s)"
        )
        return

    result = verify_manifest(
        manifest_path,
        expected_model=cfg.get("model"),
        expected_model_version=cfg.get(
            "model_version"
        ),
        expected_model_directory=default_model_directory(
            cfg, root
        ),
        expected_license_reference=cfg.get(
            "model_license_reference"
        ),
        require_complete=bool(
            cfg.get(
                "model_manifest_require_complete",
                True,
            )
        ),
        verify_files=not args.skip_hash,
    )
    print(
        json.dumps(
            result, ensure_ascii=False, indent=2
        )
    )
    if not result["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
