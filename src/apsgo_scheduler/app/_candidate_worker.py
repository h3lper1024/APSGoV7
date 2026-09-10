"""Private candidate computation in an owned process; no formal acceptance."""

import os
from time import perf_counter, process_time
import traceback

from ._candidate_payload import _pack, decode_request, decode_state
from .input_normalizer import normalize_input
from .rule_set_loader import load_rule_set
from ..core.budget import SolveRuntimeBudget
from ..core.compatibility import RuleEdgeDecisionCache
from ..core.contracts import fingerprint, require_int, require_text
from ..core.evaluation import _evaluate_candidate_plan
from ..core.neighborhoods import SearchContext, _validate_search
from ..core.rules.base import RuleEvaluationContext
from ..core.virtual_material import VirtualFactory
from ..core.width_optimization import _compute_width_recipe


class _Cancellation:
    def __init__(self, event):
        self.event = event

    def is_cancelled(self):
        return self.event.is_set()


class CandidateComputer:
    """One request and one accepted-state cache per current generation."""

    def __init__(self, request_message, request_fingerprint, problem_fingerprint, timing, cancellation=None):
        self.request_fingerprint = request_fingerprint
        request = decode_request(request_message, request_fingerprint)
        self.runtime = SolveRuntimeBudget(*timing, request.policy.candidate_check_limit, 0, cancellation)
        if not self.runtime.allows_search():
            raise TimeoutError("candidate worker stopped before initialization")
        rules = load_rule_set(request.rule_set_spec)
        problem = normalize_input(request, rules)
        if problem.input_fingerprint != problem_fingerprint:
            raise ValueError("candidate worker problem fingerprint mismatch")
        rule_context = RuleEvaluationContext(problem.period_order, {p: i for i, p in enumerate(problem.period_order)}, tuple(p.prototype_id for p in problem.virtual_prototypes))
        cache = RuleEdgeDecisionCache(problem, rules, rule_context, request.policy.numeric_semantics_key)
        self.context = SearchContext(VirtualFactory(cache, self.runtime), request.policy)
        self.state = None
        self.generation = None
        self.state_fingerprint = None
        self.initialization_count = 0

    def set_generation(self, message, generation):
        require_int(generation, "generation")
        if self.generation is not None and generation < self.generation:
            raise ValueError("candidate worker received an obsolete generation")
        state = decode_state(message, self.request_fingerprint, generation)
        if generation == self.generation:
            if message["body_fingerprint"] != self.state_fingerprint:
                raise ValueError("same generation cannot replace its accepted state")
            return
        if not self.runtime.allows_search():
            raise TimeoutError("candidate worker stopped before generation initialization")
        _validate_search(state, self.context)
        cache = self.context.factory.cache
        evaluation, holder = _evaluate_candidate_plan(state.current_plan, state, cache.rule_set, cache.context, None)
        if _pack(evaluation) != _pack(state.current_evaluation):
            raise ValueError("candidate worker recomputed evaluation differs from snapshot")
        if not self.runtime.allows_search():
            raise TimeoutError("candidate worker stopped during generation initialization")
        # Publish a fully checked local generation, never a speculative candidate.
        self.state = state
        self.context._evaluation_reuse = holder
        self.generation = generation
        self.state_fingerprint = message["body_fingerprint"]
        self.initialization_count += 1

    def compute(self, recipe, *, request_fingerprint, generation, window_id, ordinal):
        require_int(generation, "generation")
        require_int(ordinal, "ordinal")
        require_text(window_id, "window_id")
        if self.state is None or request_fingerprint != self.request_fingerprint or generation != self.generation:
            raise ValueError("candidate task request or generation mismatch")
        started, cpu_started = perf_counter(), process_time()
        previous_count = self.context.complete_candidate_evaluation_count
        result = {"request_fingerprint": request_fingerprint, "generation": generation,
                  "window_id": window_id, "ordinal": ordinal, "status": "aborted", "attempted": False}
        try:
            if not self.runtime.allows_search():
                return result
            result["attempted"] = True
            if type(recipe) is not tuple or not recipe or type(recipe[0]) is not str or any(type(i) is not int or i < 0 for i in recipe[1:]):
                raise ValueError("invalid candidate recipe")
            prepared = _compute_width_recipe(self.state, self.context, recipe)
            if not self.runtime.allows_search():
                return result
            result["status"] = "rejected" if prepared is None else "improvement"
            if prepared is not None:
                result["proposal"] = _pack((prepared.plan, prepared.evaluation,
                    prepared.affected_chain_ids, prepared.virtual_sequence, prepared.action_name))
            return result
        except Exception as error:
            result.update(status="candidate_error", error={"type": type(error).__name__, "message": str(error), "traceback": traceback.format_exc()})
            return result
        finally:
            result["complete_evaluations"] = self.context.complete_candidate_evaluation_count - previous_count
            result["stop_reason"] = None if self.runtime.stop_reason is None else self.runtime.stop_reason.value
            result["wall_seconds"] = perf_counter() - started
            result["cpu_seconds"] = process_time() - cpu_started


def run_candidate_worker(connection, cancelled, request_message, identity, problem_fingerprint, timing, groups):
    """Offline bounded groups; each candidate returns its own ordered envelope.

    This entry does not enumerate, debit logical checks, accept or serve HTTP.
    The parent owns process lifetime and draining the private result channel.
    """
    started, cpu_started = perf_counter(), process_time()
    try:
        worker = CandidateComputer(request_message, identity, problem_fingerprint, timing, _Cancellation(cancelled))
        connection.send({"kind": "ready", "pid": os.getpid(), "request_fingerprint": identity})
        for generation, message, tasks in groups:
            worker.set_generation(message, generation)
            for window_id, ordinal, recipe in tasks:
                connection.send({"kind": "candidate", "result": worker.compute(recipe,
                    request_fingerprint=identity, generation=generation, window_id=window_id, ordinal=ordinal)})
        connection.send({"kind": "finished", "generation_initializations": worker.initialization_count,
            "logical_checks": worker.runtime.candidate_check_count,
            "final_state_fingerprint": fingerprint(worker.state),
            "wall_seconds": perf_counter() - started, "cpu_seconds": process_time() - cpu_started})
    except Exception as error:
        connection.send({"kind": "worker_error", "error": {"type": type(error).__name__, "message": str(error), "traceback": traceback.format_exc()}})
        raise
    finally:
        connection.close()
