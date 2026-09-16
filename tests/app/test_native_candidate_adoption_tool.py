"""An artificial performance window must never leak its limit into final audit."""

from types import SimpleNamespace

import pytest

from tools import verify_native_candidate_adoption as tool


@pytest.mark.parametrize("mode", ("single", "native_serial"))
@pytest.mark.parametrize("fail", (False, True))
def test_window_restores_original_policy_and_counts_native_calls(monkeypatch, tmp_path, mode, fail):
    state = SimpleNamespace(plan=SimpleNamespace(fingerprint="original"))
    budget = SimpleNamespace(candidate_check_count=134226, candidate_check_limit=400000)
    records = []

    class Pool:
        def __init__(self, *, _native_executor):
            self.executor = _native_executor

        def prepare_many(self):
            return ()

    def refine(current, runtime):
        assert runtime.candidate_check_limit == 154226
        pool = tool.refinement.NumericCandidateBatchWorkspace(_native_executor="serial")
        if pool.executor is not None:
            pool.prepare_many()
        if fail:
            raise RuntimeError("injected refinement failure")
        runtime.candidate_check_count = 154226
        current.plan.fingerprint = "changed"

    def solve(request, destination):
        tool.refinement.improve_numeric_refinement(state, budget)
        assert budget.candidate_check_limit == 400000
        return {"core_audit": {"passed": True}, "application_audit": {"passed": True},
                "wall_seconds": 1, "cpu_seconds": 1, "peak_rss_bytes": 1}

    monkeypatch.setattr(tool.refinement, "NumericCandidateBatchWorkspace", Pool)
    monkeypatch.setattr(tool.refinement, "improve_numeric_refinement", refine)
    monkeypatch.setattr(tool, "_run_once", solve)
    monkeypatch.setattr(tool, "record_json", lambda path, value: records.append(value))
    if fail:
        with pytest.raises(RuntimeError, match="injected"):
            tool.run(None, tmp_path, mode, 20000)
    else:
        result = tool.run(None, tmp_path, mode, 20000)
        assert result["native_batch_calls"] == (mode == "native_serial")
        assert result["stages"][0]["entry_checks"] == 134226
        assert result["stages"][0]["exit_checks"] == 154226
        assert len(records) == 1
    assert budget.candidate_check_limit == 400000
    assert tool.refinement.NumericCandidateBatchWorkspace is Pool
