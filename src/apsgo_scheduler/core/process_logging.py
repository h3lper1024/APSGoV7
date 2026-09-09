"""Small standard-library log helpers; observation cannot interrupt a solve."""

import logging

STAGE_NAMES = {
    "request_preparation": "请求准备",
    "rule_loading": "规则加载",
    "input_normalization": "输入标准化",
    "core_solve": "核心求解",
    "input_validation": "核心输入校验",
    "construction_graph": "构建连接图",
    "minimum_path_cover": "最小路径覆盖",
    "initial_solution": "初始方案构造",
    "local_search": "局部搜索",
    "controlled_split_and_replay": "受控拆单及搜索重放",
    "local_search_replay": "拆单后局部搜索重放",
    "width_optimization": "相邻链宽差优化",
    "core_audit": "核心独立审计",
    "result_assembly": "结果组装",
    "result_contract_audit": "结果契约审计",
    "result_sealing": "结果签发",
}


def emit(logger, event, *, level=logging.INFO, exc_info=False, **details):
    try:
        if "stage" in details:
            details = {**details, "stage_name": STAGE_NAMES[details["stage"]]}
        logger.log(
            level,
            event + " " + " ".join(f"{key}=%s" for key in details),
            *details.values(),
            exc_info=exc_info,
            extra={"solver_event": event, "solver_details": details},
        )
    except Exception:
        # A handler failure must not undo an accepted move or change publication.
        pass


def stop_status(reason):
    if reason is None or reason == "local_search_complete":
        return "completed"
    if reason == "user_cancelled":
        return "cancelled"
    if reason == "input_invalid":
        return "failed"
    return "error" if reason == "system_error" else "truncated"


def audit_status(report):
    if report.status == "completed":
        return "completed" if report.passed else "failed"
    return "truncated" if report.status == "time_limit" else report.status.value
