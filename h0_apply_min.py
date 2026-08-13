import ast
import inspect
from pathlib import Path

import h0_runtime_actual as runtime
import h0_web_import_actual as web

ROOT = Path(__file__).resolve().parent


def read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def write(path, content):
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def replace_once(path, old, new):
    text = read(path)
    count = text.count(old)
    if count != 1:
        raise SystemExit(
            f"expected exactly one match in {path}, found {count}: {old[:100]!r}"
        )
    write(path, text.replace(old, new, 1))


def replace_function(path, name, function):
    text = read(path)
    tree = ast.parse(text)
    matches = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    ]
    if len(matches) != 1:
        raise SystemExit(f"expected one top-level function {name} in {path}")
    node = matches[0]
    start_line = min([node.lineno] + [item.lineno for item in node.decorator_list])
    lines = text.splitlines(keepends=True)
    start = sum(len(line) for line in lines[:start_line - 1])
    end = sum(len(line) for line in lines[:node.end_lineno])
    source = inspect.getsource(function).rstrip() + "\n\n"
    write(path, text[:start] + source + text[end:])


RUNTIME_IMPORT = """from data_contract import (
    MAX_EMBEDDING_DIMENSION,
    MAX_EMBEDDINGS_PER_EMPLOYEE,
    MAX_GALLERY_EMPLOYEES,
    MAX_TOTAL_EMBEDDINGS,
    bounded_limit,
    strict_int,
    validate_gallery_label,
    validate_token,
    validate_url_path,
)
"""
FACE_IMPORT = """from data_contract import (
    employee_directory,
    employee_filename_token,
    employee_id_from_storage_component,
    filename_token,
    safe_log_message,
    validate_employee_id,
    validate_erp_docname,
    validate_log_type,
)
"""
WEB_IMPORT = """from data_contract import (
    employee_directory,
    validate_employee_id,
    validate_gallery_label,
)
"""
IMPORT_LOCAL_IMPORT = """from data_contract import (
    GalleryError,
    employee_directory,
    safe_log_value,
    validate_employee_id,
    validate_employee_name,
)
"""

replace_once(
    "runtime_policy.py",
    "from model_manifest import is_placeholder\n",
    RUNTIME_IMPORT + "from model_manifest import is_placeholder\n",
)
replace_function("runtime_policy.py", "gallery_policy_issues", runtime.gallery_policy_issues)
replace_function("runtime_policy.py", "effective_gallery_options", runtime.effective_gallery_options)
replace_once(
    "runtime_policy.py",
    "    strict = production_enabled(cfg)\n",
    "    strict = production_enabled(cfg)\n"
    "    endpoint_value = cfg.get(\"embedding_gallery_path\")\n"
    "    validate_url_path(\n"
    "        endpoint_value if endpoint_value not in (None, \"\") else \"/api/faces/embeddings\",\n"
    "        \"embedding_gallery_path\",\n"
    "    )\n",
)
replace_function("runtime_policy.py", "gallery_freshness_status", runtime.gallery_freshness_status)

replace_once(
    "face_attendance.py",
    "from model_runtime import ModelRuntimeError, create_face_analysis\n",
    FACE_IMPORT + "from model_runtime import ModelRuntimeError, create_face_analysis\n",
)
replace_once(
    "face_attendance.py",
    '    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}"\n',
    '    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} " + safe_log_message(message)\n',
)
replace_once(
    "face_attendance.py",
    '    employee_part = employee or "unknown"\n',
    '    reason = filename_token(reason, "rejection reason")\n'
    '    employee_part = employee_filename_token(employee) if employee else "unknown"\n',
)
replace_once(
    "face_attendance.py",
    '    path = folder / f"{stamp}_{employee}_{score:.3f}.jpg"\n',
    '    employee = validate_employee_id(employee)\n'
    '    path = folder / f"{stamp}_{employee_filename_token(employee)}_{score:.3f}.jpg"\n',
)
replace_once(
    "face_attendance.py",
    '        if not employee_dir.is_dir():\n            continue\n        vectors = []\n',
    '        if employee_dir.is_symlink():\n'
    '            raise SystemExit("Enrollment directory must not be a symbolic link")\n'
    '        if not employee_dir.is_dir():\n'
    '            continue\n'
    '        employee_id = employee_id_from_storage_component(employee_dir.name)\n'
    '        vectors = []\n',
)
replace_once(
    "face_attendance.py",
    '                    "employee": employee_dir.name,\n',
    '                    "employee": employee_id,\n',
)
replace_once(
    "face_attendance.py",
    '    app = face_app(cfg=cfg)\n    out_dir = FACES / employee\n',
    '    employee = validate_employee_id(employee)\n'
    '    app = face_app(cfg=cfg)\n'
    '    out_dir = employee_directory(FACES, employee)\n',
)
for name in ("create_checkin_api", "create_checkin_bench", "create_checkin"):
    marker = f"def {name}(employee, log_type, image_path=None):\n"
    replace_once(
        "face_attendance.py",
        marker,
        marker + "    employee = validate_employee_id(employee)\n"
        "    log_type = validate_log_type(log_type)\n",
    )
replace_once(
    "face_attendance.py",
    '    log_type = log_type or cfg["log_type"]\n',
    '    employee = validate_employee_id(employee)\n'
    '    log_type = validate_log_type(log_type or cfg["log_type"])\n',
)
replace_once(
    "face_attendance.py",
    '        return cfg["log_type"]\n',
    '        return validate_log_type(cfg["log_type"])\n',
)
replace_once(
    "face_attendance.py",
    '    return cfg.get("folder_log_types", {}).get(folder, cfg["log_type"])\n',
    '    return validate_log_type(\n'
    '        cfg.get("folder_log_types", {}).get(folder, cfg["log_type"])\n'
    '    )\n',
)
replace_once(
    "face_attendance.py",
    "        seen_this_image.add(employee)\n",
    "        employee = validate_employee_id(employee)\n"
    "        seen_this_image.add(employee)\n",
)

replace_once("web_admin.py", "from werkzeug.utils import secure_filename\n\n", "")
replace_once(
    "web_admin.py",
    "from embedding_gallery import GalleryError, gallery_status, load_gallery, read_sync_status\n",
    WEB_IMPORT + "from embedding_gallery import GalleryError, gallery_status, load_gallery, read_sync_status\n",
)
replace_function("web_admin.py", "upload", web.upload)
replace_function("web_admin.py", "export_embeddings", web.export_embeddings)

replace_once(
    "import_local_faces.py",
    "import requests\n\n",
    "import requests\n\n" + IMPORT_LOCAL_IMPORT,
)
replace_function("import_local_faces.py", "copy_faces", web.copy_faces)

replace_once(
    "config.example.json",
    '  "max_embeddings_per_employee": 50,\n',
    '  "max_embeddings_per_employee": 50,\n'
    '  "max_gallery_embeddings": 500000,\n'
    '  "max_embedding_dimension": 4096,\n',
)
replace_once(
    ".github/workflows/tests.yml",
    "            test_embedding_gallery.py \\\n",
    "            test_embedding_gallery.py \\\n"
    "            test_gallery_contract.py \\\n",
)
replace_once(
    "docs/attendance-platform-plan.md",
    "- [ ] `H0-07` Constrain and encode employee IDs and all gallery string/numeric fields before filesystem, URL, log, or ERP use. Add path-traversal, length, character, dimension, and count tests.",
    "- [x] `H0-07` Constrain and encode employee IDs and all gallery string/numeric fields before filesystem, URL, log, or ERP use. Add path-traversal, length, character, dimension, and count tests.",
)

for path in (
    "data_contract.py", "embedding_gallery.py", "runtime_policy.py",
    "face_attendance.py", "web_admin.py", "import_local_faces.py",
    "test_gallery_contract.py",
):
    ast.parse(read(path), filename=path)
