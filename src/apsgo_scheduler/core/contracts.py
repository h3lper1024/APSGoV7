"""Shared scalar validation, diagnostics, policy and core result identities."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from decimal import Context, Decimal, localcontext
from enum import Enum
from hashlib import sha256
from types import MappingProxyType
from typing import TYPE_CHECKING, TypeAlias, Union

if TYPE_CHECKING:
    from .evaluation import PlanEvaluation
    from .model import SchedulePlan
    from .neighborhoods import AcceptedMoveTrace
    from .resource_facts import PlanDerivedFacts

RuleScalar = str | int | Decimal | bool | None
RuleParameterValue: TypeAlias = Union[
    RuleScalar, tuple["RuleParameterValue", ...], Mapping[str, "RuleParameterValue"]
]
CONSTRUCTION_ORDER_KEY = "solverpy_stable_order_v1"
NUMERIC_SEMANTICS_KEY = "solverpy_float_epsilon_1e_9"


def contract_values(value, omitted=()):
    """Only explicitly optional extensions disappear from historical projections."""
    return {
        part.name: getattr(value, part.name)
        for part in fields(value)
        if part.name not in omitted
        and not (part.metadata.get("omit_none") and getattr(value, part.name) is None)
    }


def canonical_json(value) -> str:
    """Encode value contracts deterministically, without floats or ambient rounding."""

    def encode(item):
        if isinstance(item, Enum):
            return encode(item.value)
        if isinstance(item, Decimal):
            if not item.is_finite():
                # Invalid typed requests still need an identity before validation.
                return ["decimal", str(item)]
            sign, digits, exponent = item.as_tuple()
            coefficient = "".join(map(str, digits)).rstrip("0")
            if not coefficient:
                return ["decimal", 0, "0", 0]
            return ["decimal", sign, coefficient, exponent + len(digits) - len(coefficient)]
        if is_dataclass(item) and not isinstance(item, type):
            item = contract_values(item)
        if isinstance(item, Mapping):
            if any(not isinstance(key, str) for key in item):
                raise ValueError("canonical mapping keys must be text")
            return ["object", [[key, encode(item[key])] for key in sorted(item)]]
        if isinstance(item, (tuple, list)):
            return ["array", [encode(part) for part in item]]
        if isinstance(item, frozenset):
            parts = [encode(part) for part in item]
            return ["set", sorted(parts, key=dump)]
        if item is None or type(item) in (str, int, bool):
            return item
        raise ValueError(f"unsupported canonical value type: {type(item).__name__}")

    def dump(item):
        return json.dumps(item, ensure_ascii=True, separators=(",", ":"), allow_nan=False)

    return dump(encode(value))


def fingerprint(value) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


class RuleScope(str, Enum):
    NODE = "node"
    EDGE = "edge"
    CHAIN = "chain"
    PLAN = "plan"
    ACTION_ELIGIBILITY = "action_eligibility"


def require_text(value, name: str, *, allow_none: bool = False) -> None:
    if allow_none and value is None:
        return
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty text")


def require_int(value, name: str, *, minimum: int | None = 0) -> None:
    if type(value) is not int or (minimum is not None and value < minimum):
        raise ValueError(f"{name} must be an integer with minimum {minimum}")


def require_decimal(
    value, name: str, *, positive=False, nonnegative=False, allow_none=False
) -> None:
    if allow_none and value is None:
        return
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} must be a finite Decimal")
    if (positive and value <= 0) or (nonnegative and value < 0):
        raise ValueError(f"{name} is outside its permitted range")


def require_enum(value, enum_type, name: str) -> None:
    if not isinstance(value, enum_type):
        raise ValueError(f"{name} must be {enum_type.__name__}")


def sum_decimals(values) -> Decimal:
    """Sum finite signed decimals exactly, independent of the caller's context."""
    values = tuple(values)
    if not values:
        return Decimal(0)
    for value in values:
        require_decimal(value, "value")
    exponent = min(value.as_tuple().exponent for value in values)
    leading = max(value.adjusted() for value in values)
    precision = max(1, leading - exponent + len(str(len(values))) + 1)
    with localcontext(Context(prec=precision)):
        return sum(values, Decimal(0))


def sum_weights(values) -> Decimal:
    """Preserve nonnegative weight validation and exact conservation arithmetic."""
    values = tuple(values)
    for value in values:
        require_decimal(value, "weight", nonnegative=True)
    return sum_decimals(values)


def freeze_tuple(values, expected_type, name: str) -> tuple:
    if not isinstance(values, (tuple, list)):
        raise ValueError(f"{name} must be an ordered sequence")
    result = tuple(values)
    if any(not isinstance(value, expected_type) for value in result):
        raise ValueError(f"{name} has an invalid element")
    return result


def freeze_scalars(values: Mapping[str, RuleScalar]) -> Mapping[str, RuleScalar]:
    if not isinstance(values, Mapping):
        raise ValueError("rule attributes must be a mapping")
    copied = {}
    for key, value in values.items():
        require_text(key, "attribute key")
        if isinstance(value, Decimal):
            require_decimal(value, key)
        elif value is not None and type(value) not in (str, int, bool):
            raise ValueError(f"{key} must be a rule scalar, not a nested or mutable value")
        copied[key] = value
    return MappingProxyType(copied)


def freeze_rule_parameters(values: Mapping) -> Mapping[str, RuleParameterValue]:
    """Detach and freeze rule-only data, without widening node attribute values."""
    if not isinstance(values, Mapping):
        raise ValueError("rule parameters must be a mapping")
    active_ids = set()

    def freeze(value, path):
        if isinstance(value, Decimal):
            require_decimal(value, path)
            return value
        if value is None or type(value) in (str, int, bool):
            return value
        if not isinstance(value, (Mapping, list, tuple)):
            raise ValueError(f"{path} has unsupported rule parameter type: {type(value).__name__}")
        identity = id(value)
        if identity in active_ids:
            raise ValueError(f"{path} contains a circular rule parameter reference")
        active_ids.add(identity)
        try:
            if isinstance(value, Mapping):
                copied = {}
                for key, part in value.items():
                    require_text(key, f"{path} key")
                    copied[key] = freeze(part, f"{path}[{key!r}]")
                return MappingProxyType(copied)
            return tuple(freeze(part, f"{path}[{index}]") for index, part in enumerate(value))
        finally:
            active_ids.remove(identity)

    return freeze(values, "parameters")


class ControlledSplitMode(str, Enum):
    SAME_PERIOD_SPLIT = "same_period_split"
    FUTURE_BORROW_RETURN = "future_borrow_return"


def validate_split_target(mode, source: str, origin: str, target: str) -> None:
    require_enum(mode, ControlledSplitMode, "split_mode")
    for name, value in (
        ("source_period", source),
        ("origin_assigned_period", origin),
        ("target_assigned_period", target),
    ):
        require_text(value, name)
    if target != source:
        raise ValueError("split target must equal its authorized source period")
    if (mode is ControlledSplitMode.SAME_PERIOD_SPLIT) != (source == origin):
        raise ValueError("split mode does not match source and original assigned periods")


class SolveStatus(str, Enum):
    SUCCESS = "success"
    PUBLISHABLE_WITH_ALLOWED_DEVIATION = "publishable_with_allowed_deviation"
    COMPLETE_NOT_PUBLISHABLE = "complete_not_publishable"
    NO_COMPLETE_PLAN = "no_complete_plan"
    CANCELLED = "cancelled"
    FAILED = "failed"


class SearchStopReason(str, Enum):
    LOCAL_SEARCH_COMPLETE = "local_search_complete"
    CANDIDATE_LIMIT_REACHED = "candidate_limit_reached"
    SEARCH_TIME_LIMIT_REACHED = "search_time_limit_reached"
    FINALIZATION_TIME_LIMIT_REACHED = "finalization_time_limit_reached"
    USER_CANCELLED = "user_cancelled"
    INPUT_INVALID = "input_invalid"
    SYSTEM_ERROR = "system_error"

    @property
    def search_was_truncated(self) -> bool:
        return self in {
            self.CANDIDATE_LIMIT_REACHED,
            self.SEARCH_TIME_LIMIT_REACHED,
            self.FINALIZATION_TIME_LIMIT_REACHED,
            self.USER_CANCELLED,
        }


class DiagnosticSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class DiagnosticPhase(str, Enum):
    REQUEST_VALIDATION = "request_validation"
    INPUT_NORMALIZATION = "input_normalization"
    RULE_LOADING = "rule_loading"
    CONSTRUCTION = "construction"
    SEARCH = "search"
    CORE_AUDIT = "core_audit"
    RESULT_ASSEMBLY = "result_assembly"
    RESULT_AUDIT = "result_audit"


@dataclass(frozen=True, slots=True)
class DiagnosticIssue:
    code: str
    phase: DiagnosticPhase
    field_path: str | None
    subject_id: str | None
    message: str
    severity: DiagnosticSeverity

    def __post_init__(self):
        require_text(self.code, "code")
        require_enum(self.phase, DiagnosticPhase, "phase")
        require_text(self.field_path, "field_path", allow_none=True)
        require_text(self.subject_id, "subject_id", allow_none=True)
        require_text(self.message, "message")
        require_enum(self.severity, DiagnosticSeverity, "severity")


@dataclass(frozen=True, slots=True)
class SolverPolicy:
    seed: int
    total_time_limit_seconds: Decimal
    finalization_reserve_seconds: Decimal
    candidate_check_limit: int
    construction_order_key: str
    numeric_semantics_key: str
    whole_chain_pair_scan_slack_weight: Decimal
    maximum_virtual_bridge_nodes: int = 2

    def __post_init__(self):
        require_int(self.seed, "seed", minimum=None)
        require_int(self.candidate_check_limit, "candidate_check_limit")
        require_decimal(self.total_time_limit_seconds, "total_time_limit_seconds", positive=True)
        require_decimal(
            self.finalization_reserve_seconds, "finalization_reserve_seconds", positive=True
        )
        if self.finalization_reserve_seconds >= self.total_time_limit_seconds:
            raise ValueError("finalization reserve must be smaller than total time")
        if self.construction_order_key != CONSTRUCTION_ORDER_KEY:
            raise ValueError("unknown construction_order_key")
        if self.numeric_semantics_key != NUMERIC_SEMANTICS_KEY:
            raise ValueError("unknown numeric_semantics_key")
        require_decimal(
            self.whole_chain_pair_scan_slack_weight,
            "whole_chain_pair_scan_slack_weight",
            nonnegative=True,
        )
        require_int(self.maximum_virtual_bridge_nodes, "maximum_virtual_bridge_nodes")
        if self.maximum_virtual_bridge_nodes > 2:
            raise ValueError("only zero, one or two bridge nodes are supported")


@dataclass(frozen=True, slots=True)
class SolveMetrics:
    graph_edge_check_count: int = 0
    graph_allowed_edge_count: int = 0
    matching_edge_count: int = 0
    path_count: int = 0
    initial_chain_count: int = 0
    candidate_check_count: int = 0
    complete_candidate_evaluation_count: int = 0
    accepted_move_count: int = 0
    accepted_split_count: int = 0
    accepted_same_period_split_count: int = 0
    accepted_future_borrow_return_count: int = 0
    final_chain_count: int = 0
    stage_duration_seconds: Mapping[str, Decimal] = field(default_factory=dict)

    def __post_init__(self):
        for name in self.__slots__:
            if name != "stage_duration_seconds":
                require_int(getattr(self, name), name)
        if self.accepted_split_count != (
            self.accepted_same_period_split_count + self.accepted_future_borrow_return_count
        ):
            raise ValueError("accepted split total does not match its mode counts")
        frozen = freeze_scalars(self.stage_duration_seconds)
        for name, seconds in frozen.items():
            require_decimal(seconds, name, nonnegative=True)
        object.__setattr__(self, "stage_duration_seconds", frozen)


class CoreAuditStatus(str, Enum):
    NOT_RUN = "not_run"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    TIME_LIMIT = "time_limit"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class CoreAuditReport:
    status: CoreAuditStatus
    passed: bool
    audited_evaluation_fingerprint: str | None
    invariant_failure_codes: tuple[str, ...]
    action_authorization_failure_codes: tuple[str, ...]
    derived_resource_fingerprint: str | None
    audited_split_count: int | None
    audited_same_period_split_count: int | None
    audited_future_borrow_return_count: int | None
    search_evaluation_matches: bool | None
    report_fingerprint: str

    def __post_init__(self):
        require_enum(self.status, CoreAuditStatus, "status")
        if type(self.passed) is not bool:
            raise ValueError("passed must be boolean")
        require_text(self.report_fingerprint, "report_fingerprint")
        for name in ("audited_evaluation_fingerprint", "derived_resource_fingerprint"):
            require_text(getattr(self, name), name, allow_none=True)
        for name in ("invariant_failure_codes", "action_authorization_failure_codes"):
            codes = freeze_tuple(getattr(self, name), str, name)
            for code in codes:
                require_text(code, name)
            object.__setattr__(self, name, codes)
        counts = (
            self.audited_split_count,
            self.audited_same_period_split_count,
            self.audited_future_borrow_return_count,
        )
        if self.status is not CoreAuditStatus.COMPLETED:
            if (
                self.passed
                or any(value is not None for value in counts)
                or self.search_evaluation_matches is not None
            ):
                raise ValueError("unfinished audit must not claim counts, a comparison or success")
            if (
                self.audited_evaluation_fingerprint is not None
                or self.derived_resource_fingerprint is not None
            ):
                raise ValueError("unfinished audit must not claim audited identities")
            return
        for count in counts:
            require_int(count, "audited split count")
        if counts[0] != counts[1] + counts[2]:
            raise ValueError("audited split total does not match its mode counts")
        if type(self.search_evaluation_matches) is not bool:
            raise ValueError("completed audit must contain a completed comparison")
        if self.passed and (
            not self.search_evaluation_matches
            or self.invariant_failure_codes
            or self.action_authorization_failure_codes
            or self.audited_evaluation_fingerprint is None
            or self.derived_resource_fingerprint is None
        ):
            raise ValueError("passed audit must be consistent and have all audited identities")


@dataclass(frozen=True, slots=True)
class CoreCandidateSnapshot:
    plan: SchedulePlan
    search_evaluation: PlanEvaluation

    def __post_init__(self):
        from .evaluation import PlanEvaluation
        from .model import SchedulePlan

        if not isinstance(self.plan, SchedulePlan) or not isinstance(
            self.search_evaluation, PlanEvaluation
        ):
            raise ValueError("core candidate requires a complete plan and PlanEvaluation")


@dataclass(frozen=True, slots=True)
class AuditedCoreRelease:
    canonical_plan: SchedulePlan
    audited_evaluation: PlanEvaluation
    resource_facts: PlanDerivedFacts
    plan_fingerprint: str
    evaluation_fingerprint: str
    resource_fingerprint: str
    core_audit_fingerprint: str
    release_fingerprint: str

    def __post_init__(self):
        from .evaluation import PlanEvaluation
        from .model import SchedulePlan
        from .resource_facts import PlanDerivedFacts

        if not isinstance(self.canonical_plan, SchedulePlan) or not isinstance(
            self.audited_evaluation, PlanEvaluation
        ):
            raise ValueError("core release requires a complete plan and PlanEvaluation")
        if not isinstance(self.resource_facts, PlanDerivedFacts):
            raise ValueError("core release requires typed audited resource facts")
        for name in (
            "plan_fingerprint",
            "evaluation_fingerprint",
            "resource_fingerprint",
            "core_audit_fingerprint",
            "release_fingerprint",
        ):
            require_text(getattr(self, name), name)
        if self.resource_fingerprint != self.resource_facts.facts_fingerprint:
            raise ValueError("core release must preserve the audited resource identity")


@dataclass(frozen=True, slots=True)
class SolverResult:
    status: SolveStatus
    stop_reason: SearchStopReason
    diagnostic_candidate: CoreCandidateSnapshot | None
    release: AuditedCoreRelease | None
    core_audit: CoreAuditReport
    metrics: SolveMetrics
    issues: tuple[DiagnosticIssue, ...]
    trace: tuple[AcceptedMoveTrace, ...]
    problem_fingerprint: str
    rule_set_fingerprint: str
    policy_fingerprint: str
    core_result_fingerprint: str

    def __post_init__(self):
        from .neighborhoods import AcceptedMoveTrace

        require_enum(self.status, SolveStatus, "status")
        require_enum(self.stop_reason, SearchStopReason, "stop_reason")
        for name, expected, optional in (
            ("diagnostic_candidate", CoreCandidateSnapshot, True),
            ("release", AuditedCoreRelease, True),
            ("core_audit", CoreAuditReport, False),
            ("metrics", SolveMetrics, False),
        ):
            value = getattr(self, name)
            if not (optional and value is None) and not isinstance(value, expected):
                raise ValueError(f"{name} must be {expected.__name__}")
        object.__setattr__(self, "issues", freeze_tuple(self.issues, DiagnosticIssue, "issues"))
        object.__setattr__(self, "trace", freeze_tuple(self.trace, AcceptedMoveTrace, "trace"))
        for name in (
            "problem_fingerprint",
            "rule_set_fingerprint",
            "policy_fingerprint",
            "core_result_fingerprint",
        ):
            require_text(getattr(self, name), name)
        publishable = self.status in {
            SolveStatus.SUCCESS,
            SolveStatus.PUBLISHABLE_WITH_ALLOWED_DEVIATION,
        }
        if publishable != (self.release is not None):
            raise ValueError("only publishable core statuses may carry a release")
        if (self.status is SolveStatus.CANCELLED) != (
            self.stop_reason is SearchStopReason.USER_CANCELLED
        ):
            raise ValueError("cancelled status and stop reason must agree")
        if self.status is SolveStatus.NO_COMPLETE_PLAN and self.diagnostic_candidate is not None:
            raise ValueError("no-complete-plan status cannot carry a complete candidate")
        if (
            self.status is SolveStatus.COMPLETE_NOT_PUBLISHABLE
            and self.diagnostic_candidate is None
        ):
            raise ValueError("complete-not-publishable status requires a diagnostic candidate")
        if not publishable:
            return
        if (
            self.diagnostic_candidate is None
            or self.core_audit.status is not CoreAuditStatus.COMPLETED
            or not self.core_audit.passed
            or self.stop_reason
            in {
                SearchStopReason.FINALIZATION_TIME_LIMIT_REACHED,
                SearchStopReason.INPUT_INVALID,
                SearchStopReason.SYSTEM_ERROR,
            }
            or any(issue.severity is DiagnosticSeverity.ERROR for issue in self.issues)
        ):
            raise ValueError("core release requires an audited candidate without terminal failure")
        release = self.release
        if (
            release.canonical_plan != self.diagnostic_candidate.plan
            or release.core_audit_fingerprint != self.core_audit.report_fingerprint
            or release.evaluation_fingerprint != self.core_audit.audited_evaluation_fingerprint
            or release.resource_fingerprint != self.core_audit.derived_resource_fingerprint
        ):
            raise ValueError("core result must preserve its audited release bindings")
        if (self.status is SolveStatus.SUCCESS) != (not release.audited_evaluation.violations):
            raise ValueError("core success and allowed-deviation statuses must reflect audit facts")
