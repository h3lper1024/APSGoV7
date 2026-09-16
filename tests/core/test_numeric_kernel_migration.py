"""Frozen same-standard reference; not a second production evaluator."""
from itertools import permutations

from apsgo_scheduler.core._numeric_evaluation import evaluate_numeric_plan
from apsgo_scheduler.core._numeric_state import NumericPlan
from tests.core import numeric_migration_reference_evaluation as reference
from tests.core.test_numeric_construction import construction_case
from tests.core.test_numeric_incremental_evaluation import _assert_same


def test_frozen_reference_complete_candidate_corpus():
    task, rules, quality = construction_case(
        weights=("100", "210", "350"), widths=("1000", "1010", "900"),
        due_dates=("2026-05-31", "2026-06-01", "2026-06-01"),
        duration_hours=("1", "24", "2"),
    )
    for rows in permutations(range(3)):
        for offsets in ((0, 3), (0, 1, 3), (0, 2, 3), (0, 1, 2, 3)):
            count = len(offsets) - 1
            plan = NumericPlan.build(task, rows, offsets, tuple(range(count)), (0,) * count)
            _assert_same(evaluate_numeric_plan(task, rules, quality, plan),
                         reference.evaluate_numeric_plan(task, rules, quality, plan))
