import json, hashlib
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
root = Path("/Users/miles/dev/dev-py/APSGOV7/diagnostics/unified_local_search/result_comparison_GDCUx1")
archive = Path("/Users/miles/dev/dev-py/APSGOV7/diagnostics/unified_local_search")
def read(path):
    return json.loads(path.read_text(), parse_float=Decimal)
def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()
rows = []
for name in read(root / "comparison_plan.json")["order"]:
    dataset, version = name.split("_", 1)
    history = archive / (f"stage_00/{dataset}" if version == "baseline" else
                        f"stage_03/priority_{dataset}_04" if version == "full_revisit" else
                        f"stage_03/focused_{dataset}_05")
    run = root / name
    m, old = read(run / "measurement.json"), read(history / "measurement.json")
    identity, old_identity = read(run / "input_identity.json"), read(history / "input_identity.json")
    request = read(run / "prepared_request.json")["request"]
    original = archive / "stage_00" / dataset
    baseline = read(original / "measurement.json")
    assert sha(run / "prepared_request.json") == sha(original / "prepared_request.json")
    assert identity["original_policy"] == identity["measured_policy"]
    assert identity["original_request_fingerprint"] == identity["measured_request_fingerprint"]
    assert identity["source"] == old_identity["source"] and identity["tool_sha256"] == old_identity["tool_sha256"]
    assert m["first_search"]["initial"]["plan_fingerprint"] == baseline["first_search"]["initial"]["plan_fingerprint"]
    final = m["final_search"]
    repeat = {key: final[key] == old["final_search"][key] for key in
              ("plan_fingerprint", "evaluation_fingerprint", "trace_fingerprint", "quality")}
    repeat["counters"] = m["result"]["run_manifest"]["counters"] == old["result"]["run_manifest"]["counters"]
    assert all(repeat.values()), (name, repeat)
    counters = m["result"]["run_manifest"]["counters"]
    assert counters["candidate_check_count"] == 200000
    assert m["result"]["stop_reason"] == "candidate_limit_reached"
    assert m["result"]["core_audit"]["passed"] and m["result"]["audit_report"]["passed"]
    chains = final["plan"]["chains"]
    expected = {n["source_order_id"]: n["weight"] for n in request["orders"]}
    assert len(expected) == len(request["orders"]) == 531
    periods = {p["period_id"]: p["sequence"] for p in request["periods"]}
    actual, ids = defaultdict(Decimal), set()
    virtual, borrowed, virtual_count, late = Decimal(0), Decimal(0), 0, 0
    for ch in chains:
        for n in ch["nodes"]:
            assert n["node_id"] not in ids
            ids.add(n["node_id"])
            if n["material_role"] == "virtual_sphc":
                virtual += n["weight"]
                virtual_count += 1
            else:
                actual[n["source_order_id"]] += n["weight"]
                late += periods[ch["assigned_period"]] > periods[n["source_period"]]
                if periods[ch["assigned_period"]] < periods[n["source_period"]]:
                    borrowed += n["weight"]
    assert dict(actual) == expected and late == 0
    gaps = [abs(a["nodes"][-1]["width"] - b["nodes"][0]["width"]) for a, b in zip(chains, chains[1:])]
    assert sum(gaps) == final["quality"][4] and virtual == final["quality"][5]
    assert len(chains) == final["quality"][6]
    rows.append({
        "run": name, "quality": final["quality"], "wall_seconds": m["public_call_wall_seconds"],
        "cpu_seconds": m["public_call_cpu_seconds"], "counters": counters,
        "status": m["result"]["status"], "stop_reason": m["result"]["stop_reason"],
        "not_worse_than_original_baseline": final["quality"] <= baseline["final_search"]["quality"],
        "historical_reproduction": repeat, "input_and_initial_plan_identical": True,
        "core_audit_passed": True, "result_audit_passed": True,
        "independent_source_weight_conservation": True, "independent_late_order_count": late,
        "independent_width_gap_sum": sum(gaps), "largest_width_gap": max(gaps),
        "mean_width_gap": sum(gaps) / Decimal(len(gaps)), "virtual_count": virtual_count,
        "virtual_weight": virtual, "real_weight": sum(expected.values()),
        "virtual_percent": virtual * 100 / (virtual + sum(expected.values())),
        "borrowed_real_weight": borrowed,
        "history_directory": str(history), "source_sha256": identity["source"]["production_source_sha256"],
        "tool_sha256": identity["tool_sha256"], "request_sha256": sha(run / "prepared_request.json"),
        "artifacts": {p.name: sha(p) for p in sorted(run.glob("*.json"))},
        "log_sha256": sha(root / (name + ".log")),
    })
print(json.dumps({"scope": "six_fresh_runs_comparison_not_formal_performance_acceptance", "rows": rows},
                 indent=2, default=lambda x: float(x)))
