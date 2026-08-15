"""Single-node leased ERPNext delivery worker.

P2-03 drains durable ``delivery_jobs`` created transactionally with accepted
recognition decisions. P2-04 permits ambiguous post-submit replay only after the
job is immutably bound to a live, authenticated ERPNext capability proving the
database-unique delivery-ID contract. Unverified ambiguity remains ``uncertain``.
"""

from __future__ import annotations

import argparse
import random
import shutil
import signal
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import requests

from attachment_service import (
    AttachmentWorker,
    configuration_issues as attachment_configuration_issues,
    settings_from_config as attachment_settings_from_config,
)
from erpnext_adapter import (
    ERPNextAdapter,
    ERPNextAdapterConfigurationError,
    ERPNextAdapterContractError,
    ERPNextAdapterConflictError,
    ERPNextAdapterError,
    EmployeeCheckinRequest,
    EmployeeCheckinResult,
    build_erpnext_adapter,
    select_erpnext_transport,
)
from erpnext_idempotency import (
    ERPNextIdempotencyCapability,
    ERPNextIdempotencyCapabilityError,
    ERPNextIdempotencyConflictError,
    idempotency_configuration_issues,
)
from runtime_state import RuntimeState, resolve_runtime_path
from secret_store import ConfigLoadError, load_runtime_config


ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"


class DeliveryWorkerConfigurationError(ValueError):
    pass


class SafeRetryableDeliveryError(RuntimeError):
    """A failure known to have happened before a remote commit was possible."""

    def __init__(self, message, *, retry_after_seconds=0):
        self.retry_after_seconds = float(retry_after_seconds or 0)
        super().__init__(message)


@dataclass(frozen=True)
class DeliveryFailureClassification:
    outcome: str
    error_class: str
    retry_after_seconds: float = 0.0
    safe_after_submission: bool = False


@dataclass(frozen=True)
class DeliveryWorkerSettings:
    mode: str
    enabled: bool
    poll_seconds: float
    batch_size: int
    lease_seconds: int
    heartbeat_seconds: float
    max_attempts: int
    retry_base_seconds: float
    retry_max_seconds: float
    retry_jitter_fraction: float
    queue_max_active_jobs: int
    queue_min_free_bytes: int
    idempotency_required: bool
    idempotency_probe_cache_seconds: float


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


def delivery_mode(cfg):
    value = str(cfg.get("delivery_mode") or "synchronous").strip().lower()
    if value == "outbox":
        value = "worker"
    return value


def configuration_issues(cfg):
    if not isinstance(cfg, dict):
        return ["delivery worker configuration must be a JSON object"]
    issues = []
    mode = delivery_mode(cfg)
    if mode not in {"synchronous", "worker"}:
        issues.append("delivery_mode must be synchronous or worker")
        mode = "synchronous"
    enabled = _bool(cfg, "delivery_worker_enabled", False, issues)
    poll_seconds = _number(
        cfg, "delivery_worker_poll_seconds", 1.0, 0.05, 60.0, issues
    )
    batch_size = _int(cfg, "delivery_worker_batch_size", 10, 1, 100, issues)
    lease_seconds = _int(
        cfg, "delivery_worker_lease_seconds", 120, 30, 3600, issues
    )
    heartbeat_seconds = _number(
        cfg, "delivery_worker_heartbeat_seconds", 20.0, 1.0, 1800.0, issues
    )
    _int(cfg, "delivery_worker_max_attempts", 6, 1, 100, issues)
    retry_base = _number(
        cfg, "delivery_retry_base_seconds", 5.0, 0.1, 3600.0, issues
    )
    retry_max = _number(
        cfg, "delivery_retry_max_seconds", 900.0, 0.1, 86400.0, issues
    )
    _number(
        cfg, "delivery_retry_jitter_fraction", 0.2, 0.0, 1.0, issues
    )
    _int(
        cfg,
        "delivery_queue_max_active_jobs",
        10000,
        1,
        1_000_000,
        issues,
    )
    _int(
        cfg,
        "delivery_queue_min_free_bytes",
        1_073_741_824,
        0,
        10**15,
        issues,
    )
    if heartbeat_seconds >= lease_seconds:
        issues.append(
            "delivery_worker_heartbeat_seconds must be shorter than "
            "delivery_worker_lease_seconds"
        )
    elif heartbeat_seconds > lease_seconds / 2:
        issues.append(
            "delivery_worker_heartbeat_seconds must be at most half of "
            "delivery_worker_lease_seconds"
        )
    if retry_max < retry_base:
        issues.append(
            "delivery_retry_max_seconds must be at least "
            "delivery_retry_base_seconds"
        )
    if mode == "worker" and not enabled:
        issues.append(
            "delivery_worker_enabled must be true when delivery_mode is worker"
        )
    issues.extend(attachment_configuration_issues(cfg, root=ROOT))
    issues.extend(idempotency_configuration_issues(cfg))
    return issues


def settings_from_config(cfg):
    issues = configuration_issues(cfg)
    if issues:
        raise DeliveryWorkerConfigurationError("; ".join(issues))
    return DeliveryWorkerSettings(
        mode=delivery_mode(cfg),
        enabled=bool(cfg.get("delivery_worker_enabled", False)),
        poll_seconds=float(cfg.get("delivery_worker_poll_seconds", 1.0)),
        batch_size=int(cfg.get("delivery_worker_batch_size", 10)),
        lease_seconds=int(cfg.get("delivery_worker_lease_seconds", 120)),
        heartbeat_seconds=float(
            cfg.get("delivery_worker_heartbeat_seconds", 20.0)
        ),
        max_attempts=int(cfg.get("delivery_worker_max_attempts", 6)),
        retry_base_seconds=float(cfg.get("delivery_retry_base_seconds", 5.0)),
        retry_max_seconds=float(cfg.get("delivery_retry_max_seconds", 900.0)),
        retry_jitter_fraction=float(
            cfg.get("delivery_retry_jitter_fraction", 0.2)
        ),
        queue_max_active_jobs=int(
            cfg.get("delivery_queue_max_active_jobs", 10000)
        ),
        queue_min_free_bytes=int(
            cfg.get("delivery_queue_min_free_bytes", 1_073_741_824)
        ),
        idempotency_required=bool(
            cfg.get("erpnext_idempotency_required", False)
        ),
        idempotency_probe_cache_seconds=float(
            cfg.get("erpnext_idempotency_probe_cache_seconds", 300)
        ),
    )


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


def classify_delivery_failure(exc, *, idempotency_verified=False):
    idempotency_verified = bool(idempotency_verified)
    if isinstance(exc, SafeRetryableDeliveryError):
        return DeliveryFailureClassification(
            "retryable",
            "safe_pre_submission_failure",
            retry_after_seconds=max(0.0, exc.retry_after_seconds),
            safe_after_submission=True,
        )
    if isinstance(exc, requests.ConnectTimeout):
        return DeliveryFailureClassification(
            "retryable",
            "connect_timeout",
            safe_after_submission=True,
        )
    if isinstance(exc, ERPNextAdapterConflictError):
        return DeliveryFailureClassification(
            "permanent",
            "erpnext_idempotency_conflict",
        )
    if isinstance(exc, ERPNextAdapterContractError):
        return DeliveryFailureClassification(
            "permanent",
            "erpnext_idempotency_contract",
        )
    if isinstance(exc, ERPNextAdapterConfigurationError):
        return DeliveryFailureClassification(
            "permanent",
            "adapter_configuration",
        )
    if isinstance(exc, FileNotFoundError):
        return DeliveryFailureClassification(
            "permanent",
            "local_file_missing",
        )
    if isinstance(exc, requests.HTTPError):
        response = getattr(exc, "response", None)
        status = int(getattr(response, "status_code", 0) or 0)
        if status == 429:
            return DeliveryFailureClassification(
                "retryable",
                "http_rate_limit",
                retry_after_seconds=_retry_after_seconds(response),
                safe_after_submission=True,
            )
        if status in {400, 401, 403, 404, 405, 409, 413, 415, 422}:
            return DeliveryFailureClassification(
                "permanent",
                f"http_{status}",
            )
        if idempotency_verified:
            return DeliveryFailureClassification(
                "retryable",
                f"http_{status or 'unknown'}_idempotent_replay",
                safe_after_submission=True,
            )
        return DeliveryFailureClassification(
            "uncertain",
            f"http_{status or 'unknown'}_ambiguous",
        )
    if isinstance(exc, requests.ReadTimeout):
        if idempotency_verified:
            return DeliveryFailureClassification(
                "retryable",
                "read_timeout_idempotent_replay",
                safe_after_submission=True,
            )
        return DeliveryFailureClassification("uncertain", "read_timeout")
    if isinstance(exc, requests.ConnectionError):
        if idempotency_verified:
            return DeliveryFailureClassification(
                "retryable",
                "connection_lost_idempotent_replay",
                safe_after_submission=True,
            )
        return DeliveryFailureClassification("uncertain", "connection_lost")
    if isinstance(exc, ERPNextAdapterError):
        if idempotency_verified:
            return DeliveryFailureClassification(
                "retryable",
                "adapter_response_idempotent_replay",
                safe_after_submission=True,
            )
        return DeliveryFailureClassification("uncertain", "adapter_error")
    if idempotency_verified:
        return DeliveryFailureClassification(
            "retryable",
            "unexpected_idempotent_replay",
            safe_after_submission=True,
        )
    return DeliveryFailureClassification(
        "uncertain",
        type(exc).__name__ or "unexpected_delivery_error",
    )


def delivery_capacity_status(state, cfg):
    max_active = int(cfg.get("delivery_queue_max_active_jobs", 10000))
    min_free = int(cfg.get("delivery_queue_min_free_bytes", 1_073_741_824))
    active = int(state.active_delivery_job_count())
    database_parent = Path(state.path).resolve().parent
    free = int(shutil.disk_usage(database_parent).free)
    reasons = []
    if active >= max_active:
        reasons.append(
            f"active delivery queue has {active} jobs; limit is {max_active}"
        )
    if min_free and free < min_free:
        reasons.append(
            f"delivery database filesystem has {free} free bytes; "
            f"minimum is {min_free}"
        )
    return {
        "ok": not reasons,
        "active_jobs": active,
        "max_active_jobs": max_active,
        "free_bytes": free,
        "minimum_free_bytes": min_free,
        "reasons": reasons,
    }


class LeaseHeartbeat:
    def __init__(
        self,
        state,
        *,
        delivery_id,
        owner,
        lease_seconds,
        interval_seconds,
    ):
        self.state = state
        self.delivery_id = delivery_id
        self.owner = owner
        self.lease_seconds = lease_seconds
        self.interval_seconds = interval_seconds
        self.error = None
        self._stop = threading.Event()
        self._thread = None

    def _run(self):
        while not self._stop.wait(self.interval_seconds):
            try:
                self.state.renew_delivery_job_lease(
                    self.delivery_id,
                    owner=self.owner,
                    lease_seconds=self.lease_seconds,
                )
            except Exception as exc:  # recovery classifies the remote boundary
                self.error = exc
                self._stop.set()
                return

    def __enter__(self):
        self._thread = threading.Thread(
            target=self._run,
            name=f"delivery-heartbeat-{self.delivery_id[:12]}",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.interval_seconds + 1.0))
        return False


class DeliveryWorker:
    def __init__(
        self,
        state,
        adapter: ERPNextAdapter,
        settings: DeliveryWorkerSettings,
        *,
        owner=None,
        clock=time.time,
        sleep=time.sleep,
        random_source=random.random,
        logger=print,
        attachment_settings=None,
        attachment_cfg=None,
        attachment_root=None,
    ):
        self.state = state
        self.adapter = adapter
        self.settings = settings
        self.clock = clock
        self.sleep = sleep
        self.random_source = random_source
        self.logger = logger
        self._idempotency_capability = None
        self._idempotency_verified_at = 0.0
        host = socket.gethostname().strip() or "unknown-host"
        self.owner = owner or f"delivery:{host}:{uuid.uuid4().hex}"
        self.delivery_enabled = settings.mode == "worker" and settings.enabled
        self.attachment_worker = None
        if attachment_settings is not None and attachment_settings.enabled:
            self.attachment_worker = AttachmentWorker(
                state,
                adapter,
                attachment_settings,
                owner=f"{self.owner}:attachment",
                clock=clock,
                random_source=random_source,
                logger=logger,
                cfg=attachment_cfg,
                root=attachment_root,
            )

    def _ensure_idempotency_contract(self, *, force=False):
        if not self.settings.idempotency_required:
            return None
        now = float(self.clock())
        if (
            not force
            and isinstance(
                self._idempotency_capability, ERPNextIdempotencyCapability
            )
            and now - self._idempotency_verified_at
            < self.settings.idempotency_probe_cache_seconds
        ):
            return self._idempotency_capability
        verifier = getattr(self.adapter, "verify_idempotency_contract", None)
        if not callable(verifier):
            raise DeliveryWorkerConfigurationError(
                "ERPNext adapter cannot verify the required idempotency contract"
            )
        # The worker cache owns the refresh interval. Once it expires, force
        # the adapter to perform a new authenticated probe rather than returning
        # its own older in-memory capability.
        capability = verifier(force=True)
        if not isinstance(capability, ERPNextIdempotencyCapability):
            raise DeliveryWorkerConfigurationError(
                "ERPNext adapter did not return a verified idempotency capability"
            )
        self._idempotency_capability = capability
        self._idempotency_verified_at = now
        return capability

    def recover(self):
        outcomes = {"delivery": [], "attachments": []}
        if self.delivery_enabled:
            outcomes["delivery"] = self.state.recover_expired_delivery_job_leases(
                max_attempts=self.settings.max_attempts,
                now=self.clock(),
            )
        if self.attachment_worker is not None:
            outcomes["attachments"] = self.attachment_worker.recover()
        return outcomes

    def run_once(self, *, max_jobs=None):
        # Local lease recovery must remain available during an ERPNext outage.
        # The live capability is required before a new job can be claimed.
        self.recover()
        if self.delivery_enabled:
            self._ensure_idempotency_contract()
        default_limit = 0
        if self.delivery_enabled:
            default_limit += self.settings.batch_size
        if self.attachment_worker is not None:
            default_limit += self.attachment_worker.settings.batch_size
        limit = default_limit if max_jobs is None else int(max_jobs)
        if limit < 1 or limit > 1000:
            raise DeliveryWorkerConfigurationError(
                "max_jobs must be between 1 and 1000"
            )
        delivery_processed = 0
        attachment_processed = 0
        outcomes = []
        if self.delivery_enabled:
            delivery_limit = min(limit, self.settings.batch_size)
            while delivery_processed < delivery_limit:
                job = self.state.claim_next_delivery_job(
                    owner=self.owner,
                    lease_seconds=self.settings.lease_seconds,
                    transport=self.adapter.transport,
                    max_attempts=self.settings.max_attempts,
                    now=self.clock(),
                )
                if job is None:
                    break
                outcomes.append(self._process_job(job))
                delivery_processed += 1
        remaining = limit - delivery_processed
        if self.attachment_worker is not None and remaining > 0:
            attachment_limit = min(
                remaining,
                self.attachment_worker.settings.batch_size,
            )
            attached = self.attachment_worker.run_once(
                max_jobs=attachment_limit
            )
            attachment_processed = int(attached["processed"])
            outcomes.extend(attached["outcomes"])
        return {
            "processed": delivery_processed + attachment_processed,
            "delivery_processed": delivery_processed,
            "attachment_processed": attachment_processed,
            "outcomes": outcomes,
        }

    def _process_job(self, job):
        delivery_id = job["delivery_id"]
        capability = self._ensure_idempotency_contract()
        if capability is not None:
            try:
                job = self.state.bind_delivery_job_idempotency_contract_by_lease(
                    delivery_id,
                    owner=self.owner,
                    capability=capability,
                    now=self.clock(),
                )
            except (
                ERPNextIdempotencyCapabilityError,
                ERPNextIdempotencyConflictError,
            ) as exc:
                current = self.state.mark_delivery_job_permanent_failure_by_lease(
                    delivery_id,
                    owner=self.owner,
                    error_class="erpnext_idempotency_binding",
                    error=str(exc),
                    now=self.clock(),
                )
                self.logger(
                    f"delivery permanent failure id={delivery_id} "
                    "class=erpnext_idempotency_binding"
                )
                return current["state"]

        try:
            metadata = (
                {
                    "delivery_id": job["delivery_id"],
                    "event_id": job["event_id"],
                    "decision_id": job["decision_id"],
                    "camera_id": job["camera_id"],
                    "branch": job["branch"],
                    "delivery_contract_version": job[
                        "delivery_contract_version"
                    ],
                }
                if capability is not None
                else {}
            )
            request = EmployeeCheckinRequest.build(
                job["employee"],
                job["log_type"],
                job["effective_at"],
                **metadata,
            )
        except Exception as exc:
            current = self.state.mark_delivery_job_permanent_failure_by_lease(
                delivery_id,
                owner=self.owner,
                error_class="invalid_delivery_payload",
                error=str(exc),
                now=self.clock(),
            )
            self.logger(
                f"delivery permanent failure id={delivery_id} "
                f"class=invalid_delivery_payload"
            )
            return current["state"]

        try:
            self.state.mark_delivery_submission_started(
                delivery_id,
                owner=self.owner,
                now=self.clock(),
            )
        except Exception:
            # No remote call has happened. Lease recovery can safely requeue it.
            raise

        idempotency_verified = capability is not None
        with LeaseHeartbeat(
            self.state,
            delivery_id=delivery_id,
            owner=self.owner,
            lease_seconds=self.settings.lease_seconds,
            interval_seconds=self.settings.heartbeat_seconds,
        ) as heartbeat:
            try:
                result = self.adapter.create_employee_checkin(
                    request, image_path=None
                )
                if not isinstance(result, EmployeeCheckinResult):
                    raise ERPNextAdapterError(
                        "ERPNext adapter returned an invalid result"
                    )
                if idempotency_verified and (
                    not result.idempotency_verified
                    or result.delivery_id != delivery_id
                    or result.erpnext_site != capability.site
                    or result.idempotency_fingerprint
                    != capability.fingerprint
                ):
                    raise ERPNextAdapterContractError(
                        "ERPNext result is not bound to the verified delivery contract"
                    )
            except Exception as exc:
                classification = classify_delivery_failure(
                    exc, idempotency_verified=idempotency_verified
                )
                if heartbeat.error is not None:
                    classification = DeliveryFailureClassification(
                        "retryable" if idempotency_verified else "uncertain",
                        (
                            "delivery_lease_heartbeat_lost_idempotent_replay"
                            if idempotency_verified
                            else "delivery_lease_heartbeat_lost"
                        ),
                        safe_after_submission=idempotency_verified,
                    )
                return self._record_failure_or_recovery_pending(
                    job, exc, classification
                )

        if heartbeat.error is not None:
            classification = DeliveryFailureClassification(
                "retryable" if idempotency_verified else "uncertain",
                (
                    "delivery_lease_heartbeat_lost_idempotent_replay"
                    if idempotency_verified
                    else "delivery_lease_heartbeat_lost"
                ),
                safe_after_submission=idempotency_verified,
            )
            return self._record_failure_or_recovery_pending(
                job, heartbeat.error, classification
            )

        try:
            current = self.state.mark_delivery_job_delivered_by_lease(
                delivery_id,
                owner=self.owner,
                remote_docname=result.docname,
                transport=result.transport,
                now=self.clock(),
            )
        except Exception as exc:
            classification = DeliveryFailureClassification(
                "retryable" if idempotency_verified else "uncertain",
                (
                    "local_delivery_commit_failed_idempotent_replay"
                    if idempotency_verified
                    else "local_delivery_commit_failed"
                ),
                safe_after_submission=idempotency_verified,
            )
            return self._record_failure_or_recovery_pending(
                job, exc, classification
            )
        self.logger(
            f"delivery completed id={delivery_id} "
            f"doc={current['remote_docname']} transport={current['transport']}"
        )
        return current["state"]

    def _record_failure_or_recovery_pending(
        self, job, exc, classification
    ):
        try:
            return self._record_failure(job, exc, classification)
        except Exception as state_exc:
            self.logger(
                f"delivery state update deferred to lease recovery "
                f"id={job['delivery_id']}: {state_exc}"
            )
            return "recovery_pending"

    def _record_failure(self, job, exc, classification):
        delivery_id = job["delivery_id"]
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
            current = self.state.mark_delivery_job_retry_by_lease(
                delivery_id,
                owner=self.owner,
                error_class=classification.error_class,
                error=str(exc),
                delay_seconds=delay,
                max_attempts=self.settings.max_attempts,
                safe_after_submission=classification.safe_after_submission,
                now=now,
            )
            self.logger(
                f"delivery {current['state']} id={delivery_id} "
                f"class={classification.error_class} "
                f"next={current['next_attempt_unix']}"
            )
            return current["state"]
        if classification.outcome == "permanent":
            current = self.state.mark_delivery_job_permanent_failure_by_lease(
                delivery_id,
                owner=self.owner,
                error_class=classification.error_class,
                error=str(exc),
                now=now,
            )
            self.logger(
                f"delivery permanent failure id={delivery_id} "
                f"class={classification.error_class}"
            )
            return current["state"]
        current = self.state.mark_delivery_job_uncertain_by_lease(
            delivery_id,
            owner=self.owner,
            error_class=classification.error_class,
            error=str(exc),
            now=now,
        )
        self.logger(
            f"delivery uncertain id={delivery_id} "
            f"class={classification.error_class}"
        )
        return current["state"]

    def run_forever(self, stop_event=None):
        stop_event = stop_event or threading.Event()
        while not stop_event.is_set():
            result = self.run_once()
            if result["processed"] == 0:
                stop_event.wait(self.settings.poll_seconds)


def _runtime_adapter(cfg):
    transport = select_erpnext_transport(cfg)
    if transport == "rest":
        return build_erpnext_adapter(cfg)
    import face_attendance as attendance

    return build_erpnext_adapter(
        cfg,
        bench_execute=attendance.bench_execute,
        bench_attach=lambda docname, path: attendance.attach_image(
            "Employee Checkin", docname, path
        ),
    )


def load_config(path=CONFIG):
    try:
        return load_runtime_config(path)
    except ConfigLoadError as exc:
        raise DeliveryWorkerConfigurationError(str(exc)) from exc


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--max-jobs", type=int)
    parser.add_argument("--config", default=str(CONFIG))
    parser.add_argument("--database", default="")
    args = parser.parse_args(argv)

    cfg = load_config(Path(args.config))
    settings = settings_from_config(cfg)
    attachment_settings = attachment_settings_from_config(cfg, root=ROOT)
    if (
        (settings.mode != "worker" or not settings.enabled)
        and not attachment_settings.enabled
    ):
        print("delivery and attachment workers disabled by configuration", flush=True)
        return 0

    database = (
        Path(args.database)
        if args.database
        else resolve_runtime_path(
            ROOT,
            cfg.get("runtime_state_db"),
            "runtime_state.sqlite3",
        )
    )
    state = RuntimeState(database)
    adapter = _runtime_adapter(cfg)
    worker = DeliveryWorker(
        state,
        adapter,
        settings,
        attachment_settings=attachment_settings,
        attachment_cfg=cfg,
        attachment_root=ROOT,
    )

    if args.once:
        result = worker.run_once(max_jobs=args.max_jobs)
        print(result, flush=True)
        return 0

    stop_event = threading.Event()

    def stop(_signum, _frame):
        stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    worker.run_forever(stop_event)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
