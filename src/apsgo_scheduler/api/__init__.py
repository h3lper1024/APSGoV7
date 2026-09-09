"""Public request and result contracts."""

from ..core.contracts import (
    ControlledSplitMode,
    RuleScope,
    SearchStopReason,
    SolverPolicy,
    SolveStatus,
)
from ..core.model import MaterialRole, VirtualPurpose
from .diagnostics import DiagnosticIssue, DiagnosticPhase, DiagnosticSeverity
from .request import (
    OrderInput,
    PeriodInput,
    QualityCriterionSpec,
    RuleDefinitionSpec,
    RuleSetSpec,
    SchedulingRequest,
    VirtualPrototypeInput,
)
from .result import (
    ResultAuditReport,
    ResultAuditStatus,
    RunManifest,
    SchedulingRelease,
    SchedulingResult,
)
from .rule_management import (
    ActiveRulesResponse,
    EditableRuleInput,
    RuleManagementContractError,
    RuleManagementError,
    SetActiveRulesRequest,
    SetActiveRulesResponse,
    dumps_active_rules_response,
    dumps_rule_management_error,
    dumps_set_active_rules_request,
    dumps_set_active_rules_response,
    loads_set_active_rules_request,
)

__all__ = [
    "ActiveRulesResponse",
    "ControlledSplitMode",
    "DiagnosticIssue",
    "DiagnosticPhase",
    "DiagnosticSeverity",
    "EditableRuleInput",
    "MaterialRole",
    "OrderInput",
    "PeriodInput",
    "QualityCriterionSpec",
    "ResultAuditReport",
    "ResultAuditStatus",
    "RuleDefinitionSpec",
    "RuleManagementContractError",
    "RuleManagementError",
    "RuleScope",
    "RuleSetSpec",
    "RunManifest",
    "SchedulingRelease",
    "SchedulingRequest",
    "SchedulingResult",
    "SetActiveRulesRequest",
    "SetActiveRulesResponse",
    "SearchStopReason",
    "SolverPolicy",
    "SolveStatus",
    "VirtualPrototypeInput",
    "VirtualPurpose",
    "dumps_active_rules_response",
    "dumps_rule_management_error",
    "dumps_set_active_rules_request",
    "dumps_set_active_rules_response",
    "loads_set_active_rules_request",
]
