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
    sys_aliases = {"sys"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            targets = import_targets(node, module, path.name == "__init__.py")
            for target in targets:
                top = target.partition(".")[0]
                assert top in sys.stdlib_module_names or top == "apsgo_scheduler", (
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
                assert node.value not in BENCHMARK_NUMBERS, (
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


@pytest.mark.parametrize(
    "source",
    (
        "from tests.app import helper",
        "source = 'tests/baseline.json'",
        "source = '/Users/example/private.json'",
        "source = 'C:\\\\private\\\\config.json'",
        "import numpy",
    ),
)
def test_service_source_guard_rejects_test_local_and_external_dependencies(source):
    with pytest.raises(AssertionError):
        check_service_source(source, SERVICE_PACKAGE / "example.py")


def test_service_source_guard_accepts_line_template_and_scheduler_dependency():
    check_service_source(
        "import sqlite3\nfrom apsgo_scheduler.api.request import RuleSetSpec\nline = 'GQGA4'",
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
