"""Original computation in isolated generations; never a formal acceptance."""

from dataclasses import fields, replace
from time import monotonic
import multiprocessing as mp

import pytest

from apsgo_scheduler.api.request import OrderInput, PeriodInput, QualityCriterionSpec, RuleDefinitionSpec
from apsgo_scheduler.app import _candidate_worker as workers
from apsgo_scheduler.app._candidate_payload import _pack, encode_request, encode_state
from apsgo_scheduler.app.input_normalizer import normalize_input
from apsgo_scheduler.core.contracts import fingerprint
from apsgo_scheduler.core import evaluation
from apsgo_scheduler.core.neighborhoods import _commit_prepared_candidate
from apsgo_scheduler.core.width_optimization import _compute_width_recipe
from tests.app.test_input_normalizer import make_request, make_spec
from tests.core.search.test_width_optimization_baseline import width_case


MOVE = ("width_node_move", 0, 1, 2, 3, 2, 2)
CUT = ("width_chain_cut", 0, 2, 0, 2)
ORDER = ("width_chain_order_relocation", 0, 1)


def worker_case():
    state, context = width_case()
    active = context.factory.cache.rule_set
    spec = make_spec(
        rules=tuple(RuleDefinitionSpec(r.rule_id, type(r).__name__, r.name, r.scope, r.enabled, r.version, r.parameters) for r in active.rules),
        quality_spec=tuple(QualityCriterionSpec(q.criterion_id, q.metric_key, q.direction.value, q.aggregation.value, q.numeric_projection.value) for q in active.quality_spec),
        allowed_final_deviation_codes=active.allowed_final_deviation_codes,
    )
    orders = tuple(OrderInput(**{f.name: getattr(n, f.name) for f in fields(OrderInput)}) for c in state.current_plan.chains for n in c.nodes)
    request = make_request(rule_set_spec=spec, orders=orders, periods=(PeriodInput("period", 0),), virtual_prototypes=(), policy=context.policy)
    message = encode_request(request)
    identity = message["request_fingerprint"]
    now = monotonic()
    args = (message, identity, normalize_input(request).input_fingerprint, (now, now + 120, now + 125))
    return state, context, args


def compute(worker, recipe=MOVE, **changes):
    return worker.compute(recipe, **({"request_fingerprint": worker.request_fingerprint, "generation": worker.generation, "window_id": "window", "ordinal": 0} | changes))


def business(result):
    return {k: v for k, v in result.items() if k not in ("wall_seconds", "cpu_seconds")}


def test_original_computation_and_generation_cache_do_not_accept_candidates(monkeypatch):
    state, context, args = worker_case()
    worker = workers.CandidateComputer(*args)
    wire = encode_state(state, args[1], 0)
    worker.set_generation(wire, 0)
    holder = worker.context._evaluation_reuse
    assert holder.matches(worker.state, worker.context.factory.cache.rule_set, worker.context.factory.cache.context)
    before = fingerprint(worker.state)
    expected = _compute_width_recipe(state, context, CUT)
    assert expected is not None
    result = compute(worker, CUT)
    assert result["status"] == "improvement"
    assert result["proposal"] == _pack((expected.plan, expected.evaluation, expected.affected_chain_ids, expected.virtual_sequence, expected.action_name))
    assert fingerprint(worker.state) == before == fingerprint(state)
    assert worker.runtime.candidate_check_count == 0
    assert worker.context.accepted_move_traces == ()
    assert worker.context._evaluation_reuse is holder
    worker.set_generation(wire, 0)
    assert worker.initialization_count == 1 and worker.context._evaluation_reuse is holder

    # The untouched B chain is returned by the original evaluator from its cache.
    prepared = _compute_width_recipe(worker.state, worker.context, CUT)
    assert prepared.evaluation_reuse.entries["B"] is holder.entries["B"]
    assert prepared.evaluation_reuse.entries["A"] is not holder.entries["A"]
    original = evaluation._evaluate_plan
    entries = []

    def observed(plan, rules, rule_context, previous):
        entries.append(previous)
        return original(plan, rules, rule_context, previous)

    monkeypatch.setattr(evaluation, "_evaluate_plan", observed)
    assert business(compute(worker, CUT)) == business(result)
    assert entries and entries[0] is holder.entries
    assert _commit_prepared_candidate(expected)
    worker.set_generation(encode_state(state, args[1], 1), 1)
    assert worker.initialization_count == 2
    assert worker.context._evaluation_reuse is not holder
    assert fingerprint(worker.state) == fingerprint(state) != before


@pytest.mark.parametrize("change", ("problem", "evaluation", "same_generation", "obsolete_generation", "task_generation", "task_request"))
def test_invalid_identity_or_evaluation_never_replaces_current_generation(change):
    state, _, args = worker_case()
    if change == "problem":
        with pytest.raises(ValueError, match="problem fingerprint"):
            workers.CandidateComputer(args[0], args[1], "wrong", args[3])
        return
    worker = workers.CandidateComputer(*args)
    worker.set_generation(encode_state(state, args[1], 1), 1)
    before = fingerprint(worker.state)
    with pytest.raises(ValueError):
        if change == "evaluation":
            state.current_evaluation = replace(state.current_evaluation, quality_key=(*state.current_evaluation.quality_key[:-1], 9))
            worker.set_generation(encode_state(state, args[1], 2), 2)
        elif change == "same_generation":
            state.accepted_move_count += 1
            worker.set_generation(encode_state(state, args[1], 1), 1)
        elif change == "obsolete_generation":
            worker.set_generation(encode_state(state, args[1], 0), 0)
        else:
            compute(worker, **({"generation": 0} if change == "task_generation" else {"request_fingerprint": "wrong"}))
    assert worker.generation == 1 and fingerprint(worker.state) == before


def test_candidate_error_is_local_but_cancellation_discards_late_improvement(monkeypatch):
    state, _, args = worker_case()
    event = mp.get_context("spawn").Event()
    worker = workers.CandidateComputer(*args, cancellation=workers._Cancellation(event))
    worker.set_generation(encode_state(state, args[1], 0), 0)
    error = compute(worker, ("unknown_action",))
    assert error["status"] == "candidate_error"
    assert error["error"]["type"] == "ValueError" and "Unknown width" in error["error"]["traceback"]
    assert compute(worker)["status"] == "improvement"
    original = workers._compute_width_recipe

    def late(*args):
        result = original(*args)
        event.set()
        return result

    monkeypatch.setattr(workers, "_compute_width_recipe", late)
    result = compute(worker)
    assert result["status"] == "aborted" and "proposal" not in result
    assert not compute(worker)["attempted"]
    assert worker.context.accepted_move_traces == ()


def test_expired_initialization_uses_real_budget():
    _, _, args = worker_case()
    now = monotonic()
    with pytest.raises(TimeoutError):
        workers.CandidateComputer(*args[:3], (now - 3, now - 2, now - 1))


def test_true_spawn_returns_candidates_before_error_and_reuses_generations():
    state, context, args = worker_case()
    first = encode_state(state, args[1], 0)
    prepared = _compute_width_recipe(state, context, MOVE)
    assert _commit_prepared_candidate(prepared)
    second = encode_state(state, args[1], 1)
    groups = ((0, first, (("first", 0, MOVE), ("first", 1, ("unknown_action",)))),
              (0, first, (("repeat", 2, CUT),)), (1, second, (("next", 3, ORDER),)))
    local = workers.CandidateComputer(*args)
    expected = []
    for generation, message, tasks in groups:
        local.set_generation(message, generation)
        expected.extend(business(compute(local, recipe, window_id=window, ordinal=ordinal)) for window, ordinal, recipe in tasks)
    spawn = mp.get_context("spawn")
    parent, child = spawn.Pipe(duplex=False)
    event = spawn.Event()
    process = spawn.Process(target=workers.run_candidate_worker, args=(child, event, *args, groups))
    messages = []
    try:
        process.start()
        child.close()
        while not messages or messages[-1]["kind"] != "finished":
            assert parent.poll(30)
            messages.append(parent.recv())
        process.join(5)
        assert process.exitcode == 0
        actual = [business(m["result"]) for m in messages if m["kind"] == "candidate"]
        # Traceback caller locations differ; the original candidate error is retained.
        for a, b in zip(actual, expected):
            if a["status"] == "candidate_error":
                assert a["error"].pop("traceback") and b["error"].pop("traceback")
        assert actual == expected
        assert messages[-1]["generation_initializations"] == 2
        assert messages[-1]["logical_checks"] == 0
        assert messages[-1]["final_state_fingerprint"] == fingerprint(state)
    finally:
        if process.is_alive():
            process.kill()
            process.join(5)
        parent.close()
        child.close()
        if not process.is_alive():
            process.close()
