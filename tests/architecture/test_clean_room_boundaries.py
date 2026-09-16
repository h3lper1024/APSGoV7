"""Keep the new production tree independent of old worktree leftovers."""

import ast
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "src" / "apsgo_scheduler"
SERVICE_PACKAGE = ROOT / "src" / "apsgo_v7_service"
ALLOWED_SOURCE_PREFIXES = ("src/apsgo_scheduler/", "src/apsgo_v7_service/")
LAYERS = {"api", "core", "app"}
OLD_SOURCE = re.compile(
    r"\b(?:apsgo|shared_kernel|kernel_contracts|OptimizationProblem|"
    r"SchedulingSolution|SchedulingOutcome|ResourceLedger)\b"
    r"|APSGOV6|apsgo-v3|PYTHONPATH"
)
REFERENCE_SOURCE = re.compile(r"(?:^/|^[A-Za-z]:[\\/]).*\.py(?:$|[\\/])")
LOCAL_ABSOLUTE_PATH = re.compile(r"^/Users/|^[A-Za-z]:[\\/]")
TEST_SOURCE = re.compile(r"(?:^|[\\/])tests(?:[\\/]|$)")
LINE_NAMES = re.compile(r"GQGA4|GQPT|XQGA")
BENCHMARK_NUMBERS = {531, 29333.91, 37, 3}


def module_name(path, package):
    parts = path.relative_to(package.parent).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def import_targets(node, module, is_package):
    """Resolve relative imports without importing any production code."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    base = node.module or ""
    if node.level:
        package = module if is_package else module.rpartition(".")[0]
        try:
            base = importlib.util.resolve_name("." * node.level + base, package)
        except (ImportError, ValueError) as error:
            raise AssertionError(f"Invalid relative import in {module}") from error
    if base == "apsgo_scheduler":
        assert all(alias.name != "*" for alias in node.names), "No root wildcard import"
        return [f"{base}.{alias.name}" for alias in node.names]
    return [base]


def check_source(source, path, package):
    assert not OLD_SOURCE.search(source), f"Historical source reference: {path}"
    assert not LINE_NAMES.search(source), f"Production line name: {path}"
    tree = ast.parse(source, filename=str(path))
    module = module_name(path, package)
    approved_shape_numbers = set()
    if module == "apsgo_scheduler.core._numeric_construction":
        shape = ast.parse("_GRAPH_CHECKED, _GRAPH_ALLOWED, _GRAPH_DIRECTION = range(3)").body[0]
        for node in tree.body:
            if ast.dump(node) == ast.dump(shape):
                approved_shape_numbers.update(item for item in ast.walk(node) if isinstance(item, ast.Constant))
    if module == "apsgo_scheduler.core._candidate_edit":
        shape = ast.parse('guard != "width_optimization" or len(indices) != 3', mode="eval").body
        for definition in tree.body:
            if isinstance(definition, ast.ClassDef) and definition.name == "CandidateEdit":
                for function in definition.body:
                    if isinstance(function, ast.FunctionDef) and function.name == "bind":
                        for expression in ast.walk(function):
                            if ast.dump(expression) == ast.dump(shape):
                                approved_shape_numbers.update(item for item in ast.walk(expression)
                                                              if isinstance(item, ast.Constant))
    if module == "apsgo_scheduler.core.width_optimization":
        # The approved three-node neighborhood and three critical families are
        # algorithm shapes, not the historical three-violation benchmark result.
        shapes = {
            "_critical_recipe": ast.parse("stop - start > 3 or other_stop - other_start > 3", mode="eval").body,
            "_scan_critical_families": ast.parse("sizes = (3, 6)").body[0],
        }
        for function in tree.body:
            if isinstance(function, ast.FunctionDef) and function.name in shapes:
                for expression in ast.walk(function):
                    if ast.dump(expression) == ast.dump(shapes[function.name]):
                        approved_shape_numbers.update(item for item in ast.walk(expression)
                                                      if isinstance(item, ast.Constant))
    if module == "apsgo_scheduler.core._numeric_rules":
        # Numeric enum ordinals are compact internal codes, not frozen benchmark results.
        for definition in tree.body:
            if isinstance(definition, ast.ClassDef) and any(
                isinstance(base, ast.Name) and base.id == "IntEnum" for base in definition.bases
            ):
                approved_shape_numbers.update(
                    item for item in ast.walk(definition) if isinstance(item, ast.Constant)
                )
    if module == "apsgo_scheduler.core._numeric_resources":
        # The separator subject is left piece / virtual / right piece, not a
        # benchmark result. Permit only this exact allocation in its owner.
        shape = ast.parse("np.arange(3, dtype=np.int64)", mode="eval").body
        for function in tree.body:
            if isinstance(function, ast.FunctionDef) and function.name == "scan_private_separator":
                for expression in ast.walk(function):
                    if ast.dump(expression) == ast.dump(shape):
                        approved_shape_numbers.update(item for item in ast.walk(expression)
                                                      if isinstance(item, ast.Constant))
    if module == "apsgo_scheduler.core._numeric_kernel":
        # Native table column/rule ordinals use 3, just like NumericRuleKind.
        # Other historical benchmark values remain forbidden in this module.
        approved_shape_numbers.update(
            item for item in ast.walk(tree)
            if isinstance(item, ast.Constant) and type(item.value) is int and item.value == 3
        )
    if module in ("apsgo_scheduler.core._numeric_evaluation",
                   "apsgo_scheduler.core._numeric_rules"):
        # Boundary projections of the documented native buffer column 3.
        for node in ast.walk(tree):
            if isinstance(node, ast.Subscript):
                approved_shape_numbers.update(
                    item for item in ast.walk(node.slice)
                    if isinstance(item, ast.Constant) and type(item.value) is int and item.value == 3
                )
    sys_aliases = {"sys"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            targets = import_targets(node, module, path.name == "__init__.py")
            for target in targets:
                top = target.partition(".")[0]
                numeric_dependency = (
                    (module == "apsgo_scheduler.core._bridge_numeric" and top in {"numpy", "numba"})
                    or (module == "apsgo_scheduler.core._numeric_kernel" and top in {"numpy", "numba"})
                    or (module == "apsgo_scheduler.core._numeric_chain_ops" and top in {"numpy", "numba"})
                    or (module == "apsgo_scheduler.core._numeric_candidate_kernel" and top in {"numpy", "numba"})
                    or (module == "apsgo_scheduler.core._numeric_resources" and top in {"numpy", "numba"})
                    or (module == "apsgo_scheduler.core._delivery_parallel" and top in {"numpy", "numba"})
                    or (module == "apsgo_scheduler.core._search_numeric" and top == "numpy")
                        or (module == "apsgo_scheduler.core._numeric_state" and top == "numpy")
                        or (module == "apsgo_scheduler.core._numeric_evaluation" and top == "numpy")
                        or (module == "apsgo_scheduler.core._numeric_construction" and top in {"numpy", "numba"})
                        or (module == "apsgo_scheduler.core._numeric_audit" and top == "numpy")
                        or (module == "apsgo_scheduler.core._numeric_refinement" and top == "numpy")
                        or (module == "apsgo_scheduler.core._numeric_batch" and top == "numpy")
                        or (module == "apsgo_scheduler.core._numeric_resources" and top == "numpy")
                    )
                assert top in sys.stdlib_module_names or top == "apsgo_scheduler" or (
                    numeric_dependency
                ), (
                    f"External production dependency: {target} in {path}"
                )
                assert top not in {"importlib", "runpy", "ctypes"}, (
                    f"Dynamic code loading is outside the production boundary: {target}"
                )
                assert top != "sqlite3", f"Database access belongs in apsgo_v7_service, not {path}"
                if top == "apsgo_scheduler":
                    pieces = target.split(".")
                    assert len(pieces) == 1 or pieces[1] in LAYERS, target
                    local = package.parent.joinpath(*pieces)
                    assert (
                        local.with_suffix(".py").is_file() or (local / "__init__.py").is_file()
                    ), f"Missing production dependency: {target}"
            if isinstance(node, ast.Import):
                sys_aliases.update(
                    alias.asname or alias.name for alias in node.names if alias.name == "sys"
                )
            elif node.module == "sys":
                assert all(alias.name != "path" for alias in node.names), "No sys.path import"
        elif isinstance(node, ast.Constant):
            if type(node.value) in (int, float):
                assert node.value not in BENCHMARK_NUMBERS or node in approved_shape_numbers, (
                    f"Benchmark numeric literal {node.value} in {path}:{node.lineno}"
                )
            elif isinstance(node.value, str):
                assert not REFERENCE_SOURCE.search(node.value), (
                    f"Absolute source path in {path}:{node.lineno}"
                )
        elif isinstance(node, ast.Name):
            assert node.id not in {"__import__", "eval", "exec"}, (
                f"Dynamic code execution in {path}:{node.lineno}"
            )
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            assert not (node.value.id in sys_aliases and node.attr == "path"), (
                f"Production sys.path access in {path}:{node.lineno}"
            )


def check_package(package):
    assert package.is_dir(), f"Missing package: {package}"
    assert not package.is_symlink(), f"Symlink production package: {package}"
    assert not package.parent.is_symlink(), f"Symlink source directory: {package.parent}"
    assert (package / "__init__.py").is_file()
    for layer in LAYERS:
        assert (package / layer / "__init__.py").is_file(), f"Missing layer: {layer}"
    for path in package.rglob("*"):
        assert not path.is_symlink(), f"Symlink in production tree: {path}"
        if path.name == "__pycache__" or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(package)
        assert relative.parts[0] in LAYERS or relative == Path("__init__.py"), (
            f"Unexpected production boundary: {relative}"
        )
        if path.is_file():
            assert path.suffix == ".py", f"Non-source production payload: {path}"
            check_source(path.read_text(encoding="utf-8"), path, package)


def check_service_source(source, path):
    assert not OLD_SOURCE.search(source), f"Historical source reference: {path}"
    tree = ast.parse(source, filename=str(path))
    module = module_name(path, SERVICE_PACKAGE)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for target in import_targets(node, module, path.name == "__init__.py"):
                top = target.partition(".")[0]
                assert top in sys.stdlib_module_names or top in {
                    "apsgo_scheduler",
                    "apsgo_v7_service",
                    "fastapi",
                    "uvicorn",
                    "yaml",
                }, f"External service dependency: {target} in {path}"
                assert top != "tests", f"Test dependency in service package: {target}"
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert not LOCAL_ABSOLUTE_PATH.search(node.value), (
                f"Absolute local path in {path}:{node.lineno}"
            )
            assert not TEST_SOURCE.search(node.value), f"Test path in {path}:{node.lineno}"


def test_production_tree_is_self_contained():
    check_package(PACKAGE)


def test_service_package_is_plain_python_and_not_a_symlink():
    assert SERVICE_PACKAGE.is_dir() and not SERVICE_PACKAGE.is_symlink()
    assert (SERVICE_PACKAGE / "__init__.py").is_file()
    for path in SERVICE_PACKAGE.rglob("*"):
        assert not path.is_symlink(), f"Symlink in service package: {path}"
        if path.is_file() and "__pycache__" not in path.parts:
            assert path.suffix == ".py", f"Non-source service payload: {path}"
            check_service_source(path.read_text(encoding="utf-8"), path)


def test_tracked_source_contains_only_new_package():
    if not (ROOT / ".git").exists():
        # An archive has no ignored leftovers: every source path is relevant.
        paths = [
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / "src").rglob("*")
            if (path.is_file() or path.is_symlink()) and "__pycache__" not in path.parts
        ]
    else:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--", "src"],
            cwd=ROOT,
            check=True,
            capture_output=True,
        )
        paths = result.stdout.decode("utf-8").strip("\0").split("\0")
    assert all(not path or path.startswith(ALLOWED_SOURCE_PREFIXES) for path in paths), paths


@pytest.mark.parametrize(
    "source",
    [
        "import numpy",
        "import numba",
        "import sqlite3",
        "import apsgo.rules",
        "class ResourceLedger: pass",
        "class OptimizationProblem: pass",
        "class SchedulingSolution: pass",
        "class SchedulingOutcome: pass",
        "import shared_kernel",
        "import kernel_contracts",
        "line = 'GQGA4'",
        "line = 'GQPT'",
        "line = 'XQGA'",
        "count = 531",
        "weight = 29333.91",
        "count = 37",
        "count = 3",
        "import importlib",
        "from importlib import import_module as load",
        "__import__('hidden')",
        "eval('hidden')",
        "exec('hidden')",
        "import sys as runtime; runtime.path.append('/tmp')",
        "from sys import path as paths",
        "source = '/external/reference/solver.py'",
        "source = 'C:\\\\external\\\\solver.py'",
    ],
)
def test_source_guard_rejects_forbidden_examples(source):
    with pytest.raises(AssertionError):
        check_source(source, PACKAGE / "core" / "example.py", PACKAGE)


def test_source_guard_accepts_standard_library():
    check_source(
        "from dataclasses import dataclass\nimport math\nvalue = 2",
        PACKAGE / "core" / "example.py",
        PACKAGE,
    )


@pytest.mark.parametrize("function,statement", [
    ("_critical_recipe", "return stop - start > 3 or other_stop - other_start > 3"),
    ("_scan_critical_families", "sizes = (3, 6)"),
])
def test_approved_search_shapes_do_not_allow_unrelated_benchmark_literals(function, statement):
    path = PACKAGE / "core" / "width_optimization.py"
    source = f"def {function}():\n    {statement}\n"
    check_source(source, path, PACKAGE)
    for changed, target in (
        (source, PACKAGE / "core" / "example.py"),
        (source.replace(function, "unrelated"), path),
        (source + "    benchmark = 3\n", path),
        (source.replace("3", "37"), path),
    ):
        with pytest.raises(AssertionError, match="Benchmark numeric literal"):
            check_source(changed, target, PACKAGE)


@pytest.mark.parametrize("source", ("import numpy as np", "from numba import njit"))
def test_numeric_dependency_exception_is_only_the_private_bridge_module(source):
    check_source(source, PACKAGE / "core" / "_bridge_numeric.py", PACKAGE)
    for relative in (
        "core/example.py", "core/_bridge_numeric_extra.py",
        "api/_bridge_numeric.py", "app/_bridge_numeric.py",
    ):
        with pytest.raises(AssertionError, match="External production dependency"):
            check_source(source, PACKAGE / relative, PACKAGE)


@pytest.mark.parametrize("source", ("import scipy", "import llvmlite", "import sqlite3"))
def test_numeric_dependency_exception_does_not_relax_other_boundaries(source):
    with pytest.raises(AssertionError):
        check_source(source, PACKAGE / "core" / "_bridge_numeric.py", PACKAGE)


def test_search_layout_allows_only_numpy_in_exact_private_module():
    check_source("import numpy as np", PACKAGE / "core" / "_search_numeric.py", PACKAGE)
    for source in ("import numba", "import scipy", "import llvmlite"):
        with pytest.raises(AssertionError):
            check_source(source, PACKAGE / "core" / "_search_numeric.py", PACKAGE)
    with pytest.raises(AssertionError):
        check_source("import numpy", PACKAGE / "core" / "_search_numeric_extra.py", PACKAGE)


def test_delivery_parallel_dependency_exception_is_exact():
    for source in ("import numpy", "import numba"):
        check_source(source, PACKAGE / "core" / "_delivery_parallel.py", PACKAGE)
        with pytest.raises(AssertionError, match="External production dependency"):
            check_source(source, PACKAGE / "core" / "_delivery_parallel_extra.py", PACKAGE)
    with pytest.raises(AssertionError, match="External production dependency"):
        check_source("import scipy", PACKAGE / "core" / "_delivery_parallel.py", PACKAGE)


def test_authoritative_numeric_state_dependency_exception_is_exact():
    check_source("import numpy", PACKAGE / "core" / "_numeric_state.py", PACKAGE)
    for source in ("import numba", "import scipy", "import pandas"):
        with pytest.raises(AssertionError, match="External production dependency"):
            check_source(source, PACKAGE / "core" / "_numeric_state.py", PACKAGE)
    with pytest.raises(AssertionError, match="External production dependency"):
        check_source("import numpy", PACKAGE / "core" / "_numeric_state_extra.py", PACKAGE)


def test_shared_numeric_chain_operations_exception_is_exact():
    path = PACKAGE / "core" / "_numeric_chain_ops.py"
    for source in ("import numpy", "from numba import njit"):
        check_source(source, path, PACKAGE)
    for source in ("import pandas", "import scipy", "import tests", "N = 531"):
        with pytest.raises(AssertionError):
            check_source(source, path, PACKAGE)
    with pytest.raises(AssertionError):
        check_source("import numba", PACKAGE / "core" / "_numeric_chain_ops_extra.py", PACKAGE)


def test_private_numeric_resource_dependencies_remain_exact():
    path = PACKAGE / "core" / "_numeric_resources.py"
    for source in ("import numpy", "from numba import njit, literal_unroll"):
        check_source(source, path, PACKAGE)
    for source in ("import pandas", "import scipy", "import tests", "N = 531"):
        with pytest.raises(AssertionError):
            check_source(source, path, PACKAGE)
    with pytest.raises(AssertionError):
        check_source("import numba", PACKAGE / "core" / "_numeric_resources_extra.py", PACKAGE)


def test_unified_candidate_dependencies_are_exact_and_do_not_call_stages():
    path = PACKAGE / "core" / "_numeric_candidate_kernel.py"
    for source in ("import numpy", "from numba import njit"):
        check_source(source, path, PACKAGE)
    for source in ("import pandas", "import scipy", "import tests", "N = 531"):
        with pytest.raises(AssertionError):
            check_source(source, path, PACKAGE)
    with pytest.raises(AssertionError):
        check_source("import numba", PACKAGE / "core" / "_numeric_candidate_kernel_extra.py", PACKAGE)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not imports & {"_numeric_search", "_numeric_refinement", "_numeric_construction", "_numeric_batch"}


def test_private_separator_three_row_shape_is_exact():
    path = PACKAGE / "core" / "_numeric_resources.py"
    check_source("def scan_private_separator():\n    return np.arange(3, dtype=np.int64)", path, PACKAGE)
    for source in ("N = 3", "def other():\n    return np.arange(3, dtype=np.int64)",
                   "def scan_private_separator():\n    return np.zeros(3, dtype=np.int64)"):
        with pytest.raises(AssertionError, match="Benchmark numeric literal"):
            check_source(source, path, PACKAGE)


def test_numeric_evaluation_dependency_exception_is_exact():
    check_source("import numpy", PACKAGE / "core" / "_numeric_evaluation.py", PACKAGE)
    for source in ("import numba", "import scipy", "import pandas"):
        with pytest.raises(AssertionError, match="External production dependency"):
            check_source(source, PACKAGE / "core" / "_numeric_evaluation.py", PACKAGE)
    with pytest.raises(AssertionError, match="External production dependency"):
        check_source("import numpy", PACKAGE / "core" / "_numeric_evaluation_extra.py", PACKAGE)


def test_numeric_batch_dependency_exception_is_exact():
    check_source("import numpy", PACKAGE / "core" / "_numeric_batch.py", PACKAGE)
    for source in ("import numba", "import scipy", "import pandas", "import tests"):
        with pytest.raises(AssertionError):
            check_source(source, PACKAGE / "core" / "_numeric_batch.py", PACKAGE)
    with pytest.raises(AssertionError):
        check_source("import numpy", PACKAGE / "core" / "_numeric_batch_extra.py", PACKAGE)


def test_complete_numeric_kernel_dependency_and_benchmark_exception_is_exact():
    for source in ("import numpy", "from numba import njit"):
        check_source(source, PACKAGE / "core" / "_numeric_kernel.py", PACKAGE)
    for source in ("import scipy", "import pandas", "import tests",
                   "benchmark = 531", "benchmark = 37"):
        with pytest.raises(AssertionError):
            check_source(source, PACKAGE / "core" / "_numeric_kernel.py", PACKAGE)
    with pytest.raises(AssertionError):
        check_source("import numba", PACKAGE / "core" / "_numeric_kernel_extra.py", PACKAGE)


def test_numeric_construction_dependency_exception_is_exact():
    check_source("import numpy", PACKAGE / "core" / "_numeric_construction.py", PACKAGE)
    check_source("from numba import njit", PACKAGE / "core" / "_numeric_construction.py", PACKAGE)
    for source in ("import scipy", "import pandas"):
        with pytest.raises(AssertionError, match="External production dependency"):
            check_source(source, PACKAGE / "core" / "_numeric_construction.py", PACKAGE)
    with pytest.raises(AssertionError, match="External production dependency"):
        check_source("import numpy", PACKAGE / "core" / "_numeric_construction_extra.py", PACKAGE)
    with pytest.raises(AssertionError, match="Benchmark numeric literal"):
        check_source("N = 3", PACKAGE / "core" / "_numeric_construction.py", PACKAGE)


def test_candidate_recipe_arity_is_not_a_benchmark_constant():
    source = 'class CandidateEdit:\n    def bind(self):\n        return guard != "width_optimization" or len(indices) != 3\n'
    path = PACKAGE / "core" / "_candidate_edit.py"
    check_source(source, path, PACKAGE)
    for changed in (source.replace("bind", "unrelated"), source + "benchmark = 3\n",
                    source.replace("3", "37")):
        with pytest.raises(AssertionError, match="Benchmark numeric literal"):
            check_source(changed, path, PACKAGE)


@pytest.mark.parametrize(
    "source",
    (
        "from tests.app import helper",
        "source = 'tests/baseline.json'",
        "source = '/Users/example/private.json'",
        "source = 'C:\\\\private\\\\config.json'",
        "import numpy",
        "import numba",
    ),
)
def test_service_source_guard_rejects_test_local_and_external_dependencies(source):
    with pytest.raises(AssertionError):
        check_service_source(source, SERVICE_PACKAGE / "example.py")


def test_service_source_guard_accepts_declared_runtime_dependencies():
    check_service_source(
        "import sqlite3\nimport yaml\n"
        "from apsgo_scheduler.api.request import RuleSetSpec\nline = 'GQGA4'",
        SERVICE_PACKAGE / "example.py",
    )


def test_production_symlink_is_rejected(tmp_path):
    link = tmp_path / "apsgo_scheduler"
    link.symlink_to(PACKAGE, target_is_directory=True)
    with pytest.raises(AssertionError, match="Symlink"):
        check_package(link)


def test_source_directory_symlink_is_rejected(tmp_path):
    link = tmp_path / "src"
    link.symlink_to(PACKAGE.parent, target_is_directory=True)
    with pytest.raises(AssertionError, match="Symlink source directory"):
        check_package(link / PACKAGE.name)
