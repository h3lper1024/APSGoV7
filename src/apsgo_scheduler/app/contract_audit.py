"""Check the public mapping against audited core values, without evaluating rules."""

import logging
from collections import defaultdict
from dataclasses import fields

from ..api.request import SchedulingRequest, fingerprint_public_request
from ..api.result import ResultAuditReport, ResultAuditStatus
from ..core.budget import SolveRuntimeBudget
from ..core.contracts import (
    ControlledSplitMode,
    CoreAuditStatus,
    DiagnosticSeverity,
    RuleScope,
    SearchStopReason,
    SolverResult,
    SolveStatus,
    fingerprint,
    sum_weights,
)
from ..core.model import MaterialRole, SchedulingProblem, VirtualPurpose
from ..core.rules.base import RuleDisposition
from .result_assembler import DraftSchedulingResult, fingerprint_draft_result


def _values(value, omitted=()):
    return {
        item.name: getattr(value, item.name) for item in fields(value) if item.name not in omitted
    }


def _same_fields(left, right, names):
    return all(getattr(left, name) == getattr(right, name) for name in names)


def _request_binding(request, problem, check, runtime):
    check(
        _same_fields(request, problem, ("product_line_code", "process_code", "scenario")),
        "request_problem_mapping_mismatch",
    )
    check(len(request.orders) == len(problem.nodes), "request_problem_mapping_mismatch")
    # Verify identities and unchanged values, not a second attribute-normalization pipeline.
    for raw, normalized in zip(request.orders, problem.nodes):
        if not runtime.allows_finalization():
            return False
        check(
            all(
                getattr(raw, name).strip() == getattr(normalized, name)
                for name in ("node_id", "source_order_id", "source_resource_id", "source_period")
            )
            and _same_fields(
                raw,
                normalized,
                (
                    "weight",
                    "material_role",
                    "width",
                    "thickness",
                    "min_temperature",
                    "max_temperature",
                ),
            ),
            "request_problem_mapping_mismatch",
        )
    check(
        tuple(
            item.period_id.strip()
            for item in sorted(request.periods, key=lambda item: item.sequence)
        )
        == problem.period_order,
        "request_problem_mapping_mismatch",
    )
    check(
        len(request.virtual_prototypes) == len(problem.virtual_prototypes),
        "request_problem_mapping_mismatch",
    )
    for raw, normalized in zip(request.virtual_prototypes, problem.virtual_prototypes):
        if not runtime.allows_finalization():
            return False
        check(
            raw.prototype_id.strip() == normalized.prototype_id
            and _same_fields(
                raw,
                normalized,
                ("unit_weight", "width", "thickness", "min_temperature", "max_temperature"),
            ),
            "request_problem_mapping_mismatch",
        )
    return True


def _fact_mappings(plan, facts, problem, check, runtime):
    locations, real_weights = {}, defaultdict(list)
    pieces, separators = defaultdict(list), defaultdict(list)
    original = {node.node_id: node for node in problem.nodes}
    periods = {period: index for index, period in enumerate(problem.period_order)}
    chain_ids = []
    for chain in plan.chains:
        if not runtime.allows_finalization():
            return False
        chain_ids.append(chain.chain_id)
        check(chain.assigned_period in periods, "plan_mapping_mismatch")
        for position, node in enumerate(chain.nodes):
            if not runtime.allows_finalization():
                return False
            check(node.node_id not in locations, "plan_mapping_mismatch")
            locations[node.node_id] = (node, chain, position)
            if node.material_role is MaterialRole.GENERATED_VIRTUAL:
                if node.virtual_lineage.purpose is VirtualPurpose.SPLIT_SEPARATOR:
                    separators[node.virtual_lineage.related_partition_id].append(node)
            else:
                parent_id = (
                    node.node_id
                    if node.split_lineage is None
                    else node.split_lineage.parent_node_id
                )
                parent = original.get(parent_id)
                check(
                    parent is not None
                    and _same_fields(
                        node,
                        parent,
                        ("source_order_id", "source_resource_id", "source_period", "material_role"),
                    ),
                    "source_weight_mismatch",
                )
                real_weights[parent_id].append(node.weight)
                if node.split_lineage is not None:
                    pieces[node.split_lineage.partition_id].append(node)
    check(len(chain_ids) == len(set(chain_ids)), "plan_mapping_mismatch")
    check(set(real_weights) == set(original), "source_weight_mismatch")
    for parent_id, parent in original.items():
        if not runtime.allows_finalization():
            return False
        check(sum_weights(real_weights[parent_id]) == parent.weight, "source_weight_mismatch")

    def borrowed(node, chain):
        return (
            node.material_role is not MaterialRole.GENERATED_VIRTUAL
            and periods[node.source_period] > periods[chain.assigned_period]
        )

    collections = (
        (facts.assignments, "node_assignment_mismatch", lambda node, chain: True),
        (facts.future_borrows, "future_borrow_mapping_mismatch", borrowed),
        (
            facts.virtual_generations,
            "virtual_generation_mapping_mismatch",
            lambda node, chain: node.material_role is MaterialRole.GENERATED_VIRTUAL,
        ),
        (
            facts.actual_transitions,
            "actual_transition_mapping_mismatch",
            lambda node, chain: node.material_role is MaterialRole.ACTUAL_TRANSITION,
        ),
    )
    for rows, code, belongs in collections:
        if not runtime.allows_finalization():
            return False
        expected = tuple(
            node.node_id for node, chain, _ in locations.values() if belongs(node, chain)
        )
        check(tuple(row.node_id for row in rows) == expected, code)
        for row in rows:
            if not runtime.allows_finalization():
                return False
            location = locations.get(row.node_id)
            if location is None:
                check(False, code)
                continue
            node, chain, position = location
            values = {
                "node_id": node.node_id,
                "source_order_id": node.source_order_id,
                "source_resource_id": node.source_resource_id,
                "source_period": node.source_period,
                "material_role": node.material_role,
                "chain_id": chain.chain_id,
                "assigned_period": chain.assigned_period,
                "position": position,
                "weight": node.weight,
            }
            if node.virtual_lineage is not None:
                values.update(_values(node.virtual_lineage))
            check(
                all(getattr(row, item.name) == values.get(item.name) for item in fields(row)), code
            )

    if not _split_mappings(facts, pieces, separators, original, locations, check, runtime):
        return False
    nodes = tuple(node for node, _, _ in locations.values())
    real = tuple(node for node in nodes if node.material_role is not MaterialRole.GENERATED_VIRTUAL)
    totals = {
        "input_real_weight": sum_weights(node.weight for node in problem.nodes),
        "scheduled_real_weight": sum_weights(node.weight for node in real),
        "generated_virtual_weight": sum_weights(
            node.weight for node in nodes if node.material_role is MaterialRole.GENERATED_VIRTUAL
        ),
        "future_pool_weight": sum_weights(
            node.weight for node in real if periods[node.source_period] > 0
        ),
        "borrowed_future_weight": sum_weights(
            node.weight for node, chain, _ in locations.values() if borrowed(node, chain)
        ),
    }
    check(
        all(getattr(facts, name) == value for name, value in totals.items()),
        "resource_totals_mismatch",
    )
    return runtime.allows_finalization()


def _split_mappings(facts, pieces, separators, original, locations, check, runtime):
    partitions = facts.split_partitions
    check(
        len(partitions) == len(pieces)
        and {item.partition_id for item in partitions} == set(pieces)
        and set(separators) <= set(pieces),
        "split_partition_mapping_mismatch",
    )
    check(
        tuple(item.accepted_source_sequence for item in partitions)
        == tuple(range(1, len(partitions) + 1)),
        "split_partition_mapping_mismatch",
    )
    for fact in partitions:
        if not runtime.allows_finalization():
            return False
        parent = original.get(fact.parent_node_id)
        ordered = sorted(pieces[fact.partition_id], key=lambda node: node.split_lineage.piece_index)
        bridges = sorted(
            separators[fact.partition_id], key=lambda node: node.virtual_lineage.accepted_sequence
        )
        check(
            parent is not None
            and fact.parent_source_order_id == parent.source_order_id
            and fact.source_resource_id == parent.source_resource_id
            and fact.source_period == parent.source_period
            and fact.parent_weight == parent.weight,
            "split_partition_mapping_mismatch",
        )
        check(
            fact.piece_node_ids == tuple(node.node_id for node in ordered)
            and fact.piece_weights == tuple(node.weight for node in ordered)
            and fact.separator_virtual_node_ids == tuple(node.node_id for node in bridges)
            and sum_weights(fact.piece_weights) == fact.parent_weight
            and fact.partition_fingerprint == fingerprint(fact.fingerprint_payload()),
            "split_partition_mapping_mismatch",
        )
        for index, node in enumerate(ordered, start=1):
            if not runtime.allows_finalization():
                return False
            lineage = node.split_lineage
            shared = tuple(
                item.name
                for item in fields(lineage)
                if item.name not in {"piece_index", "piece_count"}
            )
            check(
                _same_fields(lineage, fact, shared)
                and lineage.piece_index == index
                and lineage.piece_count == len(ordered)
                and locations[node.node_id][1].assigned_period == fact.target_assigned_period,
                "split_partition_mapping_mismatch",
            )
    return True


def _report(status, codes, draft_fingerprint, plan_fingerprint=None, resource_fingerprint=None):
    values = dict(
        status=status,
        passed=status is ResultAuditStatus.COMPLETED and not codes,
        failure_codes=tuple(codes),
        plan_fingerprint=plan_fingerprint,
        resource_fingerprint=resource_fingerprint,
        draft_fingerprint=draft_fingerprint,
    )
    return ResultAuditReport(**values, report_fingerprint=fingerprint(values))


def audit_result_contract(draft, request, problem, core_result, runtime) -> ResultAuditReport:
    """Audit mapping only; the core remains the sole rule and resource-fact producer."""
    for value, expected in (
        (draft, DraftSchedulingResult),
        (request, SchedulingRequest),
        (problem, SchedulingProblem),
        (core_result, SolverResult),
        (runtime, SolveRuntimeBudget),
    ):
        if not isinstance(value, expected):
            raise ValueError(f"result audit requires {expected.__name__}")
    codes = []

    def check(condition, code):
        if not condition and code not in codes:
            codes.append(code)

    def interrupted():
        status = {
            SearchStopReason.USER_CANCELLED: ResultAuditStatus.CANCELLED,
            SearchStopReason.FINALIZATION_TIME_LIMIT_REACHED: ResultAuditStatus.TIME_LIMIT,
        }.get(runtime.stop_reason, ResultAuditStatus.ERROR)
        return _report(status, (), draft.draft_fingerprint)

    try:
        if not runtime.allows_finalization():
            return interrupted()
        check(
            draft.draft_fingerprint == fingerprint_draft_result(draft), "draft_fingerprint_mismatch"
        )
        check(
            draft.contract_version == request.contract_version
            and draft.request_id == request.request_id
            and draft.run_manifest.request_fingerprint == fingerprint_public_request(request),
            "request_identity_mismatch",
        )
        check(
            problem.input_fingerprint
            == fingerprint(_values(problem, ("problem_id", "input_fingerprint")))
            == core_result.problem_fingerprint
            == draft.run_manifest.problem_fingerprint,
            "problem_identity_mismatch",
        )
        check(
            request.rule_set_spec.fingerprint
            == core_result.rule_set_fingerprint
            == draft.run_manifest.rule_set_fingerprint
            and fingerprint(request.policy)
            == core_result.policy_fingerprint
            == draft.run_manifest.policy_fingerprint,
            "manifest_binding_mismatch",
        )
        metrics = _values(core_result.metrics, ("stage_duration_seconds",))
        core_values = _values(core_result, ("core_result_fingerprint",))
        core_values["metrics"] = metrics
        check(
            core_result.core_result_fingerprint == fingerprint(core_values),
            "core_result_identity_mismatch",
        )
        check(draft.metrics == core_result.metrics, "metrics_mismatch")
        check(
            draft.run_manifest.counters == metrics
            and draft.run_manifest.trace_fingerprint == fingerprint(core_result.trace)
            and draft.run_manifest.stop_reason is core_result.stop_reason
            and draft.run_manifest.diagnostic_codes == tuple(issue.code for issue in draft.issues)
            and draft.issues == core_result.issues,
            "manifest_binding_mismatch",
        )
        check(
            draft.diagnostic_candidate is core_result.diagnostic_candidate,
            "diagnostic_candidate_mismatch",
        )
        check(
            draft.core_audit == core_result.core_audit
            and draft.core_audit.status is CoreAuditStatus.COMPLETED
            and draft.core_audit.passed
            and draft.core_audit.search_evaluation_matches,
            "core_audit_binding_mismatch",
        )
        release = core_result.release
        proposed = draft.proposed_release
        check(release is not None, "core_release_unavailable")
        if release is not None:
            check(proposed.plan == release.canonical_plan, "plan_mapping_mismatch")
            check(proposed.evaluation == release.audited_evaluation, "evaluation_mapping_mismatch")
            check(proposed.resource_facts is release.resource_facts, "resource_reference_mismatch")
            check(
                proposed.core_release_fingerprint == release.release_fingerprint
                and release.core_audit_fingerprint == draft.core_audit.report_fingerprint,
                "core_release_binding_mismatch",
            )
            check(fingerprint(proposed.plan) == release.plan_fingerprint, "plan_mapping_mismatch")
            check(
                fingerprint(proposed.evaluation)
                == release.evaluation_fingerprint
                == draft.core_audit.audited_evaluation_fingerprint,
                "evaluation_mapping_mismatch",
            )
        facts = proposed.resource_facts
        check(
            facts.facts_fingerprint
            == fingerprint(_values(facts, ("facts_fingerprint",)))
            == draft.core_audit.derived_resource_fingerprint,
            "resource_fingerprint_mismatch",
        )
        allowed = all(
            item.disposition is RuleDisposition.ALLOWED_FINAL_DEVIATION
            and item.scope is RuleScope.CHAIN
            and item.reason_code == "chain_weight_below_minimum"
            for item in proposed.evaluation.violations
        )
        expected_status = (
            SolveStatus.PUBLISHABLE_WITH_ALLOWED_DEVIATION
            if proposed.evaluation.violations
            else SolveStatus.SUCCESS
        )
        check(
            draft.proposed_status is core_result.status is expected_status
            and draft.stop_reason is core_result.stop_reason is runtime.stop_reason
            and allowed
            and not any(issue.severity is DiagnosticSeverity.ERROR for issue in draft.issues),
            "result_status_mismatch",
        )
        if not _request_binding(request, problem, check, runtime):
            return interrupted()
        if not _fact_mappings(proposed.plan, facts, problem, check, runtime):
            return interrupted()
        partitions = facts.split_partitions
        same = sum(item.split_mode is ControlledSplitMode.SAME_PERIOD_SPLIT for item in partitions)
        counts = (len(partitions), same, len(partitions) - same)
        check(
            counts
            == (
                draft.metrics.accepted_split_count,
                draft.metrics.accepted_same_period_split_count,
                draft.metrics.accepted_future_borrow_return_count,
            )
            == (
                draft.core_audit.audited_split_count,
                draft.core_audit.audited_same_period_split_count,
                draft.core_audit.audited_future_borrow_return_count,
            ),
            "split_count_mismatch",
        )
        if not runtime.allows_finalization():
            return interrupted()
        report = _report(
            ResultAuditStatus.COMPLETED,
            codes,
            draft.draft_fingerprint,
            fingerprint(proposed.plan),
            facts.facts_fingerprint,
        )
        return report if runtime.allows_finalization() else interrupted()
    except Exception:
        logging.getLogger(__name__).exception("结果契约自检异常，禁止签发。")
        runtime.stop_reason = SearchStopReason.SYSTEM_ERROR
        return _report(ResultAuditStatus.ERROR, ("result_audit_error",), draft.draft_fingerprint)
