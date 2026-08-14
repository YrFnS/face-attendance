"""Leased worker for private Employee Checkin crop attachments.

The attachment worker is intentionally separate from Employee Checkin delivery.
Once a check-in job is marked delivered, any later crop upload failure changes
only the attachment job and never downgrades the attendance delivery.
"""

from __future__ import annotations

import random
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import requests

from attachment_spool import (
    AttachmentSpoolError,
    cleanup_orphaned_spool,
    discard_spooled_crop,
    resolve_spool_root,
    verify_spooled_crop,
)
from erpnext_adapter import (
    ERPNextAdapter,
    ERPNextAdapterConfigurationError,
    ERPNextAdapterError,
    PrivateAttachmentResult,
)


class AttachmentWorkerConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class AttachmentFailureClassification:
    outcome: str
    error_class: str
    retry_after_seconds: float = 0.0
    safe_after_submission: bool = False


@dataclass(frozen=True)
class AttachmentWorkerSettings:
    enabled: bool = False
    poll_seconds: float = 2.0
    batch_size: int = 10
    lease_seconds: int = 120
    heartbeat_seconds: float = 20.0
    max_attempts: int = 6
    retry_base_seconds: float = 10.0
    retry_max_seconds: float = 3600.0
    retry_jitter_fraction: float = 0.2
    max_image_bytes: int = 10 * 1024 * 1024
    queue_max_active_jobs: int = 10000
    queue_min_free_bytes: int = 536870912
    orphan_grace_seconds: int = 3600


def _bool(cfg, key, default, issues):
    value = cfg.get(key, default)
    if not isinstance(value, bool):
        issues.append(f"{key} must be a boolean")
        return default
    return value


def _int(cfg, key, default, minimum, maximum, issues):
    value = cfg.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        issues.append(f"{key} must be an integer")
        return default
    if value < minimum or value > maximum:
        issues.append(f"{key} must be between {minimum} and {maximum}")
        return default
    return value


def _number(cfg, key, default, minimum, maximum, issues):
    value = cfg.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        issues.append(f"{key} must be numeric")
        return float(default)
    value = float(value)
    if value < minimum or value > maximum:
        issues.append(f"{key} must be between {minimum} and {maximum}")
        return float(default)
    return value


def configuration_issues(cfg, *, root=None):
    if not isinstance(cfg, dict):
        return ["attachment worker configuration must be a JSON object"]
    issues = []
    attach_requested = _bool(cfg, "attach_checkin_crop", True, issues)
    enabled = _bool(
        cfg,
        "attachment_worker_enabled",
        False,
        issues,
    )
    _number(
        cfg, "attachment_worker_poll_seconds", 2.0, 0.05, 60.0, issues
    )
    _int(cfg, "attachment_worker_batch_size", 10, 1, 100, issues)
    lease_seconds = _int(
        cfg, "attachment_worker_lease_seconds", 120, 30, 3600, issues
    )
    heartbeat_seconds = _number(
        cfg,
        "attachment_worker_heartbeat_seconds",
        20.0,
        1.0,
        1800.0,
        issues,
    )
    _int(cfg, "attachment_worker_max_attempts", 6, 1, 100, issues)
    retry_base = _number(
        cfg, "attachment_retry_base_seconds", 10.0, 0.1, 3600.0, issues
    )
    retry_max = _number(
        cfg, "attachment_retry_max_seconds", 3600.0, 0.1, 86400.0, issues
    )
    _number(
        cfg,
        "attachment_retry_jitter_fraction",
        0.2,
        0.0,
        1.0,
        issues,
    )
    _int(
        cfg,
        "attachment_max_image_bytes",
        10 * 1024 * 1024,
        1024,
        100 * 1024 * 1024,
        issues,
    )
    _int(
        cfg,
        "attachment_queue_max_active_jobs",
        10000,
        1,
        1_000_000,
        issues,
    )
    _int(
        cfg,
        "attachment_queue_min_free_bytes",
        536870912,
        0,
        10**15,
        issues,
    )
    _int(
        cfg,
        "attachment_orphan_grace_seconds",
        3600,
        60,
        7 * 86400,
        issues,
    )
    spool = cfg.get("attachment_spool_dir", "delivery_attachments")
    if not isinstance(spool, str) or not spool.strip() or spool != spool.strip():
        issues.append("attachment_spool_dir must be a non-empty trimmed string")
    elif root is not None:
        try:
            candidate = Path(spool).expanduser()
            candidate = candidate if candidate.is_absolute() else Path(root) / candidate
            # Inspect the unresolved path so a symlink component cannot be
            # hidden by Path.resolve().
            cursor = candidate
            while True:
                if cursor.exists() and cursor.is_symlink():
                    raise ValueError("path uses a symbolic link")
                if cursor == cursor.parent:
                    break
                cursor = cursor.parent
            candidate.resolve(strict=False)
        except (OSError, RuntimeError, ValueError) as exc:
            issues.append(f"attachment_spool_dir is unsafe: {exc}")
    if heartbeat_seconds >= lease_seconds:
        issues.append(
            "attachment_worker_heartbeat_seconds must be shorter than "
            "attachment_worker_lease_seconds"
        )
    elif heartbeat_seconds > lease_seconds / 2:
        issues.append(
            "attachment_worker_heartbeat_seconds must be at most half of "
            "attachment_worker_lease_seconds"
        )
    if retry_max < retry_base:
        issues.append(
            "attachment_retry_max_seconds must be at least "
            "attachment_retry_base_seconds"
        )
    delivery_mode = str(cfg.get("delivery_mode") or "synchronous").strip().lower()
    if delivery_mode == "outbox":
        delivery_mode = "worker"
    if delivery_mode == "worker" and attach_requested and not enabled:
        issues.append(
            "attachment_worker_enabled must be true when worker delivery "
            "queues private check-in crops"
        )
    return issues


def settings_from_config(cfg, *, root=None):
    issues = configuration_issues(cfg, root=root)
    if issues:
        raise AttachmentWorkerConfigurationError("; ".join(issues))
    return AttachmentWorkerSettings(
        enabled=bool(cfg.get("attachment_worker_enabled", False)),
        poll_seconds=float(cfg.get("attachment_worker_poll_seconds", 2.0)),
        batch_size=int(cfg.get("attachment_worker_batch_size", 10)),
        lease_seconds=int(cfg.get("attachment_worker_lease_seconds", 120)),
        heartbeat_seconds=float(
            cfg.get("attachment_worker_heartbeat_seconds", 20.0)
        ),
        max_attempts=int(cfg.get("attachment_worker_max_attempts", 6)),
        retry_base_seconds=float(
            cfg.get("attachment_retry_base_seconds", 10.0)
        ),
        retry_max_seconds=float(
            cfg.get("attachment_retry_max_seconds", 3600.0)
        ),
        retry_jitter_fraction=float(
            cfg.get("attachment_retry_jitter_fraction", 0.2)
        ),
        max_image_bytes=int(
            cfg.get("attachment_max_image_bytes", 10 * 1024 * 1024)
        ),
        queue_max_active_jobs=int(
            cfg.get("attachment_queue_max_active_jobs", 10000)
        ),
        queue_min_free_bytes=int(
            cfg.get("attachment_queue_min_free_bytes", 536870912)
        ),
        orphan_grace_seconds=int(
            cfg.get("attachment_orphan_grace_seconds", 3600)
        ),
    )


def attachment_capacity_status(state, cfg, root):
    maximum = int(cfg.get("attachment_queue_max_active_jobs", 10000))
    minimum_free = int(cfg.get("attachment_queue_min_free_bytes", 536870912))
    active = int(state.active_attachment_job_count())
    spool_root = resolve_spool_root(root, cfg)
    free = int(shutil.disk_usage(spool_root).free)
    reasons = []
    if active >= maximum:
        reasons.append(
            f"active attachment queue has {active} jobs; limit is {maximum}"
        )
    if minimum_free and free < minimum_free:
        reasons.append(
            f"attachment spool has {free} free bytes; minimum is {minimum_free}"
        )
    return {
        "ok": not reasons,
        "active_jobs": active,
        "max_active_jobs": maximum,
        "free_bytes": free,
        "minimum_free_bytes": minimum_free,
        "reasons": reasons,
    }


def retry_delay_seconds(
    attempt_count,
    *,
    base_seconds,
    maximum_seconds,
    jitter_fraction,
    random_value=None,
):
    attempt = max(1, int(attempt_count))
    base = max(0.1, float(base_seconds))
    maximum = max(base, float(maximum_seconds))
    raw = min(maximum, base * (2 ** min(30, attempt - 1)))
    jitter = min(1.0, max(0.0, float(jitter_fraction)))
    if not jitter:
        return raw
    value = random.random() if random_value is None else float(random_value)
    value = min(1.0, max(0.0, value))
    factor = 1.0 + ((value * 2.0) - 1.0) * jitter
    return min(maximum, max(0.1, raw * factor))


def _retry_after_seconds(response):
    if response is None:
        return 0.0
    value = str(getattr(response, "headers", {}).get("Retry-After", "")).strip()
    if not value:
        return 0.0
    try:
        result = float(value)
    except ValueError:
        return 0.0
    return min(86400.0, max(0.0, result))


def classify_attachment_failure(exc):
    if isinstance(exc, AttachmentSpoolError):
        return AttachmentFailureClassification(
            "permanent", "attachment_source_invalid"
        )
    if isinstance(exc, requests.ConnectTimeout):
        return AttachmentFailureClassification(
            "retryable",
            "attachment_connect_timeout",
            safe_after_submission=True,
        )
    if isinstance(exc, ERPNextAdapterConfigurationError):
        return AttachmentFailureClassification(
            "permanent", "attachment_adapter_configuration"
        )
    if isinstance(exc, requests.HTTPError):
        response = getattr(exc, "response", None)
        status = int(getattr(response, "status_code", 0) or 0)
        if status == 429:
            return AttachmentFailureClassification(
                "retryable",
                "attachment_http_rate_limit",
                retry_after_seconds=_retry_after_seconds(response),
                safe_after_submission=True,
            )
        if status in {400, 401, 403, 404, 405, 409, 413, 415, 422}:
            return AttachmentFailureClassification(
                "permanent", f"attachment_http_{status}"
            )
        return AttachmentFailureClassification(
            "uncertain", f"attachment_http_{status or 'unknown'}_ambiguous"
        )
    if isinstance(exc, requests.ReadTimeout):
        return AttachmentFailureClassification(
            "uncertain", "attachment_read_timeout"
        )
    if isinstance(exc, requests.ConnectionError):
        return AttachmentFailureClassification(
            "uncertain", "attachment_connection_lost"
        )
    if isinstance(exc, ERPNextAdapterError):
        return AttachmentFailureClassification(
            "uncertain", "attachment_adapter_error"
        )
    return AttachmentFailureClassification(
        "uncertain", type(exc).__name__ or "attachment_unexpected_error"
    )


class AttachmentLeaseHeartbeat:
    def __init__(
        self,
        state,
        *,
        attachment_id,
        owner,
        lease_seconds,
        interval_seconds,
    ):
        self.state = state
        self.attachment_id = attachment_id
        self.owner = owner
        self.lease_seconds = lease_seconds
        self.interval_seconds = interval_seconds
        self.error = None
        self._stop = threading.Event()
        self._thread = None

    def _run(self):
        while not self._stop.wait(self.interval_seconds):
            try:
                self.state.renew_attachment_job_lease(
                    self.attachment_id,
                    owner=self.owner,
                    lease_seconds=self.lease_seconds,
                )
            except Exception as exc:
                self.error = exc
                self._stop.set()
                return

    def __enter__(self):
        self._thread = threading.Thread(
            target=self._run,
            name=f"attachment-heartbeat-{self.attachment_id[:12]}",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.interval_seconds + 1.0))
        return False


class AttachmentWorker:
    def __init__(
        self,
        state,
        adapter: ERPNextAdapter,
        settings: AttachmentWorkerSettings,
        *,
        cfg=None,
        root=None,
        owner=None,
        clock=time.time,
        random_source=random.random,
        logger=print,
    ):
        self.state = state
        self.adapter = adapter
        self.settings = settings
        self.cfg = dict(cfg or {})
        self.root = Path(root or Path(__file__).resolve().parent)
        self.clock = clock
        self.random_source = random_source
        self.logger = logger
        self.owner = owner or f"attachment:{uuid.uuid4().hex}"

    def recover(self):
        now = self.clock()
        recovered = self.state.recover_attachment_jobs(
            max_attempts=self.settings.max_attempts,
            now=now,
        )
        try:
            cleanup_orphaned_spool(
                self.state, root=self.root, cfg=self.cfg, now=now
            )
        except Exception as exc:
            self.logger(f"attachment orphan cleanup failed: {exc}")
        # Attached jobs and cancelled jobs no longer need their spool copy when
        # delete_after_success is enabled. This also repairs the crash window
        # after unlinking the file but before recording source_state=deleted.
        # Permanent failures remain retained for later audited dead-letter review.
        try:
            spool_root = resolve_spool_root(self.root, self.cfg)
            for job in self.state.attachment_jobs_for_source_cleanup(limit=500):
                source_path = str(job.get("source_path") or "")
                removed = discard_spooled_crop(
                    source_path, spool_root=spool_root
                )
                # Missing after a confirmed terminal cleanup request is
                # treated as deleted: either this run or a crashed prior run
                # completed the unlink.
                if removed or not Path(source_path).exists():
                    self.state.mark_attachment_source_state(
                        job["attachment_id"],
                        source_state="deleted",
                        now=now,
                    )
        except Exception as exc:
            self.logger(f"terminal attachment cleanup failed: {exc}")
        return recovered

    def run_once(self, *, max_jobs=None):
        self.recover()
        if not self.settings.enabled:
            return {"processed": 0, "outcomes": []}
        limit = self.settings.batch_size if max_jobs is None else int(max_jobs)
        if limit < 1 or limit > 1000:
            raise AttachmentWorkerConfigurationError(
                "max_jobs must be between 1 and 1000"
            )
        processed = 0
        outcomes = []
        while processed < limit:
            job = self.state.claim_next_attachment_job(
                owner=self.owner,
                lease_seconds=self.settings.lease_seconds,
                transport=self.adapter.transport,
                max_attempts=self.settings.max_attempts,
                now=self.clock(),
            )
            if job is None:
                break
            outcomes.append(self._process_job(job))
            processed += 1
        return {"processed": processed, "outcomes": outcomes}

    def _process_job(self, job):
        attachment_id = job["attachment_id"]
        spool_root = resolve_spool_root(self.root, self.cfg)
        try:
            path = verify_spooled_crop(
                job,
                max_bytes=self.settings.max_image_bytes,
                spool_root=spool_root,
            )
        except Exception as exc:
            current = self.state.mark_attachment_job_permanent_failure_by_lease(
                attachment_id,
                owner=self.owner,
                error_class="attachment_source_invalid",
                error=str(exc),
                now=self.clock(),
            )
            try:
                source_path = Path(str(job.get("source_path") or ""))
                source_state = "missing" if not source_path.is_file() else "available"
                self.state.mark_attachment_source_state(
                    attachment_id, source_state=source_state, now=self.clock()
                )
            except Exception:
                pass
            self.logger(
                f"attachment permanent failure id={attachment_id} "
                "class=attachment_source_invalid"
            )
            return current["state"]

        self.state.mark_attachment_submission_started(
            attachment_id,
            owner=self.owner,
            now=self.clock(),
        )
        with AttachmentLeaseHeartbeat(
            self.state,
            attachment_id=attachment_id,
            owner=self.owner,
            lease_seconds=self.settings.lease_seconds,
            interval_seconds=self.settings.heartbeat_seconds,
        ) as heartbeat:
            try:
                result = self.adapter.attach_private_file(
                    job["parent_docname"],
                    path,
                )
                if not isinstance(result, PrivateAttachmentResult):
                    raise ERPNextAdapterError(
                        "ERPNext adapter returned an invalid attachment result"
                    )
            except Exception as exc:
                classification = classify_attachment_failure(exc)
                if heartbeat.error is not None:
                    classification = AttachmentFailureClassification(
                        "uncertain", "attachment_lease_heartbeat_lost"
                    )
                return self._record_failure(job, exc, classification)

        if heartbeat.error is not None:
            current = self.state.mark_attachment_job_uncertain_by_lease(
                attachment_id,
                owner=self.owner,
                error_class="attachment_lease_heartbeat_lost",
                error=str(heartbeat.error),
                now=self.clock(),
            )
            return current["state"]

        current = self.state.mark_attachment_job_attached_by_lease(
            attachment_id,
            owner=self.owner,
            transport=result.transport,
            remote_file_docname=result.file_docname,
            remote_file_url=result.file_url,
            now=self.clock(),
        )
        if bool(current["delete_after_success"]):
            try:
                removed = discard_spooled_crop(
                    current["source_path"], spool_root=spool_root
                )
                if removed or not Path(current["source_path"]).exists():
                    current = self.state.mark_attachment_source_state(
                        attachment_id, source_state="deleted", now=self.clock()
                    )
            except Exception as exc:
                self.logger(
                    f"attachment spool cleanup failed id={attachment_id}: {exc}"
                )
        self.logger(
            f"attachment completed id={attachment_id} "
            f"checkin={current['parent_docname']}"
        )
        return current["state"]

    def _record_failure(self, job, exc, classification):
        attachment_id = job["attachment_id"]
        now = self.clock()
        if classification.outcome == "retryable":
            calculated = retry_delay_seconds(
                job["attempt_count"],
                base_seconds=self.settings.retry_base_seconds,
                maximum_seconds=self.settings.retry_max_seconds,
                jitter_fraction=self.settings.retry_jitter_fraction,
                random_value=self.random_source(),
            )
            delay = max(calculated, classification.retry_after_seconds)
            current = self.state.mark_attachment_job_retry_by_lease(
                attachment_id,
                owner=self.owner,
                error_class=classification.error_class,
                error=str(exc),
                delay_seconds=delay,
                max_attempts=self.settings.max_attempts,
                safe_after_submission=classification.safe_after_submission,
                now=now,
            )
            return current["state"]
        if classification.outcome == "permanent":
            current = self.state.mark_attachment_job_permanent_failure_by_lease(
                attachment_id,
                owner=self.owner,
                error_class=classification.error_class,
                error=str(exc),
                now=now,
            )
            return current["state"]
        current = self.state.mark_attachment_job_uncertain_by_lease(
            attachment_id,
            owner=self.owner,
            error_class=classification.error_class,
            error=str(exc),
            now=now,
        )
        return current["state"]
