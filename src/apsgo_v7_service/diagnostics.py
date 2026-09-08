"""Local diagnostic logging; observation failures never change solver decisions."""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from time import perf_counter


request_context: ContextVar[tuple[str, float] | None] = ContextVar(
    "apsgo_diagnostic_request", default=None
)


def _safe_text(value: str) -> str:
    return "".join(
        char if char.isprintable() or char == " " else ascii(char)[1:-1]
        for char in value
    )


class TimestampFormatter(logging.Formatter):
    """Prefix every physical line, including exception continuations."""

    def format(self, record):
        at = datetime.fromtimestamp(record.created).astimezone()
        prefix = f"{at:%Y-%m-%d %H:%M:%S}.{int(record.msecs):03d}{at:%z} {record.levelname} {record.name} "
        message = _safe_text(record.getMessage())
        context = request_context.get()
        if context is not None:
            request_id, started = context
            if "request_id=" not in message:
                message += f" request_id={_safe_text(request_id)}"
            if "elapsed_seconds=" not in message:
                message += f" elapsed_seconds={perf_counter() - started:.6f}"
        lines = [message]
        if record.exc_info:
            lines.extend(self.formatException(record.exc_info).splitlines())
        if record.stack_info:
            lines.extend(record.stack_info.splitlines())
        return "\n".join(prefix + _safe_text(line) for line in lines)


def _report_log_failure(record, target):
    # The fallback cannot call logging again: a failed disk could recurse forever.
    try:
        failure = logging.LogRecord(
            "apsgo_v7_service.diagnostics", logging.WARNING, __file__, 0,
            "diagnostic_write_failed logger=%s filename=%s", (record.name, target), None,
        )
        sys.stderr.write(TimestampFormatter().format(failure) + "\n")
    except Exception:
        pass


class DiagnosticStreamHandler(logging.StreamHandler):
    def handleError(self, record):
        _report_log_failure(record, "console")


class DiagnosticFileHandler(RotatingFileHandler):
    def handleError(self, record):
        _report_log_failure(record, self.baseFilename)


def log_configuration(directory: Path | None = None) -> dict:
    """Uvicorn installs this configuration once, including algorithm loggers."""
    handlers = {
        "console": {"()": DiagnosticStreamHandler, "formatter": "timestamp"},
    }
    if directory is not None:
        directory.mkdir(parents=True, exist_ok=True)
        handlers["file"] = {
            "()": DiagnosticFileHandler,
            "formatter": "timestamp",
            "filename": str(directory / "service.log"),
            "encoding": "utf-8",
            "maxBytes": 10 * 1024 * 1024,
            "backupCount": 3,
        }
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"timestamp": {"()": TimestampFormatter}},
        "handlers": handlers,
        "loggers": {
            name: {"handlers": list(handlers), "level": "INFO", "propagate": False}
            for name in ("uvicorn", "apsgo_scheduler", "apsgo_v7_service")
        },
    }


def log_bound_result(logger, bound) -> None:
    """Read existing evidence, including candidates that were not released."""
    result = bound.result
    candidate = result.diagnostic_candidate
    evaluation = None if candidate is None else candidate.search_evaluation
    quality = () if evaluation is None else tuple(
        (item.metric_key, value)
        for item, value in zip(bound.quality_spec, evaluation.quality_key)
    )
    logger.log(
        logging.INFO if result.release is not None else logging.WARNING,
        "month_solve_result request_id=%s status=%s stop_reason=%s publishable=%s "
        "core_audit=%s core_audit_passed=%s result_audit=%s result_audit_passed=%s "
        "quality=%s violation_count=%s issue_count=%s counters=%s "
        "stage_duration_seconds=%s result_fingerprint=%s",
        result.request_id, result.status.value, result.stop_reason.value,
        result.release is not None, result.core_audit.status.value, result.core_audit.passed,
        result.audit_report.status.value, result.audit_report.passed, quality,
        0 if evaluation is None else len(evaluation.violations), len(result.issues),
        dict(result.run_manifest.counters), dict(result.run_manifest.stage_duration_seconds),
        result.result_fingerprint,
    )
    # Audit issues carry the actual final failures; candidate violations are search evidence.
    seen = set()
    for issue in result.issues:
        key = (issue.code, issue.subject_id, issue.message)
        if key not in seen:
            seen.add(key)
            logger.warning(
                "month_solve_issue request_id=%s code=%s phase=%s field=%s subject=%s message=%s",
                result.request_id, issue.code, issue.phase.value,
                issue.field_path, issue.subject_id, issue.message,
            )
    if evaluation is not None and result.release is None:
        for item in evaluation.violations:
            if any(
                item.subject_id == issue.subject_id
                and item.rule_id in issue.message and item.message in issue.message
                for issue in result.issues
            ):
                continue
            logger.warning(
                "month_solve_candidate_violation request_id=%s rule_id=%s reason=%s "
                "subject=%s disposition=%s message=%s",
                result.request_id, item.rule_id, item.reason_code, item.subject_id,
                item.disposition.value, item.message,
            )
    failures = (
        result.core_audit.invariant_failure_codes
        + result.core_audit.action_authorization_failure_codes
        + result.audit_report.failure_codes
    )
    if failures:
        logger.warning("month_solve_audit_failed request_id=%s codes=%s", result.request_id, failures)
