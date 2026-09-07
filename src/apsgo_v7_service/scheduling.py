"""Bind one verified active rule snapshot to a scheduling task and solve it."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from apsgo_scheduler.api.request import (
    OrderInput,
    PeriodInput,
    SchedulingRequest,
    fingerprint_public_request,
)
from apsgo_scheduler.api.result import SchedulingResult
from apsgo_scheduler.app.service import solve_request
from apsgo_scheduler.core.contracts import (
    SolverPolicy,
    fingerprint,
    freeze_tuple,
    require_int,
    require_text,
)

from .grade_dictionary import (
    GradePreparationReport,
    prepare_orders_with_grade_dictionary,
)
from .gqga4 import GQGA4_RULE_SET_TEMPLATE
from .rule_management import get_active_gqga4_scheduling_snapshot


@dataclass(frozen=True, slots=True)
class SchedulingTaskInput:
    """Scheduling inputs supplied before a database rule snapshot is selected."""

    contract_version: str
    request_id: str
    product_line_code: str
    process_code: str
    scenario: str
    orders: tuple[OrderInput, ...]
    periods: tuple[PeriodInput, ...]
    policy: SolverPolicy

    def __post_init__(self):
        for name in (
            "contract_version",
            "request_id",
            "product_line_code",
            "process_code",
            "scenario",
        ):
            require_text(getattr(self, name), name)
        object.__setattr__(self, "orders", freeze_tuple(self.orders, OrderInput, "orders"))
        object.__setattr__(self, "periods", freeze_tuple(self.periods, PeriodInput, "periods"))
        if not isinstance(self.policy, SolverPolicy):
            raise ValueError("policy must be SolverPolicy")


@dataclass(frozen=True, slots=True)
class BoundSchedulingTask:
    """A task request paired with the database version that supplied its rules."""

    active_rule_set_version_id: int
    rule_set_fingerprint: str
    grade_dictionary_fingerprint: str
    preparation_report: GradePreparationReport
    request: SchedulingRequest
    request_fingerprint: str = field(init=False)
    binding_fingerprint: str = field(init=False)

    def __post_init__(self):
        require_int(self.active_rule_set_version_id, "active_rule_set_version_id", minimum=1)
        require_text(self.rule_set_fingerprint, "rule_set_fingerprint")
        require_text(self.grade_dictionary_fingerprint, "grade_dictionary_fingerprint")
        if not isinstance(self.preparation_report, GradePreparationReport):
            raise ValueError("preparation_report must be GradePreparationReport")
        if (
            self.preparation_report.grade_dictionary_fingerprint
            != self.grade_dictionary_fingerprint
        ):
            raise ValueError("grade preparation report does not match the bound dictionary")
        if not isinstance(self.request, SchedulingRequest):
            raise ValueError("request must be SchedulingRequest")
        if self.preparation_report.input_order_count != len(self.request.orders):
            raise ValueError("grade preparation report does not match the bound orders")
        if self.request.rule_set_spec.fingerprint != self.rule_set_fingerprint:
            raise ValueError("bound task rule fingerprint does not match its request")
        request_fingerprint = fingerprint_public_request(self.request)
        object.__setattr__(self, "request_fingerprint", request_fingerprint)
        object.__setattr__(
            self,
            "binding_fingerprint",
            fingerprint(
                (
                    ("active_rule_set_version_id", self.active_rule_set_version_id),
                    ("rule_set_fingerprint", self.rule_set_fingerprint),
                    ("grade_dictionary_fingerprint", self.grade_dictionary_fingerprint),
                    (
                        "grade_preparation_report_fingerprint",
                        self.preparation_report.report_fingerprint,
                    ),
                    ("request_fingerprint", request_fingerprint),
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class BoundSchedulingResult:
    """A solver result with the exact rule-version provenance retained outside the core."""

    active_rule_set_version_id: int
    rule_set_fingerprint: str
    grade_dictionary_fingerprint: str
    preparation_report: GradePreparationReport
    request_fingerprint: str
    binding_fingerprint: str
    result: SchedulingResult
    bound_result_fingerprint: str = field(init=False)

    def __post_init__(self):
        require_int(self.active_rule_set_version_id, "active_rule_set_version_id", minimum=1)
        for name in (
            "rule_set_fingerprint",
            "grade_dictionary_fingerprint",
            "request_fingerprint",
            "binding_fingerprint",
        ):
            require_text(getattr(self, name), name)
        if not isinstance(self.preparation_report, GradePreparationReport):
            raise ValueError("preparation_report must be GradePreparationReport")
        if (
            self.preparation_report.grade_dictionary_fingerprint
            != self.grade_dictionary_fingerprint
        ):
            raise ValueError("grade preparation report does not match the bound dictionary")
        if not isinstance(self.result, SchedulingResult):
            raise ValueError("result must be SchedulingResult")
        expected_binding = fingerprint(
            (
                ("active_rule_set_version_id", self.active_rule_set_version_id),
                ("rule_set_fingerprint", self.rule_set_fingerprint),
                ("grade_dictionary_fingerprint", self.grade_dictionary_fingerprint),
                (
                    "grade_preparation_report_fingerprint",
                    self.preparation_report.report_fingerprint,
                ),
                ("request_fingerprint", self.request_fingerprint),
            )
        )
        if self.binding_fingerprint != expected_binding:
            raise ValueError("bound result does not preserve the task binding identity")
        manifest = self.result.run_manifest
        if manifest.request_fingerprint != self.request_fingerprint:
            raise ValueError("bound result does not describe the bound request")
        if manifest.rule_set_fingerprint not in (None, self.rule_set_fingerprint):
            raise ValueError("bound result rule fingerprint does not match the bound task")
        object.__setattr__(
            self,
            "bound_result_fingerprint",
            fingerprint(
                (
                    ("active_rule_set_version_id", self.active_rule_set_version_id),
                    ("rule_set_fingerprint", self.rule_set_fingerprint),
                    ("grade_dictionary_fingerprint", self.grade_dictionary_fingerprint),
                    (
                        "grade_preparation_report_fingerprint",
                        self.preparation_report.report_fingerprint,
                    ),
                    ("request_fingerprint", self.request_fingerprint),
                    ("binding_fingerprint", self.binding_fingerprint),
                    ("result_fingerprint", self.result.result_fingerprint),
                )
            ),
        )


def bind_gqga4_scheduling_task(
    task_input: SchedulingTaskInput,
    database_path: str | Path,
    *,
    timeout_seconds: float = 5.0,
) -> BoundSchedulingTask:
    """Read one verified active snapshot and construct the complete solver request."""

    if not isinstance(task_input, SchedulingTaskInput):
        raise ValueError("task_input must be SchedulingTaskInput")
    template = GQGA4_RULE_SET_TEMPLATE
    identity = (
        task_input.product_line_code,
        task_input.process_code,
        task_input.scenario,
    )
    expected = (template.product_line_code, template.process_code, template.scenario)
    if identity != expected:
        raise ValueError("task_input must target the GQGA4/default/month rule set")

    active = get_active_gqga4_scheduling_snapshot(
        database_path,
        timeout_seconds=timeout_seconds,
    )
    prepared_orders = prepare_orders_with_grade_dictionary(
        task_input.orders,
        active.grade_dictionary,
    )
    active_rules = active.active_rules
    bound_request = SchedulingRequest(
        contract_version=task_input.contract_version,
        request_id=task_input.request_id,
        product_line_code=task_input.product_line_code,
        process_code=task_input.process_code,
        scenario=task_input.scenario,
        orders=prepared_orders.orders,
        periods=task_input.periods,
        rule_set_spec=active_rules.rule_set_spec,
        virtual_prototypes=active_rules.virtual_prototypes,
        policy=task_input.policy,
    )
    return BoundSchedulingTask(
        active_rule_set_version_id=active_rules.active_version_id,
        rule_set_fingerprint=active_rules.rule_set_spec.fingerprint,
        grade_dictionary_fingerprint=active.grade_dictionary.dictionary_fingerprint,
        preparation_report=prepared_orders.report,
        request=bound_request,
    )


def solve_gqga4_scheduling_task(
    task_input: SchedulingTaskInput,
    database_path: str | Path,
    *,
    timeout_seconds: float = 5.0,
    cancellation=None,
) -> BoundSchedulingResult:
    """Bind once, close the database read, then delegate once to the existing solver entry."""

    task = bind_gqga4_scheduling_task(
        task_input,
        database_path,
        timeout_seconds=timeout_seconds,
    )
    result = solve_request(task.request, cancellation)
    return BoundSchedulingResult(
        active_rule_set_version_id=task.active_rule_set_version_id,
        rule_set_fingerprint=task.rule_set_fingerprint,
        grade_dictionary_fingerprint=task.grade_dictionary_fingerprint,
        preparation_report=task.preparation_report,
        request_fingerprint=task.request_fingerprint,
        binding_fingerprint=task.binding_fingerprint,
        result=result,
    )


__all__ = [
    "BoundSchedulingResult",
    "BoundSchedulingTask",
    "SchedulingTaskInput",
    "bind_gqga4_scheduling_task",
    "solve_gqga4_scheduling_task",
]
