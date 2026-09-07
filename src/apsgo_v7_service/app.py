"""HTTP adapter for GQGA4 monthly rule management."""

from __future__ import annotations

import argparse
import logging
import sqlite3
from collections.abc import Sequence
from pathlib import Path

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

from .configuration import DEFAULT_CONFIGURATION_PATH, load_service_configuration
from .gqga4 import GQGA4_RULE_SET_TEMPLATE
from .rule_management import (
    DEFAULT_AUDIT_ACTOR,
    RuleManagementServiceError,
    get_active_gqga4_rules,
    set_active_gqga4_rules,
)
from .rule_store import RuleStoreSchemaError

GET_ACTIVE_RULES_PATH = "/api/v1/rule-sets/GQGA4/default/month/getActiveRules"
SET_ACTIVE_RULES_PATH = "/api/v1/rule-sets/GQGA4/default/month/setActiveRules"
MAX_REQUEST_BODY_BYTES = 262_144
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
    def __init__(self, code: str, message: str):
        self.code = code
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
        return _error_response(400, error.code, str(error)), error.code
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


async def _read_request_body(request: Request, *, allow_body: bool) -> bytes:
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
        if content_length > MAX_REQUEST_BODY_BYTES:
            raise _HttpRequestError(
                "request_too_large",
                f"请求体不能超过 {MAX_REQUEST_BODY_BYTES} 字节。",
            )

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_REQUEST_BODY_BYTES:
            raise _HttpRequestError(
                "request_too_large",
                f"请求体不能超过 {MAX_REQUEST_BODY_BYTES} 字节。",
            )
        body.extend(chunk)
    if body and not allow_body:
        raise _HttpRequestError("unexpected_request_body", "GET 请求不能包含请求体。")
    return bytes(body)


def _require_no_query(request: Request) -> None:
    if request.scope.get("query_string", b""):
        raise _HttpRequestError("unexpected_query_parameters", "该接口不接受查询参数。")


def _require_json_content_type(request: Request) -> None:
    media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if media_type != _JSON_MEDIA_TYPE:
        raise _HttpRequestError(
            "invalid_content_type", "POST 请求的 Content-Type 必须是 application/json。"
        )


def create_app(database_path: str | Path, *, timeout_seconds: float = 5.0) -> FastAPI:
    """Create the two-route adapter without opening or initializing a database."""

    application = FastAPI(
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        redirect_slashes=False,
    )

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
    application = create_app(
        configuration.database_path,
        timeout_seconds=configuration.database_timeout_seconds,
    )
    uvicorn.run(
        application,
        host=configuration.listen_host,
        port=configuration.listen_port,
        access_log=False,
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the local APSGo V7 rule service.")
    parser.add_argument("--config", default=DEFAULT_CONFIGURATION_PATH, type=Path)
    arguments = parser.parse_args(argv)
    run_server(arguments.config)


if __name__ == "__main__":
    main()


__all__ = [
    "DEFAULT_LISTEN_HOST",
    "DEFAULT_LISTEN_PORT",
    "GET_ACTIVE_RULES_PATH",
    "MAX_REQUEST_BODY_BYTES",
    "SET_ACTIVE_RULES_PATH",
    "create_app",
    "main",
    "run_server",
]
