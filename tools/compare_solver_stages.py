"""Compare reference artifacts or stage JSON, preserving the first semantic difference."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
from typing import Any

from capture_solverpy_reference import (
    DEFAULT_MANIFEST,
    OUTPUT_NAMES,
    REPO_ROOT,
    load_manifest,
    resolve_path,
    verify_manifest,
)

MANIFEST_METADATA_KEYS = {
    "actual_runtime_seconds",
    "started_at",
    "finished_at",
    "best_solution_updated_at",
    "input_paths",
    "runtime_environment",
}


def semantic_manifest(
    value: dict[str, Any], *, allow_capture_time_budget: bool = False
) -> dict[str, Any]:
    ignored = MANIFEST_METADATA_KEYS | (
        {"time_budget_seconds"} if allow_capture_time_budget else set()
    )
    return {key: item for key, item in value.items() if key not in ignored}


def first_difference(expected: Any, actual: Any, path: str = "$") -> dict[str, Any] | None:
    if type(expected) is not type(actual):
        return {"path": path, "expected": expected, "actual": actual, "reason": "type differs"}
    if isinstance(expected, dict):
        for key in sorted(set(expected) | set(actual)):
            if key not in expected or key not in actual:
                return {
                    "path": f"{path}.{key}",
                    "expected_present": key in expected,
                    "actual_present": key in actual,
                    "reason": "key differs",
                }
            difference = first_difference(expected[key], actual[key], f"{path}.{key}")
            if difference:
                return difference
    elif isinstance(expected, list):
        for index, (left, right) in enumerate(zip(expected, actual)):
            difference = first_difference(left, right, f"{path}[{index}]")
            if difference:
                return difference
        if len(expected) != len(actual):
            return {
                "path": path,
                "expected_length": len(expected),
                "actual_length": len(actual),
                "reason": "list length differs",
            }
    elif expected != actual:
        return {"path": path, "expected": expected, "actual": actual, "reason": "value differs"}
    return None


def _read_artifact(path: Path) -> Any:
    if path.suffix == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as stream:
            return list(csv.reader(stream))
    return json.loads(path.read_text(encoding="utf-8"))


def compare_artifacts(
    manifest: dict[str, Any], actual_dir: Path, *, allow_capture_time_budget: bool = False
) -> dict[str, Any]:
    verify_manifest(manifest)
    differences = []
    metadata_differences = {}
    artifact_hashes = {}
    for name in sorted(OUTPUT_NAMES):
        expected_path = resolve_path(manifest["static_outputs"][name]["path"])
        actual_path = actual_dir / name
        if not actual_path.is_file():
            differences.append({"path": name, "reason": "actual artifact missing"})
            continue
        expected, actual = _read_artifact(expected_path), _read_artifact(actual_path)
        artifact_hashes[name] = hashlib.sha256(actual_path.read_bytes()).hexdigest()
        if name == "run_manifest.json":
            ignored = MANIFEST_METADATA_KEYS | (
                {"time_budget_seconds"} if allow_capture_time_budget else set()
            )
            metadata_differences = {
                key: {"expected": expected.get(key), "actual": actual.get(key)}
                for key in sorted(ignored)
                if expected.get(key) != actual.get(key)
            }
            expected = semantic_manifest(
                expected, allow_capture_time_budget=allow_capture_time_budget
            )
            actual = semantic_manifest(actual, allow_capture_time_budget=allow_capture_time_budget)
        difference = first_difference(expected, actual, name)
        if difference:
            differences.append(difference)
        elif name != "run_manifest.json" and expected_path.read_bytes() != actual_path.read_bytes():
            differences.append({"path": name, "reason": "serialization bytes differ"})
    return {
        "status": "fail" if differences else "pass",
        "equivalent": not differences,
        "first_difference": differences[0] if differences else None,
        "differences": differences,
        "metadata_differences": metadata_differences,
        "artifact_hashes": artifact_hashes,
    }


def compare_frozen_stage(manifest: dict[str, Any], stage: dict[str, Any]) -> dict[str, Any]:
    """Read back the frozen trace against the independently hashed static artifacts."""
    verify_manifest(manifest)
    expected = _read_artifact(resolve_path(manifest["static_outputs"]["run_manifest.json"]["path"]))
    final = stage["final"]
    if stage["instrumentation"]["time_budget_seconds_override"] != 600.0:
        raise ValueError("unexpected stage instrumentation budget")
    difference = first_difference(
        semantic_manifest(expected, allow_capture_time_budget=True),
        semantic_manifest(final["run_manifest"], allow_capture_time_budget=True),
        "final.run_manifest",
    )
    if difference is None:
        for name in sorted(OUTPUT_NAMES - {"run_manifest.json"}):
            difference = first_difference(
                manifest["static_outputs"][name]["sha256"],
                final["artifact_hashes"][name],
                f"final.artifact_hashes.{name}",
            )
            if difference:
                break
    return {
        "equivalent": difference is None,
        "first_difference": difference,
        "comparison": "frozen stage final vs immutable static artifacts",
        "explicit_time_budget_override": stage["instrumentation"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-only", action="store_true")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--expected", type=Path)
    parser.add_argument("--actual", type=Path)
    parser.add_argument("--allow-capture-time-budget", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.reference_only:
        manifest = load_manifest(args.manifest)
        if args.actual:
            result = compare_artifacts(
                manifest, args.actual, allow_capture_time_budget=args.allow_capture_time_budget
            )
        else:
            path = (
                args.expected
                or REPO_ROOT / "tests/baselines/gqga4/reference_stage_expectations.json"
            )
            result = compare_frozen_stage(manifest, _read_artifact(path))
    elif args.expected and args.actual:
        difference = first_difference(_read_artifact(args.expected), _read_artifact(args.actual))
        result = {"equivalent": difference is None, "first_difference": difference}
    else:
        parser.error("use --reference-only or provide both --expected and --actual")
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(rendered)
    print(rendered, end="")
    return 0 if result["equivalent"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
