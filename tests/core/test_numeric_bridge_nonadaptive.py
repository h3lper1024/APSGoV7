"""The nonadaptive temperature path must handle no feasible single bridge."""

from dataclasses import replace
from decimal import Decimal

from apsgo_scheduler.core._numeric_evaluation import NumericQualityProgram
from tests.core.numeric_reference_resources import choose_virtual_bridge
from apsgo_scheduler.core._numeric_rules import NumericRuleProgram, NumericRuleKind
from apsgo_scheduler.core._numeric_resources import prepare_private_bridge
from apsgo_scheduler.core._numeric_state import NumericPlan, NumericCandidateWorkspace, OK
from tests.app.test_input_normalizer import make_order, make_prototype
from tests.core.test_numeric_evaluation import numeric_quality_spec
from tests.core.test_numeric_rules import attributes, build


def test_nonadaptive_temperature_can_choose_double_or_return_no_bridge():
    rules, task = build(spec=numeric_quality_spec(), orders=tuple(
        make_order(i, width=Decimal(w), rule_attributes=attributes(),
            min_temperature=Decimal(700 + 100 * i), max_temperature=Decimal(710 + 100 * i))
        for i, w in enumerate(("1000", "400"))),
        prototypes=tuple(make_prototype(i, width=Decimal(w))
                         for i, w in enumerate(("800", "600"))))
    program = NumericRuleProgram.compile(task, rules)
    program = replace(program, rules=tuple(replace(rule, flags=(False, False))
        if rule.kind is NumericRuleKind.TEMPERATURE else rule for rule in program.rules))
    quality = NumericQualityProgram.compile(task, program, rules)
    assert choose_virtual_bridge(task, program, quality, 0, 1,
                                 max_nodes=1, first_sequence=1) is None
    result = choose_virtual_bridge(task, program, quality, 0, 1,
                                   max_nodes=2, first_sequence=1)
    assert result is not None
    assert result.task.nodes.prototype[list(result.rows)].tolist() == [0, 1]
    assert result.task.nodes.min_temperature[list(result.rows)].tolist() == [700, 700]
    assert result.task.nodes.max_temperature[list(result.rows)].tolist() == [810, 810]
    plan = NumericPlan.build(task, (0, 1), (0, 1, 2), (0, 1), (0, 0))
    workspace = NumericCandidateWorkspace.allocate(task, plan, changed_capacity=4,
        chain_capacity=2, node_capacity=2, group_capacity=0, event_capacity=2)
    assert prepare_private_bridge(workspace, program, 0, 1,
        max_nodes=1, first_sequence=1)[:2] == (OK, False)
    workspace.reset()
    assert prepare_private_bridge(workspace, program, 0, 1,
        max_nodes=2, first_sequence=1)[:2] == (OK, True)
    assert workspace.nodes.prototype[:2].tolist() == [0, 1]
    assert workspace.nodes.min_temperature[:2].tolist() == [700, 700]
    assert workspace.nodes.max_temperature[:2].tolist() == [810, 810]
