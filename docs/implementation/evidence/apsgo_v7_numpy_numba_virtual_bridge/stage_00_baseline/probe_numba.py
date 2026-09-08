"""Small compilation feasibility probe, not a solver or a performance benchmark."""

import argparse
import hashlib
import json
import platform
import sys
from importlib.metadata import version
from pathlib import Path
from time import perf_counter, process_time


def probe():
    import numba
    import numpy as np

    @numba.njit(cache=False, fastmath=False, parallel=False, boundscheck=True)
    def choose(widths, thicknesses, width_missing, thickness_missing, parameters):
        best_index, best_score = -1, 0.0
        left_width, right_width, left_thickness, right_thickness, limit = parameters
        for index in range(len(widths)):
            if width_missing[index]:
                continue
            width = widths[index]
            if abs(width - left_width) > limit or abs(right_width - width) > limit:
                continue
            thickness = 0.0 if thickness_missing[index] else thicknesses[index]
            width_cost = abs(left_width - width) + abs(width - right_width)
            thickness_cost = abs(left_thickness - thickness) + abs(thickness - right_thickness)
            score = width_cost + 100.0 * thickness_cost
            if not np.isfinite(score):
                raise ValueError("probe score must be finite")
            if best_index == -1 or score < best_score:
                best_index, best_score = index, score
        return best_index, best_score

    widths = np.array([1200.0, 1150.0, 1200.0, 0.0], dtype=np.float64)
    thicknesses = np.array([1.0, 1.0, 1.5, 1.0], dtype=np.float64)
    width_missing = np.array([False, False, False, True], dtype=np.bool_)
    thickness_missing = np.zeros(4, dtype=np.bool_)
    parameters = np.array([1100.0, 1300.0, 1.0, 1.0, 200.0], dtype=np.float64)
    arguments = widths, thicknesses, width_missing, thickness_missing, parameters
    assert all(array.ndim == 1 and array.flags.c_contiguous for array in arguments)
    assert widths.shape == thicknesses.shape == width_missing.shape == thickness_missing.shape
    assert parameters.shape == (5,)
    assert choose.signatures == [] and choose.nopython_signatures == []
    expected = choose.py_func(*arguments)
    assert expected == (0, 200.0), "equal scores must retain the first catalog position"
    assert choose.signatures == [], "the Python reference must not compile the dispatcher"

    timings = []
    for label in ("first_call_including_compilation", "repeat_1", "repeat_2", "repeat_3"):
        started_wall, started_cpu = perf_counter(), process_time()
        result = choose(*arguments)
        elapsed_cpu, elapsed_wall = process_time() - started_cpu, perf_counter() - started_wall
        assert result == expected == choose.py_func(*arguments)
        timings.append({
            "call": label,
            "wall_seconds": elapsed_wall,
            "process_cpu_seconds": elapsed_cpu,
            "selected_index": int(result[0]),
            "score": float(result[1]),
        })
    assert choose.nopython_signatures, "a Python fallback is not a successful feasibility probe"
    signatures = tuple(str(item) for item in choose.nopython_signatures)

    widths[0] = 900.0
    assert choose(*arguments) == choose.py_func(*arguments) == (1, 200.0)
    widths[0] = 1200.0
    width_missing[0] = True
    assert choose(*arguments) == choose.py_func(*arguments) == (1, 200.0)
    width_missing[0] = False
    thickness_missing[0] = True
    assert choose(*arguments) == choose.py_func(*arguments) == (1, 200.0)
    thickness_missing[0] = False
    parameters[4] = 90.0
    assert choose(*arguments) == choose.py_func(*arguments) == (-1, 0.0)
    parameters[4] = 200.0
    assert choose(*arguments) == choose.py_func(*arguments) == expected
    assert tuple(str(item) for item in choose.nopython_signatures) == signatures

    return {
        "status": "passed",
        "scope": "synthetic four-row numeric loop only; no production imports or solver",
        "timing_limitations": (
            "First call includes this tiny kernel's JIT, not dependency imports or process startup. "
            "Repeated microsecond calls do not establish a speedup or production cold-start cost."
        ),
        "runtime": {
            "python": platform.python_version(),
            "executable": sys.executable,
            "platform": platform.platform(),
            "machine": platform.machine(),
            "numpy": np.__version__,
            "numba": numba.__version__,
            "llvmlite": version("llvmlite"),
        },
        "kernel_options": {
            "cache": False, "fastmath": False, "parallel": False, "boundscheck": True,
        },
        "nopython_signatures": signatures,
        "checks_passed": [
            "array_shapes_and_contiguous_layout",
            "no_signature_before_first_compiled_call",
            "strict_less_than_keeps_first_tie",
            "first_and_repeated_calls_equal_python_reference",
            "actual_nopython_signature",
            "changed_numeric_array_is_observed",
            "changed_width_mask_is_observed",
            "changed_thickness_mask_is_observed",
            "changed_parameter_array_is_observed",
            "restored_arguments_restore_result_without_new_signature",
        ],
        "calls": timings,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if sys.flags.optimize:
        raise RuntimeError("Run without -O: this probe requires its assertions")
    # Reserve a new evidence file before importing dependencies or compiling anything.
    with args.output.open("x", encoding="utf-8", newline="\n") as output:
        started_wall, started_cpu = perf_counter(), process_time()
        report = {"probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
        try:
            report.update(probe())
        except Exception as error:
            report.update(status="failed", error_type=type(error).__name__, error=str(error))
            raise
        finally:
            report["probe_wall_seconds"] = perf_counter() - started_wall
            report["probe_process_cpu_seconds"] = process_time() - started_cpu
            json.dump(report, output, ensure_ascii=False, allow_nan=False, indent=2)
            output.write("\n")
    print(f"Feasibility assertions passed; evidence: {args.output}")


if __name__ == "__main__":
    main()
