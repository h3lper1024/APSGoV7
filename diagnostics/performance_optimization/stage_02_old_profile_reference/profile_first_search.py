"""Diagnostic-only replay: stop after the original first local search; no DB or service."""
import argparse
import ast
import cProfile
import hashlib
import json
import platform
import pstats
import re
from contextlib import ExitStack
from decimal import Decimal
from pathlib import Path
from time import perf_counter, process_time
from unittest.mock import patch

from apsgo_scheduler.api.request import (
    OrderInput, PeriodInput, QualityCriterionSpec, RuleDefinitionSpec, RuleSetSpec,
    SchedulingRequest, VirtualPrototypeInput, fingerprint_public_request,
)
from apsgo_scheduler.app.service import solve_request
from apsgo_scheduler.core import neighborhoods, solver
from apsgo_scheduler.core.contracts import RuleScope, SolverPolicy, fingerprint
from apsgo_scheduler.core.model import MaterialRole
from apsgo_v7_service.diagnostics import _json_values

SOURCE = Path('/tmp/apsgo-v7-cpu-diagnosis.hFs5W6/20260908_171033_uzpp3la1')
OUTPUT = Path(__file__).parent


def load_request():
    data = json.loads((SOURCE / 'prepared_request.json').read_text(), parse_float=Decimal)
    request = data['request'].copy()
    request['orders'] = tuple(OrderInput(**{**v, 'material_role': MaterialRole(v['material_role'])})
                              for v in request['orders'])
    request['periods'] = tuple(PeriodInput(**v) for v in request['periods'])
    request['virtual_prototypes'] = tuple(VirtualPrototypeInput(**v) for v in request['virtual_prototypes'])
    spec = request['rule_set_spec'].copy()
    spec['rules'] = tuple(RuleDefinitionSpec(**{**v, 'scope': RuleScope(v['scope'])}) for v in spec['rules'])
    spec['quality_spec'] = tuple(QualityCriterionSpec(**v) for v in spec['quality_spec'])
    request['rule_set_spec'] = RuleSetSpec(**spec)
    request['policy'] = SolverPolicy(**request['policy'])
    request = SchedulingRequest(**request)
    assert fingerprint_public_request(request) == data['request_fingerprint']
    assert request.rule_set_spec.fingerprint == data['rule_set_fingerprint']
    return request


def windows_trace():
    result = []
    for line in (SOURCE / 'solve.log').read_text().splitlines():
        if 'solver_stage_finished stage=local_search ' in line:
            break
        if 'solver_move_accepted ' not in line:
            continue
        sequence = int(re.search(r' sequence=(\d+)', line)[1])
        action = re.search(r' action_name=(\S+)', line)[1]
        checks = int(re.search(r' candidate_check_count=(\d+)', line)[1])
        before, after = re.search(r' quality_before=(.*?) quality_after=(.*?) candidate_check_count=', line).groups()
        def values(text):
            # Parse only literals, never execute expressions from the attachment.
            text = re.sub(r"Decimal\('([^']+)'\)", r"'\1'", text)
            return tuple(Decimal(str(v)) for v in ast.literal_eval(text))
        result.append((sequence, action, checks, values(before), values(after)))
    assert len(result) == 34
    return result


class FirstSearchComplete(BaseException):
    """Leave before the production solver can proceed to split/replay/audits."""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['check', 'baseline', 'profile'])
    args = parser.parse_args()
    request = load_request()
    expected = windows_trace()
    print('Input fingerprint verified:', fingerprint_public_request(request), flush=True)
    if args.mode == 'check':
        return
    report_path = OUTPUT / (args.mode + '_verified.json')
    assert not report_path.exists()
    profile = cProfile.Profile() if args.mode == 'profile' else None
    records = []
    report = {
        'mode': args.mode, 'platform': platform.platform(), 'python': platform.python_version(),
        'request_fingerprint': fingerprint_public_request(request),
        'prepared_request_sha256': hashlib.sha256((SOURCE / 'prepared_request.json').read_bytes()).hexdigest(),
        'policy_unchanged': _json_values(request.policy), 'production_code_revision': 'ec3e0a79f0a74675ad478d2011a8aad9136f8b78',
        'measurement_scope': 'first_local_search_only_no_release',
    }
    original_search = solver.run_local_search

    def counted_function(name, original):
        def wrapped(state, context):
            start = perf_counter(), process_time()
            before = (context.factory.budget.candidate_check_count,
                      context.complete_candidate_evaluation_count, state.accepted_move_count)
            print('BEGIN', name, flush=True)
            try:
                return original(state, context)
            finally:
                duration = perf_counter() - start[0]
                cpu = process_time() - start[1]
                after = (context.factory.budget.candidate_check_count,
                         context.complete_candidate_evaluation_count, state.accepted_move_count)
                item = {'function': name, 'wall_seconds': duration, 'cpu_seconds': cpu,
                        'candidate_checks': after[0] - before[0],
                        'complete_evaluations': after[1] - before[1],
                        'accepted_moves': after[2] - before[2]}
                records.append(item)
                print('END', json.dumps(item), flush=True)
        return wrapped

    def first_search(state, context):
        cache_before = (context.factory.cache.hit_count, context.factory.cache.miss_count)
        report['initial_plan_fingerprint'] = fingerprint(state.current_plan)
        report['problem_fingerprint'] = context.factory.cache.problem.input_fingerprint
        report['initial_quality'] = _json_values(state.current_evaluation.quality_key)
        start = perf_counter(), process_time()
        if profile:
            profile.enable()
        try:
            original_search(state, context)
        finally:
            if profile:
                profile.disable()
            report['wall_seconds'] = perf_counter() - start[0]
            report['cpu_seconds'] = process_time() - start[1]
        report.update({
            'functions': records,
            'candidate_check_count': context.factory.budget.candidate_check_count,
            'complete_candidate_evaluation_count': context.complete_candidate_evaluation_count,
            'accepted_move_count': state.accepted_move_count,
            'final_chain_count': len(state.current_plan.chains),
            'quality': _json_values(state.current_evaluation.quality_key),
            'plan_fingerprint': fingerprint(state.current_plan),
            'trace_fingerprint': fingerprint(context.accepted_move_traces),
            'stop_reason': context.factory.budget.stop_reason.value,
            'trace': _json_values(context.accepted_move_traces),
            'edge_cache_hits': context.factory.cache.hit_count - cache_before[0],
            'edge_cache_misses': context.factory.cache.miss_count - cache_before[1],
        })
        actual = [(v.sequence, v.action_name, v.candidate_check_count,
                   tuple(Decimal(n) for n in v.quality_before), tuple(Decimal(n) for n in v.quality_after))
                  for v in context.accepted_move_traces]
        report['windows_34_accepted_events_match'] = actual == expected
        report['windows_final_counts_match'] = (
            report['candidate_check_count'] == 64226 and report['complete_candidate_evaluation_count'] == 2263
            and report['accepted_move_count'] == 34 and report['final_chain_count'] == 22)
        report['window_problem_matches'] = report['problem_fingerprint'] == 'ab8bb3ad7588a9d5431ac1b5d3baf6370d78238f57b5382367aac3c6e196720e'
        with report_path.open('x') as stream:
            # Observation timings are floats; Decimal business values stay textual and exact.
            stream.write(json.dumps(report, ensure_ascii=False, default=str, allow_nan=False, indent=2) + '\n')
        if profile:
            profile.dump_stats(str(OUTPUT / 'first_search.pstats'))
            with (OUTPUT / 'profile_functions.txt').open('x') as stream:
                stats = pstats.Stats(profile, stream=stream)
                stats.sort_stats('cumulative').print_stats(65)
                stats.sort_stats('tottime').print_stats(45)
                stats.print_callers('bridge|evaluate_plan|canonical_json|__post_init__|allows_search|materialize')
        assert report['windows_34_accepted_events_match'] and report['windows_final_counts_match']
        assert report['window_problem_matches']
        print('FIRST_SEARCH_COMPLETE', report['wall_seconds'], 'seconds, Windows trace/counters match', flush=True)
        raise FirstSearchComplete

    with ExitStack() as stack:
        for name in ('improve_whole_chain', 'improve_real_node_relocation',
                     'improve_virtual_weight_fill', 'improve_chain_order'):
            stack.enter_context(patch.object(neighborhoods, name, counted_function(name, getattr(neighborhoods, name))))
        stack.enter_context(patch.object(solver, 'run_local_search', first_search))
        try:
            result = solve_request(request)
        except FirstSearchComplete:
            return
        raise RuntimeError('Did not reach completed first local search: ' + str((result.status, result.stop_reason, result.issues)))


if __name__ == '__main__':
    main()
