"""Local diagnostic logging; observation failures never change solver decisions."""

from __future__ import annotations

import csv
import io
import logging
import sys
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import fields, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tempfile import NamedTemporaryFile, mkdtemp
from time import perf_counter
from uuid import UUID

from apsgo_scheduler.api.json_codec import dumps_exact_json

request_context: ContextVar[RunDiagnostics | None] = ContextVar(
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
            request_id, started = context.request_id, context.started
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
        callback = getattr(self, "failure_callback", None)
        if callback is not None:
            callback("solve.log", sys.exc_info()[1])
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


def _json_values(value):
    """Project immutable carriers without asdict's deepcopy of mapping proxies."""
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {part.name: _json_values(getattr(value, part.name)) for part in fields(value)}
    if isinstance(value, Mapping):
        return {key: _json_values(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_values(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_json_values(item) for item in value), key=dumps_exact_json)
    return value


def _candidate_csv(plan, publishable):
    from .month_scheduling import build_month_solve_rows

    rows = build_month_solve_rows(plan, ())
    if not rows:
        return "artifact_kind,publishable\n"
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=("artifact_kind", "publishable", *rows[0]))
    writer.writeheader()
    for row in rows:
        converted = {}
        for key, value in row.items():
            if isinstance(value, (dict, list, tuple)):
                value = dumps_exact_json(value)
            if isinstance(value, str):
                value = _safe_text(value)
                if value.lstrip().startswith(("=", "+", "-", "@")):
                    value = "'" + value
            converted[key] = value
        writer.writerow({"artifact_kind": "diagnostic_candidate", "publishable": publishable, **converted})
    return output.getvalue()


class RunDiagnostics:
    """One worker's files; all serialization and I/O failures stay observational."""

    def __init__(self, root, request_id, started):
        self.root = root
        self.request_id = request_id
        self.started = started
        self.directory = None
        self.handler = None
        self.written = []
        self.failures = []
        self.summary = None

    def _remember_failure(self, filename, error):
        item = {"filename": filename, "error": str(error), "type": type(error).__name__}
        if item not in self.failures:
            self.failures.append(item)

    def observe(self, action, *args, **kwargs):
        try:
            action(*args, **kwargs)
        except Exception as error:
            self._failed(action.__name__, error)

    def _failed(self, filename, error):
        self._remember_failure(filename, error)
        record = logging.LogRecord(
            "apsgo_v7_service.diagnostics", logging.WARNING, __file__, 0,
            "diagnostic_write_failed filename=%s error=%s", (filename, str(error)), None,
        )
        handler = DiagnosticStreamHandler()
        handler.setFormatter(TimestampFormatter())
        handler.handle(record)
        handler.close()

    def start(self, body):
        if self.root is None:
            return
        try:
            parent = self.root / "runs" / str(UUID(self.request_id))
            parent.mkdir(parents=True, exist_ok=True)
            self.directory = Path(mkdtemp(prefix=datetime.now().strftime("%Y%m%d_%H%M%S_"), dir=parent))
        except Exception as error:
            self._failed("run_directory", error)
            return
        try:
            self.handler = DiagnosticFileHandler(self.directory / "solve.log", encoding="utf-8")
            self.handler.setFormatter(TimestampFormatter())
            self.handler.failure_callback = self._remember_failure
            self.handler.addFilter(lambda record: request_context.get() is self)
            for name in ("uvicorn", "apsgo_scheduler", "apsgo_v7_service"):
                logging.getLogger(name).addHandler(self.handler)
            self.written.append("solve.log")
        except Exception as error:
            self._failed("solve.log", error)
        self.write("request.json", lambda: body.decode("utf-8"))

    def write(self, filename, render):
        if self.directory is None:
            return
        temporary = None
        try:
            content = render()
            with NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=self.directory,
                                    prefix="." + filename, suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(content)
            temporary.replace(self.directory / filename)
            self.written.append(filename)
        except Exception as error:
            self._failed(filename, error)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError as error:
                    self._failed(temporary.name, error)

    def prepared(self, task):
        self.write("prepared_request.json", lambda: dumps_exact_json(_json_values(task)))

    def finish(self, bound, response, error, disconnected):
        if self.directory is None:
            return
        if response is not None:
            self.write("response.json", lambda: response.body.decode("utf-8"))
        result = None if bound is None else bound.result
        candidate = None if result is None else result.diagnostic_candidate
        if candidate is not None:
            self.write("candidate_rows.csv", lambda: _candidate_csv(candidate.plan, result.release is not None))

        def summary():
            return dumps_exact_json(_json_values({
                "schema_version": 1,
                "artifact_kind": "diagnostic_only_not_for_writeback",
                "request_id": self.request_id,
                "http_status": None if response is None else response.status_code,
                "client_disconnected": disconnected(),
                "elapsed_seconds": Decimal(str(perf_counter() - self.started)),
                "publishable": result is not None and result.release is not None,
                "candidate_evaluation_kind": "search_evaluation_not_independent_audit",
                "bound_result": bound,
                "exception": None if error is None else {"type": type(error).__name__, "message": str(error)},
                "written_files": tuple(self.written) + ("diagnostic_summary.json",),
                "write_failures": self.failures,
            }))

        self.summary = summary

    def close(self):
        if self.handler is not None:
            for name in ("uvicorn", "apsgo_scheduler", "apsgo_v7_service"):
                logging.getLogger(name).removeHandler(self.handler)
            try:
                self.handler.close()
            except Exception as error:
                self._failed("solve.log", error)
        if self.summary is not None:
            self.write("diagnostic_summary.json", self.summary)
