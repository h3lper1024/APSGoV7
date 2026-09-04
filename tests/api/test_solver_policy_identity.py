"""There is one policy class across public and core boundaries."""

from decimal import Decimal

from apsgo_scheduler.api import request
from apsgo_scheduler.core import contracts


def test_api_policy_is_the_exact_core_class():
    assert request.SolverPolicy is contracts.SolverPolicy
    assert request.RuleScope is contracts.RuleScope
    assert request.SchedulingRequest.__annotations__["policy"] is contracts.SolverPolicy


def test_zero_candidate_budget_is_preserved_through_public_policy():
    policy = request.SolverPolicy(
        -7,
        Decimal("10"),
        Decimal("1"),
        0,
        contracts.CONSTRUCTION_ORDER_KEY,
        contracts.NUMERIC_SEMANTICS_KEY,
        Decimal("0"),
    )
    assert type(policy) is contracts.SolverPolicy
    assert policy.candidate_check_limit == 0
