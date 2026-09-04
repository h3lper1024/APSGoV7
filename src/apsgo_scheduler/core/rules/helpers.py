"""Small shared operations used by rule implementations and their rule set."""

from decimal import Decimal

from ..contracts import ControlledSplitMode, fingerprint
from .base import ControlledSplitDecision, ControlledSplitRuleSubject, RuleEvaluationContext


def create_controlled_split_decision(
    subject: ControlledSplitRuleSubject,
    context: RuleEvaluationContext,
    *,
    eligible: bool,
    rule_id: str,
    rule_version: str,
    reason_code: str,
    mode: ControlledSplitMode | None = None,
    target_assigned_period: str | None = None,
    maximum_piece_weight: Decimal | None = None,
    minimum_piece_weight: Decimal | None = None,
    maximum_accepted_source_count: int = 0,
    maximum_separator_node_count: int = 0,
    maximum_separator_weight: Decimal = Decimal(0),
) -> ControlledSplitDecision:
    """Bind a canonical authorization or rejection to this input and task order."""
    if not isinstance(subject, ControlledSplitRuleSubject):
        raise ValueError("split decision requires a ControlledSplitRuleSubject")
    if not isinstance(context, RuleEvaluationContext):
        raise ValueError("split decision requires a RuleEvaluationContext")
    values = dict(
        eligible=eligible,
        rule_id=rule_id,
        rule_version=rule_version,
        reason_code=reason_code,
        mode=mode,
        target_assigned_period=target_assigned_period,
        maximum_piece_weight=maximum_piece_weight,
        minimum_piece_weight=minimum_piece_weight,
        maximum_accepted_source_count=maximum_accepted_source_count,
        maximum_separator_node_count=maximum_separator_node_count,
        maximum_separator_weight=maximum_separator_weight,
    )
    return ControlledSplitDecision(
        **values,
        decision_fingerprint=fingerprint(dict(subject=subject, context=context, decision=values)),
    )
