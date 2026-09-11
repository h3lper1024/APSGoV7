"""Independent final checks and unsigned audited material for the core result owner."""

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, fields, replace
from math import isfinite

from .budget import SolveRuntimeBudget
from .chain_order import has_production_order_rule
from .contracts import (
    ControlledSplitMode,
    CoreAuditReport,
    CoreAuditStatus,
    CoreCandidateSnapshot,
    DiagnosticIssue,
    DiagnosticPhase,
    DiagnosticSeverity,
    RuleScope,
    SearchStopReason,
    fingerprint,
    freeze_tuple,
    sum_weights,
)
from .evaluation import PlanEvaluation, evaluate_plan
from .model import MaterialRole, SchedulingProblem, VirtualPurpose, validate_dimensions
from .neighborhoods import _split_partition_id, _split_piece_weights
from .process_logging import emit
from .resource_facts import (
    PlanDerivedFacts,
    SplitPartitionFact,
    _derive_audited_resource_facts,
)
from .rules.base import ControlledSplitRuleSubject, RuleDisposition, RuleEvaluationContext
from .rules.concrete import WEIGHT_EPSILON, ChainWeightRangeRule
from .rules.rule_set import ProcessRuleSet


@dataclass(frozen=True, slots=True)
class CoreAuditOutcome:
    report: CoreAuditReport
    audited_evaluation: PlanEvaluation | None
    resource_facts: PlanDerivedFacts | None
    issues: tuple[DiagnosticIssue, ...]

    def __post_init__(self):
        if not isinstance(self.report, CoreAuditReport):
            raise ValueError("audit outcome requires CoreAuditReport")
        for name, expected in (
            ("audited_evaluation", PlanEvaluation),
            ("resource_facts", PlanDerivedFacts),
        ):
            value = getattr(self, name)
            if value is not None and not isinstance(value, expected):
                raise ValueError(f"{name} has an invalid audit value type")
        object.__setattr__(self, "issues", freeze_tuple(self.issues, DiagnosticIssue, "issues"))
        if self.report.status is not CoreAuditStatus.COMPLETED and (
            self.audited_evaluation is not None or self.resource_facts is not None
        ):
            raise ValueError("unfinished audit must not expose partial audited material")
        if self.report.passed and (self.audited_evaluation is None or self.resource_facts is None):
            raise ValueError("passed audit requires complete audited material")
        if self.resource_facts is not None and (
            self.resource_facts.facts_fingerprint != self.report.derived_resource_fingerprint
        ):
            raise ValueError("audit outcome must preserve the audited resource identity")


def _record(issues, codes, code, message, subject_id=None, field_path=None):
    issues.append(
        DiagnosticIssue(
            code,
            DiagnosticPhase.CORE_AUDIT,
            field_path,
            subject_id,
            message,
            DiagnosticSeverity.ERROR,
        )
    )
    if codes is not None and code not in codes:
        codes.append(code)


def _partition_fact(
    pieces,
    separators,
    original,
    assigned,
    rule_set,
    context,
    budget,
    ordinal,
    issues,
    invariants,
    authorizations,
):
    pieces = tuple(sorted(pieces, key=lambda node: node.split_lineage.piece_index))
    separators = tuple(sorted(separators, key=lambda node: node.virtual_lineage.accepted_sequence))
    lineage = pieces[0].split_lineage
    partition_id = lineage.partition_id
    start = len(issues)
    parent = original.get(lineage.parent_node_id)
    if parent is None or parent.material_role is not MaterialRole.NORMAL_REAL:
        _record(
            issues,
            authorizations,
            "split_authorization_parent_invalid",
            "拆单父节点不属于权威普通真实订单。",
            partition_id,
        )
        return None
    if lineage.origin_assigned_period not in context.period_index:
        _record(
            issues,
            authorizations,
            "split_authorization_period_unknown",
            "拆前排产期不属于本任务权威期序。",
            partition_id,
        )
        return None
    subject = ControlledSplitRuleSubject(
        parent.node_id, parent, lineage.origin_assigned_period, parent.source_period, ordinal - 1
    )
    if not budget.allows_finalization():
        return None
    decision = rule_set.evaluate_controlled_split(subject, context)
    if not budget.allows_finalization():
        return None
    if not decision.eligible:
        _record(
            issues,
            authorizations,
            "split_authorization_rejected",
            f"权威规则拒绝拆单资格：{decision.reason_code}。",
            partition_id,
        )
        return None
    expected = _split_piece_weights(parent.weight, decision)
    if not expected or ordinal > decision.maximum_accepted_source_count:
        _record(
            issues,
            authorizations,
            "split_authorization_limits_exceeded",
            "片重、隔离数量或累计拆单来源数超过重新授权的上限。",
            partition_id,
        )
        return None
    expected_metadata = {
        "parent_node_id": parent.node_id,
        "parent_source_order_id": parent.source_order_id,
        "source_resource_id": parent.source_resource_id,
        "source_period": parent.source_period,
        "split_mode": decision.mode,
        "target_assigned_period": decision.target_assigned_period,
        "accepted_source_sequence": ordinal,
        "parent_weight": parent.weight,
        "piece_count": len(expected),
        "authorization_rule_id": decision.rule_id,
        "authorization_rule_version": decision.rule_version,
        "authorization_decision_fingerprint": decision.decision_fingerprint,
        "reason_code": decision.reason_code,
    }
    if any(getattr(lineage, name) != value for name, value in expected_metadata.items()) or (
        partition_id != _split_partition_id(subject, decision, expected)
    ):
        _record(
            issues,
            authorizations,
            "split_authorization_identity_mismatch",
            "拆单谱系、模式或决策指纹与权威重新授权不一致。",
            partition_id,
        )
    if (
        tuple(node.split_lineage.piece_index for node in pieces)
        != tuple(range(1, len(expected) + 1))
        or tuple(node.weight for node in pieces) != expected
    ):
        _record(
            issues,
            invariants,
            "split_partition_incomplete",
            "拆单片段序号、数量或前片上限与精确尾片重量不一致。",
            partition_id,
        )
    for piece in pieces:
        if not budget.allows_finalization():
            return None
        if replace(piece.split_lineage, piece_index=lineage.piece_index) != lineage or (
            replace(piece, node_id=parent.node_id, weight=parent.weight, split_lineage=None)
            != parent
        ):
            _record(
                issues,
                invariants,
                "split_piece_attributes_mismatch",
                "拆片原始属性或同分区谱系不一致。",
                piece.node_id,
            )
        if assigned[piece.node_id] != decision.target_assigned_period:
            _record(
                issues,
                invariants,
                "split_target_period_mismatch",
                "拆片离开授权目标计划期。",
                piece.node_id,
            )
    if sum_weights(node.weight for node in pieces) != parent.weight:
        _record(
            issues,
            invariants,
            "split_partition_weight_mismatch",
            "完整拆单分区重量不等于权威父订单重量。",
            partition_id,
        )
    if (
        len(separators) != len(expected) - 1
        or len(separators) > (decision.maximum_separator_node_count)
        or sum_weights(node.weight for node in separators)
        > sum_weights((decision.maximum_separator_weight, WEIGHT_EPSILON))
    ):
        _record(
            issues,
            authorizations,
            "split_authorization_separator_limit",
            "关联隔离材料数量或重量不符合完整分区授权。",
            partition_id,
        )
    for separator in separators:
        if not budget.allows_finalization():
            return None
        # Both original separator anchors are pieces with their authoritative parent's interval.
        if (separator.min_temperature, separator.max_temperature) != (
            parent.min_temperature,
            parent.max_temperature,
        ):
            _record(
                issues,
                invariants,
                "split_separator_temperature_mismatch",
                "拆单隔离材料的生成温区与权威父订单不一致。",
                separator.node_id,
            )
    if len(issues) != start:
        return None
    values = dict(
        partition_id=partition_id,
        parent_node_id=parent.node_id,
        parent_source_order_id=parent.source_order_id,
        source_resource_id=parent.source_resource_id,
        source_period=parent.source_period,
        origin_assigned_period=lineage.origin_assigned_period,
        split_mode=decision.mode,
        target_assigned_period=decision.target_assigned_period,
        accepted_source_sequence=ordinal,
        authorization_rule_id=decision.rule_id,
        authorization_rule_version=decision.rule_version,
        authorization_decision_fingerprint=decision.decision_fingerprint,
        reason_code=decision.reason_code,
        piece_node_ids=tuple(node.node_id for node in pieces),
        piece_weights=tuple(node.weight for node in pieces),
        separator_virtual_node_ids=tuple(node.node_id for node in separators),
        parent_weight=parent.weight,
    )
    payload = tuple(
        (item.name, values[item.name])
        for item in fields(SplitPartitionFact)
        if item.name != "partition_fingerprint"
    )
    return SplitPartitionFact(**values, partition_fingerprint=fingerprint(payload))


def _audit_structure(plan, problem, rule_set, context, budget, issues, invariants, authorizations):
    original = {node.node_id: node for node in problem.nodes}
    prototypes = {item.prototype_id: item for item in problem.virtual_prototypes}
    present, assigned, groups, separators = {}, {}, defaultdict(list), defaultdict(list)
    real, virtual, virtual_sequences = [], [], set()
    ordered_chains = has_production_order_rule(rule_set)
    previous_period = -1
    if not plan.chains:
        _record(issues, invariants, "empty_plan", "最终方案没有排产链。")
    for chain_id, count in Counter(chain.chain_id for chain in plan.chains).items():
        if count > 1:
            _record(issues, invariants, "duplicate_chain_identity", "最终链身份重复。", chain_id)
    identities = Counter(node.node_id for chain in plan.chains for node in chain.nodes)
    for node_id, count in identities.items():
        if count > 1:
            _record(issues, invariants, "duplicate_node_identity", "最终节点身份重复。", node_id)
    for chain in plan.chains:
        if not budget.allows_finalization():
            return (), None
        if not chain.nodes:
            _record(issues, invariants, "empty_chain", "最终方案包含空链。", chain.chain_id)
        if chain.assigned_period not in context.period_index:
            _record(
                issues,
                invariants,
                "assigned_period_unknown",
                "排产链使用未知计划期。",
                chain.chain_id,
            )
        elif ordered_chains:
            period = context.period_index[chain.assigned_period]
            if period < previous_period:
                _record(
                    issues,
                    invariants,
                    "chain_period_order_invalid",
                    "生产链序未按本任务计划期顺序排列。",
                    chain.chain_id,
                    "chains.assigned_period",
                )
            previous_period = period
        sources = [
            node.source_period
            for node in chain.nodes
            if node.material_role is not MaterialRole.GENERATED_VIRTUAL
        ]
        if not sources:
            if chain.nodes:
                _record(
                    issues,
                    invariants,
                    "virtual_only_chain",
                    "最终排产链必须含有真实来源材料。",
                    chain.chain_id,
                )
        elif all(period in context.period_index for period in sources) and (
            chain.assigned_period != min(sources, key=context.period_index.__getitem__)
        ):
            _record(
                issues,
                invariants,
                "chain_period_not_normalized",
                "排产链计划期不等于真实来源中的最早期。",
                chain.chain_id,
            )
        for node in chain.nodes:
            if not budget.allows_finalization():
                return (), None
            assigned[node.node_id] = chain.assigned_period
            present[node.node_id] = node
            try:
                validate_dimensions(
                    node.width, node.thickness, node.min_temperature, node.max_temperature
                )
            except ValueError as error:
                _record(
                    issues,
                    invariants,
                    "physical_value_invalid",
                    f"物理字段不符合核心模型约束：{error}。",
                    node.node_id,
                )
            else:
                for name in ("width", "thickness", "min_temperature", "max_temperature"):
                    value = getattr(node, name)
                    if value is not None and not isfinite(float(value)):
                        _record(
                            issues,
                            invariants,
                            "physical_value_nonfinite",
                            "物理字段不能投影为有限计算数值。",
                            node.node_id,
                            name,
                        )
            if node.material_role is MaterialRole.GENERATED_VIRTUAL:
                virtual.append(node)
                lineage = node.virtual_lineage
                prototype = prototypes.get(lineage.prototype_id)
                if (
                    prototype is None
                    or node.weight != prototype.unit_weight
                    or any(
                        getattr(node, name) != getattr(prototype, name)
                        for name in ("width", "thickness", "grade", "rule_attributes")
                    )
                ):
                    _record(
                        issues,
                        invariants,
                        "virtual_prototype_mismatch",
                        "虚拟材料的原型身份或固定物理属性不匹配。",
                        node.node_id,
                    )
                if node.node_id in original or lineage.accepted_sequence in virtual_sequences:
                    _record(
                        issues,
                        invariants,
                        "virtual_generation_identity_repeated",
                        "虚拟材料身份覆盖原始节点或生成序号重复。",
                        node.node_id,
                    )
                virtual_sequences.add(lineage.accepted_sequence)
                if lineage.purpose is VirtualPurpose.SPLIT_SEPARATOR:
                    separators[lineage.related_partition_id].append(node)
                continue
            real.append(node)
            if node.source_period not in context.period_index:
                _record(
                    issues,
                    invariants,
                    "source_period_unknown",
                    "真实节点使用未知来源计划期。",
                    node.node_id,
                )
            if node.split_lineage is not None:
                groups[node.split_lineage.partition_id].append(node)
                if node.node_id in original:
                    _record(
                        issues,
                        invariants,
                        "split_piece_identity_collision",
                        "拆片身份覆盖原始订单身份。",
                        node.node_id,
                    )
            elif node.node_id not in original:
                _record(
                    issues,
                    invariants,
                    "unexpected_real_node",
                    "出现既非权威原订单也非授权拆片的真实节点。",
                    node.node_id,
                )
            elif node != original[node.node_id]:
                _record(
                    issues,
                    invariants,
                    "original_node_changed",
                    "原订单的身份、材料角色、属性或重量被修改。",
                    node.node_id,
                )
    parents = Counter(nodes[0].split_lineage.parent_node_id for nodes in groups.values())
    for parent in problem.nodes:
        if not budget.allows_finalization():
            return (), None
        if parent.node_id not in present and parent.node_id not in parents:
            _record(
                issues, invariants, "input_node_missing", "权威输入订单未完整排产。", parent.node_id
            )
        if parents[parent.node_id] > 1 or (parents[parent.node_id] and parent.node_id in present):
            _record(
                issues,
                invariants,
                "input_parent_repeated",
                "拆单父订单仍在方案中，或同一父订单出现多个分区。",
                parent.node_id,
            )
    for name in ("source_order_id", "source_resource_id"):
        weights = defaultdict(list)
        for node in real:
            if not budget.allows_finalization():
                return (), None
            weights[getattr(node, name)].append(node.weight)
        expected = {getattr(node, name): node.weight for node in problem.nodes}
        for identity in dict.fromkeys((*expected, *weights)):
            if not budget.allows_finalization():
                return (), None
            if sum_weights(weights[identity]) != expected.get(identity, 0):
                _record(
                    issues,
                    invariants,
                    "source_weight_mismatch",
                    f"来源的精确重量不守恒：{name}。",
                    identity,
                    name,
                )
    if sum_weights(node.weight for node in real) != sum_weights(
        node.weight for node in problem.nodes
    ):
        _record(issues, invariants, "input_real_weight_mismatch", "输入与排产真实总重量不一致。")
    if sum_weights(chain.total_weight for chain in plan.chains) != sum_weights(
        (sum_weights(node.weight for node in real), sum_weights(node.weight for node in virtual))
    ):
        _record(issues, invariants, "plan_total_weight_mismatch", "最终总重不等于真实加虚拟重量。")
    for partition_id in separators:
        if partition_id not in groups:
            _record(
                issues,
                invariants,
                "orphan_split_separator",
                "隔离虚拟材料关联的拆单分区不存在。",
                partition_id,
            )
    ordered = sorted(
        groups.values(), key=lambda nodes: nodes[0].split_lineage.accepted_source_sequence
    )
    facts = []
    for ordinal, pieces in enumerate(ordered, start=1):
        if not budget.allows_finalization():
            return (), None
        fact = _partition_fact(
            pieces,
            separators[pieces[0].split_lineage.partition_id],
            original,
            assigned,
            rule_set,
            context,
            budget,
            ordinal,
            issues,
            invariants,
            authorizations,
        )
        if fact is not None:
            facts.append(fact)
    same = sum(
        nodes[0].split_lineage.split_mode is ControlledSplitMode.SAME_PERIOD_SPLIT
        for nodes in ordered
    )
    return tuple(facts), (len(ordered), same, len(ordered) - same)


def _compare_evaluations(search, audited, issues, *, ordered_chains):
    start = len(issues)
    if ordered_chains and tuple(chain.chain_id for chain in search.chain_evaluations) != tuple(
        chain.chain_id for chain in audited.chain_evaluations
    ):
        _record(
            issues,
            None,
            "search_evaluation_chain_order_mismatch",
            "搜索与独立审计评价的生产链顺序不一致。",
            field_path="chain_evaluations",
        )

    def compare_metrics(before, after, path, subject=None):
        missing = object()
        for key in sorted(before.keys() | after.keys()):
            if before.get(key, missing) != after.get(key, missing):
                _record(
                    issues,
                    None,
                    "search_evaluation_metric_mismatch",
                    f"搜索与独立审计的原始指标不一致：{key}。",
                    subject,
                    f"{path}.{key}",
                )

    def compare_violations(before, after, path, subject=None):
        old, new = Counter(before), Counter(after)
        for violation in dict.fromkeys((*before, *after)):
            if old[violation] != new[violation]:
                _record(
                    issues,
                    None,
                    "search_evaluation_violation_mismatch",
                    f"违规明细或重复数量不一致：{violation.rule_id}/{violation.reason_code}，"
                    f"搜索 {old[violation]}，审计 {new[violation]}。",
                    violation.subject_id or subject,
                    path,
                )

    before = {chain.chain_id: chain for chain in search.chain_evaluations}
    after = {chain.chain_id: chain for chain in audited.chain_evaluations}
    for chain_id in dict.fromkeys((*after, *before)):
        if chain_id not in before or chain_id not in after:
            _record(
                issues,
                None,
                "search_evaluation_chain_mismatch",
                "搜索与独立审计评价覆盖的链身份不一致。",
                chain_id,
                "chain_evaluations",
            )
            continue
        left, right = before[chain_id], after[chain_id]
        if left.summary != right.summary:
            _record(
                issues,
                None,
                "search_evaluation_summary_mismatch",
                "搜索与独立审计的链摘要不一致。",
                chain_id,
                "chain_evaluations.summary",
            )
        compare_metrics(left.metrics, right.metrics, "chain_evaluations.metrics", chain_id)
        compare_violations(
            left.violations, right.violations, "chain_evaluations.violations", chain_id
        )
    compare_metrics(search.metrics, audited.metrics, "metrics")
    compare_violations(search.violations, audited.violations, "violations")
    if search.quality_key != audited.quality_key:
        _record(
            issues,
            None,
            "search_evaluation_quality_mismatch",
            "搜索与独立审计的完整质量键不一致。",
            field_path="quality_key",
        )
    return len(issues) == start


def _publication_issues(evaluation, rule_set, issues):
    chain_weight_ids = {
        rule.rule_id for rule in rule_set.rules if type(rule) is ChainWeightRangeRule
    }
    for violation in evaluation.violations:
        if violation.disposition is RuleDisposition.PROHIBITED:
            _record(
                issues,
                None,
                "prohibited_rule_violation",
                f"禁止违规：{violation.rule_id}/{violation.reason_code}；{violation.message}",
                violation.subject_id,
            )
        elif not (
            violation.rule_id in chain_weight_ids
            and violation.scope is RuleScope.CHAIN
            and violation.reason_code == "chain_weight_below_minimum"
            and violation.reason_code in rule_set.allowed_final_deviation_codes
        ):
            _record(
                issues,
                None,
                "unapproved_final_deviation",
                f"该允许偏差不能发布：{violation.rule_id}/{violation.reason_code}。",
                violation.subject_id,
            )


def _outcome(
    candidate,
    problem,
    rule_set,
    status,
    issues,
    invariants,
    authorizations,
    evaluation=None,
    facts=None,
    counts=None,
    matches=None,
):
    if status is not CoreAuditStatus.COMPLETED:
        evaluation, facts, counts, matches = None, None, None, None
    total, same, future = counts if counts is not None else (None, None, None)
    values = dict(
        status=status,
        passed=status is CoreAuditStatus.COMPLETED and not issues,
        audited_evaluation_fingerprint=None if evaluation is None else fingerprint(evaluation),
        invariant_failure_codes=tuple(invariants),
        action_authorization_failure_codes=tuple(authorizations),
        derived_resource_fingerprint=None if facts is None else facts.facts_fingerprint,
        audited_split_count=total,
        audited_same_period_split_count=same,
        audited_future_borrow_return_count=future,
        search_evaluation_matches=matches,
    )
    payload = dict(
        problem_fingerprint=problem.input_fingerprint,
        rule_set_fingerprint=rule_set.fingerprint,
        plan_fingerprint=fingerprint(candidate.plan),
        search_evaluation_fingerprint=fingerprint(candidate.search_evaluation),
        report=values,
        issues=tuple(issues),
    )
    report = CoreAuditReport(**values, report_fingerprint=fingerprint(payload))
    return CoreAuditOutcome(report, evaluation, facts, tuple(issues))


def audit_core_without_search_cache(
    candidate: CoreCandidateSnapshot,
    problem: SchedulingProblem,
    rule_set: ProcessRuleSet,
    budget: SolveRuntimeBudget,
) -> CoreAuditOutcome:
    """Check immutable search output independently; never sign or alter a schedule."""
    if (
        not isinstance(candidate, CoreCandidateSnapshot)
        or not isinstance(problem, SchedulingProblem)
        or not isinstance(rule_set, ProcessRuleSet)
        or not isinstance(budget, SolveRuntimeBudget)
    ):
        raise ValueError("core audit requires candidate, problem, rule set and runtime budget")
    if any(
        getattr(problem, name) != getattr(rule_set, name)
        for name in ("product_line_code", "process_code", "scenario")
    ):
        raise ValueError("core audit problem and rule-set identities must match")
    issues, invariants, authorizations = [], [], []

    def interrupted():
        status = (
            CoreAuditStatus.CANCELLED
            if budget.stop_reason is SearchStopReason.USER_CANCELLED
            else CoreAuditStatus.TIME_LIMIT
            if budget.stop_reason is SearchStopReason.FINALIZATION_TIME_LIMIT_REACHED
            else CoreAuditStatus.ERROR
        )
        code = (
            "core_audit_cancelled"
            if status is CoreAuditStatus.CANCELLED
            else (
                "core_audit_time_limit"
                if status is CoreAuditStatus.TIME_LIMIT
                else "core_audit_stopped"
            )
        )
        _record(issues, None, code, "核心审计未完成，未返回部分审计事实或释放资格。")
        return _outcome(candidate, problem, rule_set, status, issues, invariants, authorizations)

    try:
        if not budget.allows_finalization():
            return interrupted()
        context = RuleEvaluationContext(
            problem.period_order,
            {period: index for index, period in enumerate(problem.period_order)},
            tuple(prototype.prototype_id for prototype in problem.virtual_prototypes),
            problem.delivery_timing,
        )
        partitions, counts = _audit_structure(
            candidate.plan, problem, rule_set, context, budget, issues, invariants, authorizations
        )
        if not budget.allows_finalization():
            return interrupted()
        audited = evaluate_plan(candidate.plan, rule_set, context)
        if not budget.allows_finalization():
            return interrupted()
        matches = _compare_evaluations(
            candidate.search_evaluation,
            audited,
            issues,
            ordered_chains=has_production_order_rule(rule_set),
        )
        _publication_issues(audited, rule_set, issues)
        facts = None
        if not invariants and not authorizations:
            facts = _derive_audited_resource_facts(
                candidate.plan, problem, context, partitions, budget
            )
            if facts is None:
                return interrupted()
        if not budget.allows_finalization():
            return interrupted()
        outcome = _outcome(
            candidate,
            problem,
            rule_set,
            CoreAuditStatus.COMPLETED,
            issues,
            invariants,
            authorizations,
            audited,
            facts,
            counts,
            matches,
        )
        return outcome if budget.allows_finalization() else interrupted()
    except Exception as error:
        budget.stop_reason = SearchStopReason.SYSTEM_ERROR
        emit(
            logging.getLogger(__name__),
            "solver_stage_exception",
            stage="core_audit",
            status="error",
            exception_type=type(error).__name__,
            level=logging.ERROR,
            exc_info=True,
        )
        _record(
            issues,
            None,
            "core_audit_error",
            f"核心审计异常，禁止发布：{type(error).__name__}: {error}",
        )
        return _outcome(
            candidate, problem, rule_set, CoreAuditStatus.ERROR, issues, invariants, authorizations
        )
