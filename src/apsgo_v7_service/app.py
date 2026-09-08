"""HTTP adapter for GQGA4 monthly rules and scheduling."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from threading import Event, Lock
from time import perf_counter
from types import SimpleNamespace

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool

from apsgo_scheduler.api.rule_management import (
    RuleManagementContractError,
    RuleManagementError,
    dumps_active_rules_response,
    dumps_rule_management_error,
    dumps_set_active_rules_response,
    loads_set_active_rules_request,
)
from apsgo_scheduler.app.rule_set_compiler import RuleSetCompilationError
from apsgo_scheduler.app.rule_set_loader import RuleSetLoadError
from apsgo_scheduler.core.contracts import SearchStopReason, SolverPolicy, SolveStatus

from .configuration import DEFAULT_CONFIGURATION_PATH, load_service_configuration
from .diagnostics import (
    DiagnosticStreamHandler,
    RunDiagnostics,
    TimestampFormatter,
    format_timing,
    log_bound_result,
    log_configuration,
    log_safely,
    request_context,
    start_cpu_timing,
    timing_metrics,
)
from .gqga4 import GQGA4_RULE_SET_TEMPLATE
from .grade_dictionary import GradePreparationError
from .month_scheduling import (
    MonthSchedulingContractError,
    MonthSchedulingMappingError,
    dumps_month_solve_error,
    dumps_month_solve_response,
    loads_month_solve_request,
)
from .rule_management import (
    DEFAULT_AUDIT_ACTOR,
    RuleManagementServiceError,
    get_active_gqga4_rules,
    set_active_gqga4_rules,
)
from .rule_store import RuleStoreSchemaError
from .scheduling import solve_gqga4_scheduling_task

GET_ACTIVE_RULES_PATH = "/api/v1/rule-sets/GQGA4/default/month/getActiveRules"
SET_ACTIVE_RULES_PATH = "/api/v1/rule-sets/GQGA4/default/month/setActiveRules"
MONTH_SOLVE_PATH = "/api/v1/scheduling/GQGA4/default/month/solve"
MAX_REQUEST_BODY_BYTES = 262_144
MAX_MONTH_SOLVE_REQUEST_BODY_BYTES = 8 * 1024 * 1024
DEFAULT_LISTEN_HOST = "0.0.0.0"
DEFAULT_LISTEN_PORT = 8001

_JSON_MEDIA_TYPE = "application/json"
_LOGGER = logging.getLogger("uvicorn.error.apsgo_v7.rule_management")
_UNAVAILABLE_SQLITE_MESSAGES = (
    "database is locked",
    "database table is locked",
    "unable to open database file",
    "attempt to write a readonly database",
    "database or disk is full",
    "disk i/o error",
)


class _HttpRequestError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 400):
        self.code = code
        self.status_code = status_code
        super().__init__(message)


def _json_response(content: str, status_code: int = 200, headers=None) -> Response:
    return Response(
        content=content,
        status_code=status_code,
        media_type=_JSON_MEDIA_TYPE,
        headers=headers,
    )


def _error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    expected_active_version_id: int | None = None,
    current_active_version_id: int | None = None,
    issues: Sequence = (),
    headers=None,
) -> Response:
    error = RuleManagementError(
        code=code,
        message=message,
        expected_active_version_id=expected_active_version_id,
        current_active_version_id=current_active_version_id,
        issues=tuple(issues),
    )
    return _json_response(
        dumps_rule_management_error(error), status_code=status_code, headers=headers
    )


def _month_error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    request_id: str | None = None,
    expected_active_version_id: int | None = None,
    current_active_version_id: int | None = None,
    issues: Sequence = (),
) -> Response:
    return _json_response(
        dumps_month_solve_error(
            code,
            message,
            request_id=request_id,
            expected_active_version_id=expected_active_version_id,
            current_active_version_id=current_active_version_id,
            issues=issues,
        ),
        status_code=status_code,
    )


def _contract_status(error: RuleManagementContractError) -> int:
    return 422 if any(issue.code == "duplicate_identity" for issue in error.issues) else 400


def _mapped_error(error: Exception) -> tuple[Response, str]:
    if isinstance(error, RuleManagementContractError):
        status = _contract_status(error)
        code = "rule_set_validation_failed" if status == 422 else "invalid_request"
        return (
            _error_response(
                status,
                code,
                "规则配置未通过校验。" if status == 422 else "请求格式不正确。",
                issues=error.issues,
            ),
            code,
        )
    if isinstance(error, (RuleSetCompilationError, RuleSetLoadError)):
        return (
            _error_response(
                422,
                "rule_set_validation_failed",
                "规则配置未通过 V7 权威校验。",
                issues=error.issues,
            ),
            "rule_set_validation_failed",
        )
    if isinstance(error, RuleManagementServiceError):
        if error.code == "rule_set_not_initialized":
            status = 404
            message = "GQGA4 月计划规则尚未初始化或尚无启用版本。"
        elif error.code in {"active_version_conflict", "operation_payload_conflict"}:
            status = 409
            message = (
                "活动规则版本已变化，请重新加载后再保存。"
                if error.code == "active_version_conflict"
                else "同一保存操作标识已用于不同的规则内容。"
            )
        else:
            status = 500
            message = "活动规则快照内部不一致。"
        return (
            _error_response(
                status,
                error.code,
                message,
                expected_active_version_id=error.expected_active_version_id,
                current_active_version_id=error.current_active_version_id,
            ),
            error.code,
        )
    if isinstance(error, _HttpRequestError):
        return _error_response(error.status_code, error.code, str(error)), error.code
    if isinstance(error, RuleStoreSchemaError):
        return (
            _error_response(500, "internal_server_error", "规则设置服务发生内部错误。"),
            "internal_server_error",
        )
    if isinstance(error, (FileNotFoundError, PermissionError, OSError)):
        return (
            _error_response(
                503,
                "rule_store_unavailable",
                "规则版本存储暂不可用，请稍后重试。",
            ),
            "rule_store_unavailable",
        )
    if isinstance(error, sqlite3.OperationalError):
        if any(part in str(error).lower() for part in _UNAVAILABLE_SQLITE_MESSAGES):
            return (
                _error_response(
                    503,
                    "rule_store_unavailable",
                    "规则版本存储暂不可用，请稍后重试。",
                ),
                "rule_store_unavailable",
            )
    if isinstance(error, sqlite3.DatabaseError):
        return (
            _error_response(
                500,
                "rule_store_failure",
                "规则版本存储发生内部错误。",
            ),
            "rule_store_failure",
        )
    return (
        _error_response(500, "internal_server_error", "规则设置服务发生内部错误。"),
        "internal_server_error",
    )


def _month_contract_status(error: MonthSchedulingContractError) -> int:
    issue = error.issues[0]
    if issue.code in {
        "duplicate_json_key",
        "invalid_json",
        "missing_field",
        "unknown_field",
        "unsupported_contract_version",
    }:
        return 400
    if issue.field_path in {"contract_version", "request_id", "expected_active_version_id"}:
        return 400
    return 422


def _mapped_month_error(
    error: Exception,
    *,
    request_id: str | None,
    expected_active_version_id: int | None,
) -> tuple[Response, str]:
    if isinstance(error, MonthSchedulingContractError):
        status = _month_contract_status(error)
        code = "invalid_request" if status == 400 else "scheduling_input_invalid"
        return (
            _month_error_response(
                status,
                code,
                "请求格式不正确。" if status == 400 else "月计划输入未通过校验。",
                request_id=request_id,
                expected_active_version_id=expected_active_version_id,
                issues=error.issues,
            ),
            code,
        )
    if isinstance(error, GradePreparationError):
        return (
            _month_error_response(
                422,
                "scheduling_input_invalid",
                "月计划输入未通过软硬钢数据准备校验。",
                request_id=request_id,
                expected_active_version_id=expected_active_version_id,
                issues=error.issues,
            ),
            "scheduling_input_invalid",
        )
    if isinstance(error, RuleManagementServiceError):
        if error.code == "active_version_conflict":
            status, message = 409, "活动规则版本已变化，请重新加载后再求解。"
        elif error.code in {"rule_set_not_initialized", "grade_dictionary_unavailable"}:
            status, message = 503, "GQGA4 月计划规则或软硬钢字典尚不可用。"
        else:
            status, message = 500, "活动求解快照内部不一致。"
        return (
            _month_error_response(
                status,
                error.code,
                message,
                request_id=request_id,
                expected_active_version_id=(
                    error.expected_active_version_id or expected_active_version_id
                ),
                current_active_version_id=error.current_active_version_id,
            ),
            error.code,
        )
    if isinstance(error, _HttpRequestError):
        return (
            _month_error_response(
                error.status_code,
                error.code,
                str(error),
                request_id=request_id,
                expected_active_version_id=expected_active_version_id,
            ),
            error.code,
        )
    if isinstance(error, (FileNotFoundError, PermissionError, OSError)):
        return (
            _month_error_response(
                503,
                "rule_store_unavailable",
                "规则版本存储暂不可用，请稍后重试。",
                request_id=request_id,
                expected_active_version_id=expected_active_version_id,
            ),
            "rule_store_unavailable",
        )
    if isinstance(error, sqlite3.OperationalError) and any(
        part in str(error).lower() for part in _UNAVAILABLE_SQLITE_MESSAGES
    ):
        return (
            _month_error_response(
                503,
                "rule_store_unavailable",
                "规则版本存储暂不可用，请稍后重试。",
                request_id=request_id,
                expected_active_version_id=expected_active_version_id,
            ),
            "rule_store_unavailable",
        )
    code = (
        "result_mapping_failed"
        if isinstance(error, MonthSchedulingMappingError)
        else "internal_server_error"
    )
    return (
        _month_error_response(
            500,
            code,
            "月计划求解服务发生内部错误。",
            request_id=request_id,
            expected_active_version_id=expected_active_version_id,
        ),
        code,
    )


def _log_result(
    method: str,
    status_code: int,
    *,
    operation_id: str | None = None,
    previous_version_id: int | None = None,
    active_version_id: int | None = None,
    saved_version_id: int | None = None,
    fingerprint: str | None = None,
    result_code: str,
    idempotent_replay: bool | None = None,
) -> None:
    identity = GQGA4_RULE_SET_TEMPLATE
    _LOGGER.info(
        "rule_management method=%s product_line=%s process=%s scenario=%s "
        "operation_id=%s previous_version_id=%s active_version_id=%s saved_version_id=%s "
        "fingerprint=%s actor=%s result_code=%s idempotent_replay=%s status=%s",
        method,
        identity.product_line_code,
        identity.process_code,
        identity.scenario,
        operation_id or "-",
        previous_version_id if previous_version_id is not None else "-",
        active_version_id if active_version_id is not None else "-",
        saved_version_id if saved_version_id is not None else "-",
        fingerprint or "-",
        DEFAULT_AUDIT_ACTOR,
        result_code,
        idempotent_replay if idempotent_replay is not None else "-",
        status_code,
    )


def _log_month_solve(
    status_code: int,
    *,
    request_id: str | None,
    expected_active_version_id: int | None,
    active_version_id: int | None,
    result_code: str,
    started: float,
    cpu_started: float | None,
    cpu_count: int | None,
) -> None:
    log_safely(_LOGGER, logging.INFO,
        "month_solve request_id=%s expected_active_version_id=%s "
        "active_version_id=%s result_code=%s status=%s %s",
        request_id or "-",
        expected_active_version_id if expected_active_version_id is not None else "-",
        active_version_id if active_version_id is not None else "-",
        result_code,
        status_code,
        format_timing(timing_metrics(started, cpu_started, cpu_count)),
    )


async def _read_request_body(
    request: Request,
    *,
    allow_body: bool,
    max_bytes: int = MAX_REQUEST_BODY_BYTES,
    too_large_status: int = 400,
) -> bytes:
    raw_length = request.headers.get("content-length")
    if raw_length is not None:
        try:
            content_length = int(raw_length)
        except ValueError as error:
            raise _HttpRequestError(
                "invalid_content_length", "Content-Length 必须是非负整数。"
            ) from error
        if content_length < 0:
            raise _HttpRequestError("invalid_content_length", "Content-Length 必须是非负整数。")
        if content_length > max_bytes:
            raise _HttpRequestError(
                "request_too_large",
                f"请求体不能超过 {max_bytes} 字节。",
                too_large_status,
            )

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > max_bytes:
            raise _HttpRequestError(
                "request_too_large",
                f"请求体不能超过 {max_bytes} 字节。",
                too_large_status,
            )
        body.extend(chunk)
    if body and not allow_body:
        raise _HttpRequestError("unexpected_request_body", "GET 请求不能包含请求体。")
    return bytes(body)


def _require_no_query(request: Request) -> None:
    if request.scope.get("query_string", b""):
        raise _HttpRequestError("unexpected_query_parameters", "该接口不接受查询参数。")


def _require_json_content_type(request: Request, *, status_code: int = 400) -> None:
    media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if media_type != _JSON_MEDIA_TYPE:
        raise _HttpRequestError(
            "invalid_content_type",
            "POST 请求的 Content-Type 必须是 application/json。",
            status_code,
        )


def _consume_background_task(task: asyncio.Task) -> None:
    try:
        task.exception()
    except asyncio.CancelledError:
        pass


def create_app(
    database_path: str | Path,
    *,
    timeout_seconds: float = 5.0,
    monthly_solve_policy: SolverPolicy | None = None,
    diagnostics_directory: Path | None = None,
) -> FastAPI:
    """Create the rule and scheduling adapter without opening a database."""

    if monthly_solve_policy is not None and not isinstance(monthly_solve_policy, SolverPolicy):
        raise ValueError("monthly_solve_policy must be SolverPolicy or None")

    application = FastAPI(
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        redirect_slashes=False,
    )
    solve_lock = Lock()

    async def log_startup():
        _LOGGER.info(
            "service_started diagnostics_enabled=%s diagnostic_directory=%s",
            diagnostics_directory is not None, diagnostics_directory or "-",
        )

    application.add_event_handler("startup", log_startup)

    @application.get(GET_ACTIVE_RULES_PATH, response_class=Response)
    async def get_active_rules(request: Request) -> Response:
        try:
            _require_no_query(request)
            await _read_request_body(request, allow_body=False)
            response = await run_in_threadpool(
                get_active_gqga4_rules,
                database_path,
                timeout_seconds=timeout_seconds,
            )
            content = dumps_active_rules_response(response)
        except Exception as error:
            result, result_code = _mapped_error(error)
            _log_result("GET", result.status_code, result_code=result_code)
            return result
        _log_result(
            "GET",
            200,
            active_version_id=response.active_version_id,
            fingerprint=response.rule_set_spec.fingerprint,
            result_code="active_rules_read",
        )
        return _json_response(content)

    @application.post(MONTH_SOLVE_PATH, response_class=Response)
    async def solve_month_schedule(request: Request) -> Response:
        started = perf_counter()
        cpu_started, cpu_count = start_cpu_timing()
        parsed = None
        try:
            _require_no_query(request)
            _require_json_content_type(request, status_code=415)
            if monthly_solve_policy is None:
                raise _HttpRequestError(
                    "monthly_solve_not_configured",
                    "月计划求解策略尚未配置。",
                    503,
                )
            body = await _read_request_body(
                request,
                allow_body=True,
                max_bytes=MAX_MONTH_SOLVE_REQUEST_BODY_BYTES,
                too_large_status=413,
            )
            parsed = await run_in_threadpool(
                loads_month_solve_request,
                body,
                monthly_solve_policy,
            )
            if not solve_lock.acquire(blocking=False):
                raise _HttpRequestError(
                    "scheduling_busy",
                    "已有月计划求解正在运行，请稍后重新提交。",
                    429,
                )

            cancellation_event = Event()
            cancellation = SimpleNamespace(is_cancelled=cancellation_event.is_set)
            diagnostic = RunDiagnostics(
                diagnostics_directory, parsed.task_input.request_id, started,
                cpu_started=cpu_started, cpu_count=cpu_count,
            )

            def mark_disconnected():
                if not cancellation_event.is_set():
                    token = request_context.set(diagnostic)
                    try:
                        diagnostic.observe(_LOGGER.warning, "month_solve_client_disconnected worker_continues=True")
                    finally:
                        request_context.reset(token)
                    cancellation_event.set()

            def solve_in_worker():
                token = request_context.set(diagnostic)
                bound = None
                response = None
                failure = None
                try:
                    diagnostic.observe(diagnostic.start, body)
                    diagnostic.observe(_LOGGER.info,
                        "month_solve_started order_count=%s period_count=%s "
                        "expected_active_version_id=%s seed=%s candidate_check_limit=%s "
                        "total_time_limit_seconds=%s diagnostic_directory=%s cpu_count=%s",
                        len(parsed.task_input.orders), len(parsed.task_input.periods),
                        parsed.expected_active_version_id, monthly_solve_policy.seed,
                        monthly_solve_policy.candidate_check_limit,
                        monthly_solve_policy.total_time_limit_seconds, diagnostic.directory or "-",
                        cpu_count if cpu_count is not None else "-",
                    )
                    try:
                        bound = solve_gqga4_scheduling_task(
                            parsed.task_input,
                            database_path,
                            timeout_seconds=timeout_seconds,
                            expected_active_version_id=parsed.expected_active_version_id,
                            cancellation=cancellation,
                        )
                        # Public mapping failures remain HTTP errors, not diagnostic failures.
                        content = dumps_month_solve_response(parsed, bound)
                        status_code = (
                            422
                            if bound.result.status is SolveStatus.FAILED
                            and bound.result.stop_reason is SearchStopReason.INPUT_INVALID
                            else 200
                        )
                        response = _json_response(content, status_code=status_code)
                        result_code = bound.result.status.value
                    except Exception as error:
                        failure = error
                        response, result_code = _mapped_month_error(
                            error, request_id=parsed.task_input.request_id,
                            expected_active_version_id=parsed.expected_active_version_id,
                        )
                        diagnostic.observe(_LOGGER.log,
                            logging.ERROR if response.status_code >= 500 else logging.WARNING,
                            "month_solve_error result_code=%s type=%s message=%s",
                            result_code, type(error).__name__, str(error),
                            exc_info=response.status_code >= 500,
                        )
                    if bound is not None:
                        diagnostic.observe(log_bound_result, _LOGGER, bound)
                    diagnostic.observe(diagnostic.finish, bound, response, failure, cancellation_event.is_set)
                    diagnostic.observe(_log_month_solve,
                        response.status_code,
                        request_id=parsed.task_input.request_id,
                        expected_active_version_id=parsed.expected_active_version_id,
                        active_version_id=(
                            bound.active_rule_set_version_id if bound is not None else
                            getattr(failure, "current_active_version_id", None)
                        ),
                        result_code=result_code, started=started,
                        cpu_started=cpu_started, cpu_count=cpu_count,
                    )
                    diagnostic.observe(_LOGGER.info,
                        "month_solve_finished diagnostic_directory=%s diagnostic_failure_count=%s "
                        "client_disconnected=%s",
                        diagnostic.directory or "-", len(diagnostic.failures), cancellation_event.is_set(),
                    )
                    return response
                finally:
                    try:
                        diagnostic.observe(diagnostic.close)
                        diagnostic.observe(_LOGGER.info,
                            "month_solve_worker_finished diagnostic_directory=%s "
                            "diagnostic_failure_count=%s client_disconnected=%s",
                            diagnostic.directory or "-", len(diagnostic.failures), cancellation_event.is_set(),
                        )
                    finally:
                        request_context.reset(token)
                        solve_lock.release()

            try:
                worker = asyncio.create_task(run_in_threadpool(solve_in_worker))
            except BaseException:
                solve_lock.release()
                raise
            try:
                while not worker.done():
                    await asyncio.wait((worker,), timeout=0.05)
                    if not worker.done() and await request.is_disconnected():
                        mark_disconnected()
                return await worker
            except asyncio.CancelledError:
                mark_disconnected()
                worker.add_done_callback(_consume_background_task)
                raise
        except asyncio.CancelledError:
            raise
        except Exception as error:
            result, result_code = _mapped_month_error(
                error,
                request_id=None if parsed is None else parsed.task_input.request_id,
                expected_active_version_id=(
                    None if parsed is None else parsed.expected_active_version_id
                ),
            )
            _log_month_solve(
                result.status_code,
                request_id=None if parsed is None else parsed.task_input.request_id,
                expected_active_version_id=(
                    None if parsed is None else parsed.expected_active_version_id
                ),
                active_version_id=(
                    error.current_active_version_id
                    if isinstance(error, RuleManagementServiceError)
                    else None
                ),
                result_code=result_code,
                started=started,
                cpu_started=cpu_started, cpu_count=cpu_count,
            )
            if result.status_code >= 500:
                log_safely(_LOGGER, logging.ERROR,
                    "month_solve_error request_id=%s %s",
                    None if parsed is None else parsed.task_input.request_id,
                    format_timing(timing_metrics(started, cpu_started, cpu_count)),
                    exc_info=True,
                )
            return result
    @application.post(SET_ACTIVE_RULES_PATH, response_class=Response)
    async def set_active_rules(request: Request) -> Response:
        operation_id = None
        expected_version_id = None
        try:
            _require_no_query(request)
            _require_json_content_type(request)
            body = await _read_request_body(request, allow_body=True)
            parsed = await run_in_threadpool(loads_set_active_rules_request, body)
            operation_id = parsed.save_operation_id
            expected_version_id = parsed.expected_active_version_id
            response = await run_in_threadpool(
                set_active_gqga4_rules,
                parsed,
                database_path,
                timeout_seconds=timeout_seconds,
            )
            content = dumps_set_active_rules_response(response)
        except Exception as error:
            result, result_code = _mapped_error(error)
            _log_result(
                "POST",
                result.status_code,
                operation_id=operation_id,
                previous_version_id=expected_version_id,
                active_version_id=(
                    error.current_active_version_id
                    if isinstance(error, RuleManagementServiceError)
                    else None
                ),
                result_code=result_code,
            )
            return result
        _log_result(
            "POST",
            200,
            operation_id=operation_id,
            previous_version_id=response.previous_active_version_id,
            active_version_id=response.active_rules.active_version_id,
            saved_version_id=response.saved_version_id,
            fingerprint=response.active_rules.rule_set_spec.fingerprint,
            result_code=(
                "idempotent_replay" if response.idempotent_replay else "saved_and_activated"
            ),
            idempotent_replay=response.idempotent_replay,
        )
        return _json_response(content)

    async def http_protocol_error(request: Request, error) -> Response:
        if error.status_code == 404:
            result = _error_response(404, "route_not_found", "请求的接口不存在。")
        else:
            result = _error_response(
                405,
                "method_not_allowed",
                "该接口不支持当前 HTTP 方法。",
                headers=error.headers,
            )
        _log_result(
            request.method,
            result.status_code,
            result_code="route_not_found" if error.status_code == 404 else "method_not_allowed",
        )
        return result

    application.add_exception_handler(404, http_protocol_error)
    application.add_exception_handler(405, http_protocol_error)
    return application


def run_server(
    configuration_path: str | Path = DEFAULT_CONFIGURATION_PATH,
) -> None:
    """Run the local service from one fully validated configuration."""

    configuration = load_service_configuration(configuration_path)
    log_config = log_configuration(configuration.diagnostics_directory)
    application = create_app(
        configuration.database_path,
        timeout_seconds=configuration.database_timeout_seconds,
        monthly_solve_policy=configuration.monthly_solve_policy,
        diagnostics_directory=configuration.diagnostics_directory,
    )
    uvicorn.run(
        application,
        host=configuration.listen_host,
        port=configuration.listen_port,
        access_log=False,
        workers=1,
        log_config=log_config,
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the local APSGo V7 rule service.")
    parser.add_argument("--config", default=DEFAULT_CONFIGURATION_PATH, type=Path)
    arguments = parser.parse_args(argv)
    try:
        run_server(arguments.config)
    except Exception:
        handler = DiagnosticStreamHandler()
        handler.setFormatter(TimestampFormatter())
        startup_logger = logging.Logger("apsgo_v7_service.startup")
        startup_logger.addHandler(handler)
        startup_logger.exception("service_start_failed")
        handler.close()
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()


__all__ = [
    "DEFAULT_LISTEN_HOST",
    "DEFAULT_LISTEN_PORT",
    "GET_ACTIVE_RULES_PATH",
    "MAX_REQUEST_BODY_BYTES",
    "MAX_MONTH_SOLVE_REQUEST_BODY_BYTES",
    "MONTH_SOLVE_PATH",
    "SET_ACTIVE_RULES_PATH",
    "create_app",
    "main",
    "run_server",
]
