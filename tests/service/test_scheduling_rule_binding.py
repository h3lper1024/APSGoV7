from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
from threading import Event

import pytest

from apsgo_scheduler.api.request import PeriodInput
from apsgo_scheduler.api.rule_management import SetActiveRulesRequest
from apsgo_scheduler.app import solve_request
from apsgo_scheduler.core.contracts import SolveStatus, fingerprint
from apsgo_scheduler.core.model import MaterialRole
from apsgo_v7_service import scheduling
from apsgo_v7_service.gqga4 import (
    GQGA4_INITIAL_RULES,
    GQGA4_INITIAL_VIRTUAL_PROTOTYPES,
)
from apsgo_v7_service.grade_dictionary import GradePreparationError
from apsgo_v7_service.rule_management import (
    RuleManagementServiceError,
    initialize_gqga4_rules,
    set_active_gqga4_rules,
)
from apsgo_v7_service.rule_store import RuleStore
from tests.app.test_input_normalizer import make_order, make_request, make_spec
from tests.service.grade_dictionary_support import sample_grade_dictionary


def _base_task_input():
    attributes = {
        "surface_grade": None,
        "grade_class": "普通钢",
        "hot_roll_grade": "DC01",
        "soft_hard_class": None,
        "customer_name": None,
    }
    orders = (
        make_order(
            0,
            source_period="P0",
            weight=Decimal("600"),
            width=Decimal("1000"),
            grade="DC01",
            material_role=MaterialRole.NORMAL_REAL,
            rule_attributes=attributes,
        ),
        make_order(
            1,
            source_period="P0",
            weight=Decimal("600"),
            width=Decimal("990"),
            grade="DC01",
            material_role=MaterialRole.NORMAL_REAL,
            rule_attributes=attributes,
        ),
    )
    request = make_request(
        rule_set_spec=make_spec(),
        orders=orders,
        periods=(PeriodInput("P0", 0),),
    )
    return scheduling.SchedulingTaskInput(
        contract_version=request.contract_version,
        request_id=request.request_id,
        product_line_code="GQGA4",
        process_code="default",
        scenario="month",
        orders=request.orders,
        periods=request.periods,
        policy=request.policy,
    )


def _changed_rules():
    result = []
    for rule in GQGA4_INITIAL_RULES:
        if rule.rule_id == "chain_weight_range":
            rule = replace(
                rule,
                parameters={**rule.parameters, "min_weight": Decimal("701")},
            )
        result.append(rule)
    return tuple(result)


def _consume_unrelated_version_id(database_path):
    stamp = "2026-09-07T00:00:00.000000+00:00"
    with RuleStore.initialize(database_path) as store:
        with store.transaction(write=True):
            rule_set_id = store.create_rule_set(
                "OTHER",
                "default",
                "month",
                created_at=stamp,
            )
            store.create_version(
                rule_set_id,
                1,
                None,
                "unrelated-version",
                "0" * 64,
                "{}",
                "1" * 64,
                "[]",
                "{}",
                "",
                "test",
                stamp,
                "test",
                stamp,
            )


def test_binding_constructs_request_from_database_snapshot_and_records_version(tmp_path):
    database_path = tmp_path / "rules.sqlite3"
    _consume_unrelated_version_id(database_path)
    active = initialize_gqga4_rules(
        database_path, initial_grade_dictionary=sample_grade_dictionary()
    ).active_rules
    task_input = _base_task_input()

    task = scheduling.bind_gqga4_scheduling_task(task_input, database_path)

    assert task.active_rule_set_version_id == active.active_version_id == 2
    assert task.task_input_fingerprint == fingerprint(task_input)
    assert task.active_rule_set_version_id != int(task.request.rule_set_spec.version)
    assert task.rule_set_fingerprint == task.request.rule_set_spec.fingerprint
    assert task.grade_dictionary_fingerprint == sample_grade_dictionary().dictionary_fingerprint
    assert task.request.rule_set_spec == active.rule_set_spec
    assert task.request.virtual_prototypes == active.virtual_prototypes
    assert task.preparation_report.input_order_count == 2
    assert task.preparation_report.matched_order_count == 2
    assert task.preparation_report.missing_order_count == 0
    assert task.preparation_report.missing_grades == ()
    assert task.preparation_report.grade_dictionary_fingerprint == (
        task.grade_dictionary_fingerprint
    )
    assert task.request_fingerprint != "" and task.binding_fingerprint != ""
    assert task.request.orders != task_input.orders
    assert all(
        order.rule_attributes["soft_hard_class"] == "软钢"
        for order in task.request.orders
    )
    assert all(
        order.rule_attributes["soft_hard_class"] is None
        for order in task_input.orders
    )
    assert task.request.periods == task_input.periods
    assert task.request.policy == task_input.policy
    assert not hasattr(task_input, "rule_set_spec")
    assert not hasattr(task_input, "virtual_prototypes")
    with pytest.raises(FrozenInstanceError):
        task.active_rule_set_version_id = 9
    with pytest.raises(ValueError, match="does not match"):
        replace(task, rule_set_fingerprint="wrong")
    with pytest.raises(ValueError, match="does not match the bound dictionary"):
        replace(task, grade_dictionary_fingerprint="wrong")
    assert task.binding_fingerprint == fingerprint(
        (
            ("active_rule_set_version_id", task.active_rule_set_version_id),
            ("task_input_fingerprint", task.task_input_fingerprint),
            ("rule_set_version", task.request.rule_set_spec.version),
            ("rule_set_fingerprint", task.rule_set_fingerprint),
            ("quality_spec", task.request.rule_set_spec.quality_spec),
            ("grade_dictionary_fingerprint", task.grade_dictionary_fingerprint),
            (
                "grade_preparation_report_fingerprint",
                task.preparation_report.report_fingerprint,
            ),
            ("request_fingerprint", task.request_fingerprint),
        )
    )


def test_wrong_identity_fails_before_reading_the_rule_store(tmp_path, monkeypatch):
    task_input = replace(_base_task_input(), product_line_code="OTHER")
    calls = []

    def unexpected_read(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("rule store must not be read")

    monkeypatch.setattr(
        scheduling,
        "get_active_gqga4_scheduling_snapshot",
        unexpected_read,
    )
    with pytest.raises(ValueError, match="GQGA4/default/month"):
        scheduling.bind_gqga4_scheduling_task(task_input, tmp_path / "must-not-be-read.sqlite3")
    assert calls == []


def test_binding_failure_does_not_fall_back_or_start_the_solver(tmp_path, monkeypatch):
    task_input = _base_task_input()
    solve_calls = []

    def failed_read(*args, **kwargs):
        raise RuleManagementServiceError(
            "stored_snapshot_inconsistent",
            "活动规则版本损坏。",
        )

    monkeypatch.setattr(
        scheduling,
        "get_active_gqga4_scheduling_snapshot",
        failed_read,
    )
    monkeypatch.setattr(scheduling, "solve_request", lambda *args: solve_calls.append(args))

    with pytest.raises(RuleManagementServiceError) as caught:
        scheduling.solve_gqga4_scheduling_task(task_input, tmp_path / "must-not-be-read.sqlite3")

    assert caught.value.code == "stored_snapshot_inconsistent"
    assert solve_calls == []


def test_bound_request_solves_after_all_rule_store_access_is_disabled(tmp_path, monkeypatch):
    database_path = tmp_path / "rules.sqlite3"
    initialize_gqga4_rules(
        database_path, initial_grade_dictionary=sample_grade_dictionary()
    )
    task = scheduling.bind_gqga4_scheduling_task(_base_task_input(), database_path)

    def forbidden_open(*args, **kwargs):
        raise AssertionError("search must not open the rule store")

    monkeypatch.setattr(RuleStore, "open", classmethod(forbidden_open))
    result = solve_request(task.request)

    assert result.status is SolveStatus.SUCCESS
    assert result.release is not None
    assert result.release.evaluation.violations == ()
    assert result.core_audit.passed and result.audit_report.passed
    assert result.run_manifest.request_fingerprint == task.request_fingerprint
    assert result.run_manifest.rule_set_fingerprint == task.rule_set_fingerprint
    assert all(
        node.rule_attributes["soft_hard_class"] == "软钢"
        for chain in result.release.plan.chains
        for node in chain.nodes
        if node.material_role is not MaterialRole.GENERATED_VIRTUAL
    )


def test_running_task_keeps_version_a_while_a_new_task_gets_version_b(tmp_path, monkeypatch):
    database_path = tmp_path / "rules.sqlite3"
    first = initialize_gqga4_rules(
        database_path, initial_grade_dictionary=sample_grade_dictionary()
    ).active_rules
    task_input = _base_task_input()
    entered_solver = Event()
    release_solver = Event()
    captured_requests = []
    read_count = 0
    original_read = scheduling.get_active_gqga4_scheduling_snapshot
    original_solve = scheduling.solve_request

    def counted_read(*args, **kwargs):
        nonlocal read_count
        read_count += 1
        return original_read(*args, **kwargs)

    def paused_solve(bound_request, cancellation=None):
        captured_requests.append(bound_request)
        entered_solver.set()
        assert release_solver.wait(10)
        return original_solve(bound_request, cancellation)

    monkeypatch.setattr(
        scheduling,
        "get_active_gqga4_scheduling_snapshot",
        counted_read,
    )
    monkeypatch.setattr(scheduling, "solve_request", paused_solve)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future_a = pool.submit(
            scheduling.solve_gqga4_scheduling_task,
            task_input,
            database_path,
        )
        assert entered_solver.wait(10)
        second = set_active_gqga4_rules(
            SetActiveRulesRequest(
                save_operation_id="00000000-0000-4000-8000-000000000701",
                expected_active_version_id=first.active_version_id,
                rules=_changed_rules(),
                virtual_prototypes=GQGA4_INITIAL_VIRTUAL_PROTOTYPES,
                remark="任务运行期间启用新版本",
            ),
            database_path,
        ).active_rules
        release_solver.set()
        result_a = future_a.result(timeout=10)

    result_b = scheduling.solve_gqga4_scheduling_task(task_input, database_path)

    assert read_count == 2
    assert result_a.active_rule_set_version_id == first.active_version_id
    assert result_a.task_input_fingerprint == fingerprint(task_input)
    assert result_a.rule_set_version == first.rule_set_spec.version
    assert result_a.quality_spec == first.rule_set_spec.quality_spec
    assert result_a.rule_set_fingerprint == first.rule_set_spec.fingerprint
    assert result_b.active_rule_set_version_id == second.active_version_id
    assert result_b.task_input_fingerprint == fingerprint(task_input)
    assert result_b.rule_set_version == second.rule_set_spec.version
    assert result_b.quality_spec == second.rule_set_spec.quality_spec
    assert result_b.rule_set_fingerprint == second.rule_set_spec.fingerprint
    assert result_a.rule_set_fingerprint != result_b.rule_set_fingerprint
    assert result_a.grade_dictionary_fingerprint == result_b.grade_dictionary_fingerprint
    assert result_a.preparation_report.matched_order_count == 2
    assert result_b.preparation_report.matched_order_count == 2
    assert captured_requests[0].rule_set_spec == first.rule_set_spec
    assert captured_requests[1].rule_set_spec == second.rule_set_spec
    for result in (result_a, result_b):
        assert result.result.status is SolveStatus.SUCCESS
        assert result.result.run_manifest.rule_set_fingerprint == result.rule_set_fingerprint
        assert result.result.run_manifest.request_fingerprint == result.request_fingerprint
        assert result.bound_result_fingerprint != ""
        assert all(
            order.rule_attributes["soft_hard_class"] == "软钢"
            for order in captured_requests.pop(0).orders
        )
    with pytest.raises(ValueError, match="binding identity"):
        replace(result_a, active_rule_set_version_id=result_b.active_rule_set_version_id)
    with pytest.raises(ValueError, match="binding identity"):
        replace(result_a, task_input_fingerprint="wrong")
    with pytest.raises(ValueError, match="binding identity"):
        replace(result_a, rule_set_version="999")
    with pytest.raises(ValueError, match="binding identity"):
        replace(result_a, quality_spec=tuple(reversed(result_a.quality_spec)))
    with pytest.raises(ValueError, match="bound dictionary"):
        replace(result_a, grade_dictionary_fingerprint="wrong")
    different_report = replace(
        result_a.preparation_report,
        input_order_count=3,
        matched_order_count=3,
    )
    with pytest.raises(ValueError, match="binding identity"):
        replace(result_a, preparation_report=different_report)


def test_missing_grade_is_reported_but_can_still_use_the_existing_rule_fallback(tmp_path):
    database_path = tmp_path / "rules.sqlite3"
    initialize_gqga4_rules(
        database_path,
        initial_grade_dictionary=sample_grade_dictionary(),
    )
    original = _base_task_input()
    missing_orders = tuple(replace(order, grade="UNKNOWN") for order in original.orders)
    task_input = replace(original, orders=missing_orders)

    result = scheduling.solve_gqga4_scheduling_task(task_input, database_path)

    assert result.preparation_report.matched_order_count == 0
    assert result.preparation_report.missing_order_count == 2
    assert tuple(
        item.source_order_id for item in result.preparation_report.missing_grades
    ) == tuple(sorted(order.source_order_id for order in missing_orders))
    assert result.result.status is SolveStatus.SUCCESS
    assert result.result.core_audit.passed and result.result.audit_report.passed


def test_existing_matching_classification_passes_and_conflict_stops_before_solver(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "rules.sqlite3"
    initialize_gqga4_rules(
        database_path,
        initial_grade_dictionary=sample_grade_dictionary(),
    )
    original = _base_task_input()

    def with_classification(value):
        return replace(
            original,
            orders=tuple(
                replace(
                    order,
                    rule_attributes={**order.rule_attributes, "soft_hard_class": value},
                )
                for order in original.orders
            ),
        )

    matching = scheduling.bind_gqga4_scheduling_task(
        with_classification("软钢"),
        database_path,
    )
    assert matching.preparation_report.matched_order_count == 2

    solve_calls = []
    monkeypatch.setattr(scheduling, "solve_request", lambda *args: solve_calls.append(args))
    with pytest.raises(GradePreparationError) as caught:
        scheduling.solve_gqga4_scheduling_task(
            with_classification("硬钢"),
            database_path,
        )
    assert {issue.code for issue in caught.value.issues} == {"soft_hard_class_conflict"}
    assert solve_calls == []
