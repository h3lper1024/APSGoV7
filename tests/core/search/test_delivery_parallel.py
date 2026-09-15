"""Numerical envelopes enclose the independent Decimal result; batches never choose a winner."""

from dataclasses import replace
from decimal import Decimal as D, localcontext
from itertools import permutations
from types import SimpleNamespace

import pytest

from apsgo_scheduler.core._delivery_parallel import ChainOrderBatch, _Unsupported, _seconds
from apsgo_scheduler.core.delivery_timing import _evaluate_delivery_reused, evaluate_delivery
from apsgo_scheduler.core.evaluation import _evaluate_candidate_plan, _screen_candidate_plan
from apsgo_scheduler.core.model import Chain, SchedulePlan
from tests.core.test_delivery_objective import example
from tests.core.test_backlog_priority_objective import tradeoff_case


@pytest.mark.parametrize("hours", ("4", "0.000125", "0.00375", "1.2345678901234567890123456789"))
def test_bounded_threads_enclose_decimal_last_fragment_and_half_second(hours):
    a, b, virtual, timing = example(hours_a=hours)
    nodes = (replace(a, node_id="left", weight=D(3)), b, virtual,
             replace(a, node_id="right", weight=D(7)))
    chains = (Chain("left", (nodes[0], virtual), "period"), Chain("b", (b,), "period"),
              Chain("right", (nodes[-1],), "period"))
    plan = SchedulePlan(chains)
    previous = SimpleNamespace(plan=plan, delivery=_evaluate_delivery_reused(plan, timing, True))
    batch = ChainOrderBatch(previous)
    candidates = tuple(permutations(chains))
    baseline = None
    for threads in (1, 2, 4, 8):
        with localcontext() as arithmetic:
            arithmetic.prec = 4
            results = batch.compute(candidates, threads, {})
        values = tuple(result.metrics for result in results)
        assert baseline is None or values == baseline
        baseline = values
        for candidate, result in zip(candidates, results):
            exact = evaluate_delivery(SchedulePlan(candidate), timing, second_precision=True)
            assert result.metrics[0][0] <= exact.old_backlog_last_completion_hours <= result.metrics[0][1]
            assert result.metrics[1][0] <= exact.delivery_wait_tardiness_tonne_hours <= result.metrics[1][1]
            assert result.matches(SchedulePlan(candidate), SimpleNamespace(current_plan=plan), timing)
    assert not batch.durations.flags.writeable
    assert not batch.weights.flags.writeable
    assert _seconds(125000) == 0  # 0.45 s
    assert _seconds(3750000) == 14  # 13.5 s, half-even.


def test_unsupported_numeric_range_and_bad_candidates_are_not_rejections():
    a, b, virtual, timing = example(hours_a="1e35")
    plan = SchedulePlan((Chain("a", (a, virtual, b), "period"),))
    with pytest.raises(_Unsupported):
        ChainOrderBatch(SimpleNamespace(plan=plan, delivery=_evaluate_delivery_reused(plan, timing, True)))
    _, state, search = tradeoff_case(second_precision=True)
    cache = search.factory.cache
    assert ChainOrderBatch.prepare(None, state, cache) is None
    _, previous = _evaluate_candidate_plan(state.current_plan, state, cache.rule_set, cache.context, None)
    batch = ChainOrderBatch.prepare(previous, state, cache)
    with pytest.raises(ValueError, match="actual whole chains"):
        batch.compute(((replace(state.current_plan.chains[0]), *state.current_plan.chains[1:]),), 1, {})
    for threads in (True, 0, 3):
        with pytest.raises(ValueError):
            batch.compute((state.current_plan.chains,), threads, {})


def test_numeric_rejection_requires_exact_earlier_quality_and_current_identity():
    _, state, search = tradeoff_case(second_precision=True)
    cache = search.factory.cache
    _, previous = _evaluate_candidate_plan(state.current_plan, state, cache.rule_set, cache.context, None)
    batch = ChainOrderBatch.prepare(previous, state, cache)
    candidates = tuple(permutations(state.current_plan.chains))
    for chains, hint in zip(candidates, batch.compute(candidates, 2, {})):
        plan = SchedulePlan(chains)
        actual = _screen_candidate_plan(plan, state, cache.rule_set, cache.context, previous, hint)
        expected = _screen_candidate_plan(plan, state, cache.rule_set, cache.context, previous)
        assert actual.outcome == expected.outcome
        assert not replace(hint, source=SchedulePlan(state.current_plan.chains)).matches(plan, state, cache.context.delivery_timing)
        assert not hint.matches(plan, state, replace(cache.context.delivery_timing))
    hint = batch.compute((state.current_plan.chains,), 1, {})[0]
    with pytest.raises(ValueError, match="do not match"):
        _screen_candidate_plan(SchedulePlan(tuple(reversed(state.current_plan.chains))), state,
                               cache.rule_set, cache.context, previous, hint)


def test_thread_error_propagates_and_busy_launch_does_not_mix_requests(monkeypatch):
    from apsgo_scheduler.core import _delivery_parallel as module
    from numba import get_num_threads
    _, state, search = tradeoff_case(second_precision=True)
    cache = search.factory.cache
    _, previous = _evaluate_candidate_plan(state.current_plan, state, cache.rule_set, cache.context, None)
    batch = ChainOrderBatch.prepare(previous, state, cache)
    with module._launch_lock:
        stats = {}
        assert batch.compute((state.current_plan.chains,), 2, stats) == ()
        assert stats["busy_fallbacks"] == 1
    old = get_num_threads()
    class Broken:
        signatures = ()
        def __call__(self, *args):
            raise RuntimeError("kernel failed")
    monkeypatch.setattr(module, "_parallel_bounds", Broken())
    with pytest.raises(RuntimeError, match="kernel failed"):
        batch.compute((state.current_plan.chains,), 2, {})
    assert get_num_threads() == old
    assert not module._launch_lock.locked()


@pytest.mark.parametrize("limit", (1, 5, 100))
def test_batch_first_improvement_ties_and_final_quota_match_original(limit):
    from apsgo_scheduler.core.neighborhoods import improve_chain_order
    from apsgo_scheduler.core.evaluation import evaluate_plan
    results = []
    for threads in (0, 1, 4):
        _, state, search = tradeoff_case(second_precision=True)
        cache = search.factory.cache
        nodes = tuple(node for chain in state.current_plan.chains for node in chain.nodes)
        state.current_plan = SchedulePlan(tuple(Chain(node.node_id, (node,), "P0") for node in nodes))
        state.current_evaluation = evaluate_plan(state.current_plan, cache.rule_set, cache.context)
        _, search._evaluation_reuse = _evaluate_candidate_plan(state.current_plan, state, cache.rule_set, cache.context, None)
        search._candidate_threads = threads
        search.factory.budget.candidate_check_limit = limit
        improve_chain_order(state, search)
        results.append((state.current_plan, state.current_evaluation, search.accepted_move_traces,
                        search.factory.budget.candidate_check_count, state.virtual_sequence))
        stats = search.parallel_candidate_counts
        if stats.get("precomputed"):
            assert stats["precomputed"] == stats.get("consumed", 0) + stats.get("discarded", 0)
    assert results[0] == results[1] == results[2]


def test_cancellation_after_kernel_never_consumes_or_accepts_prefetched_work(monkeypatch):
    from apsgo_scheduler.core.neighborhoods import improve_chain_order
    from apsgo_scheduler.core.contracts import SearchStopReason, fingerprint
    _, state, search = tradeoff_case(second_precision=True)
    cache = search.factory.cache
    _, search._evaluation_reuse = _evaluate_candidate_plan(state.current_plan, state, cache.rule_set, cache.context, None)
    search._candidate_threads = 2
    flag = SimpleNamespace(cancelled=False)
    search.factory.budget.cancellation = SimpleNamespace(is_cancelled=lambda: flag.cancelled)
    original = ChainOrderBatch.compute
    def cancel_after(self, *args):
        results = original(self, *args)
        flag.cancelled = True
        return results
    monkeypatch.setattr(ChainOrderBatch, "compute", cancel_after)
    before = fingerprint(state)
    improve_chain_order(state, search)
    assert fingerprint(state) == before
    assert search.factory.budget.candidate_check_count == 0
    assert search.factory.budget.stop_reason is SearchStopReason.USER_CANCELLED
    assert search.parallel_candidate_counts["discarded"] == search.parallel_candidate_counts["precomputed"]
