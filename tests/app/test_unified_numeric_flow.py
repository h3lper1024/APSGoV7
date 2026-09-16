"""Public numeric solving and independent audit cannot re-enter retired paths."""
from dataclasses import replace
from decimal import Decimal
import inspect
import ast

from apsgo_scheduler.app.service import solve_request
from apsgo_scheduler.core import _numeric_search as search
from apsgo_scheduler.core import _numeric_refinement as refinement
from apsgo_scheduler.core import _numeric_candidate_kernel as candidate
from apsgo_scheduler.core import _numeric_solver as solver
from tests.core.test_numeric_boundary_audit import numeric_request


def test_public_flow_and_both_audits_have_no_retired_preparation_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("public numeric flow entered retired preparation")
    for module, names in (
        (search, ("_layout", "_prepare_numeric_split", "_try_prepared_candidate",
                  "_resource_whole_chain_candidate", "extend_resource_workspace")),
        (refinement, ("_prepare_recipe", "_prepare_recipe_batch", "_prepare_repaired_parts",
                      "_candidate_overlay", "_try_overlay_candidate", "_family_stream")),
    ):
        for name in names:
            monkeypatch.setattr(module, name, forbidden)
    monkeypatch.setattr(refinement.NumericRefinementIndex, "build", forbidden)
    calls = {"candidate": 0, "audit": 0}
    compute, audit = candidate.compute_candidate_attempt, solver.audit_numeric_core_without_search_cache
    def counted(*args, **kwargs):
        calls["candidate"] += 1
        return compute(*args, **kwargs)
    def audited(*args, **kwargs):
        calls["audit"] += 1
        return audit(*args, **kwargs)
    monkeypatch.setattr(candidate, "compute_candidate_attempt", counted)
    monkeypatch.setattr(solver, "audit_numeric_core_without_search_cache", audited)
    request = numeric_request()
    request = replace(request, policy=replace(request.policy, candidate_check_limit=100,
        total_time_limit_seconds=Decimal("10009"), finalization_reserve_seconds=Decimal("10")))
    result = solve_request(request)
    assert calls["candidate"] > 0 and calls["audit"] == 1
    assert result.core_audit.passed and result.audit_report.passed


def test_active_stage_call_graph_excludes_retired_preparation():
    banned = {"_layout", "_prepare_numeric_split", "_try_prepared_candidate",
              "_prepare_recipe", "_prepare_recipe_batch", "_try_overlay_candidate",
              "_candidate_overlay", "extend_resource_workspace", "_source_recipe_stream",
              "_resource_whole_chain_candidate", "apply_numeric_candidate_edit"}
    # Inspect transitive local calls, rather than matching dead definitions in a
    # module that temporarily retains old functions for comparison tests.
    for module, entries in (
        (search, ("_run_numeric_local_search", "improve_numeric_controlled_split")),
        (refinement, ("improve_numeric_refinement", "_descriptor_attempts")),
    ):
        functions = {node.name: node for node in ast.parse(inspect.getsource(module)).body
                     if isinstance(node, ast.FunctionDef)}
        pending, visited = list(entries), set()
        while pending:
            name = pending.pop()
            if name in visited:
                continue
            visited.add(name)
            for call in ast.walk(functions[name]):
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
                    target = call.func.id
                    assert target not in banned, (module.__name__, name, target)
                    if target in functions:
                        pending.append(target)
