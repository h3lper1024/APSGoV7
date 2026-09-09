"""Observe actual production kernels; this is not a full solver speed benchmark."""

import argparse
import hashlib
import json
import platform
import sys
from importlib.metadata import version
from pathlib import Path
from time import perf_counter, process_time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[5]
for directory in (ROOT, ROOT / "src"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))


def probe():
    bridge_source = ROOT / "src/apsgo_scheduler/core/virtual_material.py"
    if hashlib.sha256(bridge_source.read_bytes()).hexdigest() != (
        "db91df4d5612b489bf99c01a7df1ba32e3f24b71cf93ae3d7161d1c644b7b98a"
    ):
        raise ValueError("run this stage-2 probe from its frozen pre-integration source tree")
    from apsgo_scheduler.core import _bridge_numeric as numeric
    from apsgo_scheduler.core.model import VirtualPurpose
    from tests.core.search.test_virtual_bridge_numeric import (
        SoftHardConnectionRule,
        TemperatureOverlapRule,
        ThicknessTransitionRule,
        WidthTransitionRule,
        make_factory,
        node,
        prototype,
        rule,
    )

    widths = ("1100", "1300", "1150", "1250")
    factory = make_factory(
        tuple(prototype(f"p-{index}", width=widths[index % len(widths)]) for index in range(129)),
        nodes=(node("left", width="1000"), node("right", width="1400")),
        rule_items=tuple(rule(kind) for kind in (
            WidthTransitionRule, ThicknessTransitionRule, TemperatureOverlapRule,
            SoftHardConnectionRule,
        )),
    )
    catalog = numeric.prepare_catalog(factory)
    assert catalog is not None
    arrays = numeric.prepare_bridge(catalog, *factory.cache.problem.nodes, factory.budget)
    assert arrays is not None
    original = numeric._scan_block
    assert not original.nopython_signatures, "run this probe in a fresh process"
    blocks, calls = [], []

    def timed_block(*args, **kwargs):
        started, cpu_started = perf_counter(), process_time()
        result = original(*args, **kwargs)
        blocks.append({
            "wall_seconds": perf_counter() - started,
            "cpu_seconds": process_time() - cpu_started,
        })
        return result

    with patch.object(numeric, "_scan_block", timed_block):
        for label in ("cold", "warm"):
            first_block = len(blocks)
            started, cpu_started = perf_counter(), process_time()
            single = numeric.scan_single(catalog, *arrays, factory.budget)
            double = numeric.scan_double(catalog, *arrays, factory.budget)
            calls.append({
                "label": label,
                "wall_seconds": perf_counter() - started,
                "cpu_seconds": process_time() - cpu_started,
                "block_count": len(blocks) - first_block,
            })
            assert single == (True, None) and double == (True, (2, 3))
    assert original.nopython_signatures and factory.budget.candidate_check_count == 0
    left, right = factory.cache.problem.nodes
    expected = factory.bridge(left, right, max_nodes=2, first_sequence=7)
    selected = tuple(factory.materialize(
        factory.cache.problem.virtual_prototypes[index], left, right,
        purpose=VirtualPurpose.EDGE_BRIDGE, sequence=7 + offset,
    ) for offset, index in enumerate(double[1]))
    assert selected == expected, "all materialized fields must match the original bridge"
    return {
        "status": "passed",
        "scope": "129 synthetic prototypes; real kernels only, no full solver or Windows",
        "limitations": (
            "First block includes JIT; remaining block timings include observation overhead. "
            "The test budget uses a fixed clock; production time charging is tested at integration. "
            "The cold/warm scan excludes imports and array preparation."
        ),
        "runtime": {
            "python": platform.python_version(), "executable": sys.executable,
            "platform": platform.platform(),
            **{name: version(name) for name in ("numpy", "numba", "llvmlite")},
        },
        "sources": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in (ROOT / "src/apsgo_scheduler/core/_bridge_numeric.py",
                                 ROOT / "src/apsgo_scheduler/core/virtual_material.py")},
        "nopython_signatures": [str(item) for item in original.nopython_signatures],
        "calls": calls,
        "first_block_including_compilation": blocks[0],
        "longest_remaining_block_wall_seconds": max(item["wall_seconds"] for item in blocks[1:]),
        "blocks": blocks,
        "selected_prototypes": list(double[1]),
        "materialized_fields_equal": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    if not __debug__:
        raise RuntimeError("probe assertions require Python without -O")
    with arguments.output.open("x", encoding="utf-8") as stream:
        try:
            result = probe()
        except Exception as error:
            json.dump({"status": "failed", "error": repr(error)}, stream, ensure_ascii=False)
            raise
        json.dump(result, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
    print(json.dumps({"status": result["status"], "calls": result["calls"]}))


if __name__ == "__main__":
    main()
