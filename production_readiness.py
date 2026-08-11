import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

from model_manifest import (
    default_model_directory,
    is_placeholder,
    resolve_path,
    verify_manifest,
)
from pad import configuration_issues as pad_configuration_issues


FTP_UPLOAD_ONLY_PERMISSIONS = frozenset("elw")


@dataclass(frozen=True)
class ReadinessIssue:
    code: str
    message: str
    severity: str = "blocker"


@dataclass(frozen=True)
class ReadinessReport:
    production_mode: bool
    ready: bool
    issues: tuple[ReadinessIssue, ...]
    model_integrity: dict

    @property
    def blockers(self):
        return tuple(issue for issue in self.issues if issue.severity == "blocker")

    def to_dict(self):
        return {
            "production_mode": self.production_mode,
            "ready": self.ready,
            "issues": [asdict(issue) for issue in self.issues],
            "model_integrity": self.model_integrity,
        }


class ProductionReadinessError(RuntimeError):
    def __init__(self, report):
        self.report = report
        super().__init__(format_report(report))


def _text(value):
    return str(value or "").strip()


def _is_local_url(value):
    parsed = urlparse(_text(value))
    return parsed.hostname in {"localhost", "127.0.0.1", "::1"}


def _https_issue(cfg, key, allow_key, code, label):
    value = _text(cfg.get(key))
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ReadinessIssue(code, f"{label} must be an absolute HTTP(S) URL")
    if (
        parsed.scheme != "https"
        and not _is_local_url(value)
        and not bool(cfg.get(allow_key, False))
    ):
        return ReadinessIssue(code, f"{label} must use HTTPS outside localhost")
    return None


def _admin_auth_configured(cfg):
    username = _text(cfg.get("web_admin_username"))
    password_hash = _text(cfg.get("web_admin_password_hash"))
    session_secret = _text(cfg.get("web_session_secret"))
    return bool(
        username
        and password_hash.startswith("scrypt$")
        and len(session_secret) >= 32
        and not is_placeholder(session_secret)
    )


def _ftp_permission_issues(cfg):
    default_permissions = _text(cfg.get("ftp_permissions") or "elw")
    configured_users = cfg.get("ftp_users")
    rows = []
    issues = []

    if configured_users in (None, {}):
        rows.append(("default FTP user", default_permissions))
    elif not isinstance(configured_users, dict):
        return ["ftp_users must be a JSON object"]
    else:
        for username, item in configured_users.items():
            label = f"FTP user {_text(username) or '<empty>'}"
            if not isinstance(item, dict):
                issues.append(f"{label} configuration must be an object")
                continue
            rows.append((label, _text(item.get("permissions") or default_permissions)))

    for label, permissions in rows:
        if "w" not in permissions:
            issues.append(f"{label} must include the upload permission 'w'")
        unsupported = sorted(set(permissions) - FTP_UPLOAD_ONLY_PERMISSIONS)
        if unsupported:
            issues.append(
                f"{label} grants non-upload permissions: {''.join(unsupported)}; "
                "only e, l, and w are allowed"
            )
    return issues


def check_production_readiness(cfg, root, *, verify_model_files=True):
    root = Path(root)
    production_mode = bool(cfg.get("production_mode", False))
    issues = []

    if not production_mode:
        issues.append(
            ReadinessIssue(
                "production_mode_disabled",
                "production_mode is false; live production safeguards are advisory only",
                severity="warning",
            )
        )

    if not bool(cfg.get("model_license_acknowledged", False)):
        issues.append(
            ReadinessIssue(
                "model_license_not_acknowledged",
                "model_license_acknowledged must be true after the exact model license is verified",
            )
        )
    license_reference = _text(cfg.get("model_license_reference"))
    if is_placeholder(license_reference):
        issues.append(
            ReadinessIssue(
                "model_license_reference_missing",
                "model_license_reference must identify the recorded license or internal approval",
            )
        )

    model_manifest_path = resolve_path(
        root, cfg.get("model_manifest_path"), "model_manifest.json"
    )
    if verify_model_files:
        integrity = verify_manifest(
            model_manifest_path,
            expected_model=cfg.get("model", "buffalo_l"),
            expected_model_version=cfg.get("model_version", ""),
            expected_model_directory=default_model_directory(cfg),
            expected_license_reference=license_reference or None,
            require_complete=bool(cfg.get("model_manifest_require_complete", True)),
        )
        for message in integrity.get("errors", []):
            issues.append(ReadinessIssue("model_integrity_failed", message))
        for message in integrity.get("warnings", []):
            issues.append(ReadinessIssue("model_integrity_warning", message, severity="warning"))
    else:
        integrity = {
            "ok": None,
            "manifest_path": str(model_manifest_path),
            "skipped": True,
        }

    for message in pad_configuration_issues(cfg):
        issues.append(ReadinessIssue("pad_configuration_invalid", message))
    if not bool(cfg.get("pad_required", False)):
        issues.append(
            ReadinessIssue(
                "pad_not_required",
                "pad_required must be true for production facial recognition",
            )
        )
    if _text(cfg.get("pad_provider") or "disabled").lower() == "disabled":
        issues.append(
            ReadinessIssue(
                "pad_provider_disabled",
                "a validated PAD/liveness provider must be configured",
            )
        )
    if not bool(cfg.get("pad_fail_closed", True)):
        issues.append(
            ReadinessIssue(
                "pad_not_fail_closed",
                "pad_fail_closed must be true in production",
            )
        )

    if not _admin_auth_configured(cfg):
        issues.append(
            ReadinessIssue(
                "web_admin_auth_unconfigured",
                "web administration credentials and persistent session secret are not configured",
            )
        )
    if _text(cfg.get("web_bind_host", "127.0.0.1")) not in {
        "127.0.0.1",
        "::1",
        "localhost",
    }:
        issues.append(
            ReadinessIssue(
                "web_not_loopback",
                "web_bind_host must be loopback behind the HTTPS reverse proxy",
            )
        )
    if not bool(cfg.get("web_cookie_secure", True)):
        issues.append(ReadinessIssue("web_cookie_insecure", "web_cookie_secure must be true"))
    if not bool(cfg.get("web_hsts_enabled", True)):
        issues.append(ReadinessIssue("web_hsts_disabled", "web_hsts_enabled must be true"))
    if not bool(cfg.get("https_reverse_proxy_acknowledged", False)):
        issues.append(
            ReadinessIssue(
                "https_proxy_not_acknowledged",
                "set https_reverse_proxy_acknowledged after HTTPS proxy deployment is verified",
            )
        )

    central_issue = _https_issue(
        cfg,
        "central_url",
        "allow_insecure_central_url",
        "central_url_insecure",
        "central_url",
    )
    if central_issue:
        issues.append(central_issue)
    frappe_issue = _https_issue(
        cfg,
        "frappe_url",
        "allow_insecure_frappe_url",
        "frappe_url_insecure",
        "frappe_url",
    )
    if frappe_issue:
        issues.append(frappe_issue)

    ftp_tls_enabled = bool(cfg.get("ftp_tls_enabled", False))
    network_ack = bool(cfg.get("camera_network_isolated_acknowledged", False))
    if not ftp_tls_enabled and not network_ack:
        issues.append(
            ReadinessIssue(
                "camera_transport_unprotected",
                "enable FTPS or acknowledge a verified isolated camera VLAN/VPN",
            )
        )
    if ftp_tls_enabled:
        cert = resolve_path(root, cfg.get("ftp_tls_certfile"), "")
        key = resolve_path(root, cfg.get("ftp_tls_keyfile"), "")
        if not _text(cfg.get("ftp_tls_certfile")) or not cert.is_file():
            issues.append(ReadinessIssue("ftp_tls_cert_missing", f"FTPS certificate unavailable: {cert}"))
        if not _text(cfg.get("ftp_tls_keyfile")) or not key.is_file():
            issues.append(ReadinessIssue("ftp_tls_key_missing", f"FTPS private key unavailable: {key}"))
        if not bool(cfg.get("ftp_tls_control_required", True)):
            issues.append(
                ReadinessIssue(
                    "ftp_tls_control_optional",
                    "ftp_tls_control_required must be true in production",
                )
            )
        if not bool(cfg.get("ftp_tls_data_required", True)):
            issues.append(
                ReadinessIssue(
                    "ftp_tls_data_optional",
                    "ftp_tls_data_required must be true in production",
                )
            )

    if not bool(cfg.get("ftp_staging_enabled", True)):
        issues.append(
            ReadinessIssue(
                "ftp_staging_disabled",
                "ftp_staging_enabled must be true so the watcher cannot observe partial uploads",
            )
        )
    for message in _ftp_permission_issues(cfg):
        issues.append(ReadinessIsssue("ftp_permissions_unsafe", message))

    camera_ids = cfg.get("camera_ids") if isinstance(cfg.get("camera_ids"), dict) else {}
    in_id = _text(camera_ids.get("in"))
    out_id = _text(camera_ids.get("out"))
    if not in_id or not out_id:
        issues.append(
            ReadinessIssue(
                "camera_ids_missing",
                "stable and explicit camera_ids.in and camera_ids.out are required",
            )
        )
    elif in_id == out_id:
        issues.append(
            ReadinessIssue(
                "camera_ids_duplicate",
                "IN and OUT cameras must not use the same camera ID",
            )
        )

    if bool(cfg.get("embedding_export_enabled", False)) and is_placeholder(
        cfg.get("embedding_export_token")
    ):
        issues.append(
            ReadinessIssue(
                "embedding_export_token_missing",
                "embedding_export_enabled requires a non-placeholder token",
            )
        )

    blockers = [issue for issue in issues if issue.severity == "blocker"]
    return ReadinessReport(
        production_mode=production_mode,
        ready=not blockers,
        issues=tuple(issues),
        model_integrity=integrity,
    )


def enforce_production_readiness(cfg, root, *, dry_run=False, verify_model_files=True):
    report = check_production_readiness(
        cfg, root, verify_model_files=verify_model_files
    )
    if bool(cfg.get("production_mode", False)) and not dry_run and report.blockers:
        raise ProductionReadinessError(report)
    return report


def format_report(report):
    lines = [
        f"production_mode={str(report.production_mode).lower()} ready={str(report.ready).lower()}"
    ]
    for issue in report.issues:
        lines.append(f"[{issue.severity}] {issue.code}: {issue.message}")
    return "\n".join(lines)


def load_config(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"missing config: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit("config must contain a JSON object")
    return data


def main():
    parser = argparse.ArgumentParser(description="Check Face Attendance production readiness.")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parent / "config.json",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--skip-model-hash",
        action="store_true",
        help="Skip model-file hashing for a quick configuration-only check.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero on blockers even when production_mode is false.",
    )
    args = parser.parse_args()
    cfg = load_config(args.config)
    report = check_production_readiness(
        cfg,
        args.config.resolve().parent,
        verify_model_files=not args.skip_model_hash,
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2) if args.json else format_report(report))
    if report.blockers and (args.strict or bool(cfg.get("production_mode", False))):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
