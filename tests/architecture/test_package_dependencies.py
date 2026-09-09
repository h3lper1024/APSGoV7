"""Only app -> api/core and api -> core may cross package boundaries."""

import ast

import pytest

from .test_clean_room_boundaries import PACKAGE, import_targets, module_name

ALLOWED = {"core": {"core"}, "api": {"api", "core"}, "app": {"app", "api", "core"}}


def check_dependencies(source, module, is_package=False):
    parts = module.split(".")
    if len(parts) == 1:
        assert all(
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            for node in ast.parse(source).body
        ), "Root package may only contain documentation"
        return
    layer = parts[1]
    aliases = {}
    tree = ast.parse(source)
    targets = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            targets.extend(import_targets(node, module, is_package))
            if isinstance(node, ast.Import):
                for alias in node.names:
                    aliases[alias.asname or alias.name.split(".")[0]] = (
                        alias.name if alias.asname else alias.name.split(".")[0]
                    )
    # Covers `import apsgo_scheduler as root; root.app.service`, too.
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            names = []
            while isinstance(node, ast.Attribute):
                names.append(node.attr)
                node = node.value
            if isinstance(node, ast.Name) and node.id in aliases:
                targets.append(".".join([aliases[node.id], *reversed(names)]))
    for target in targets:
        pieces = target.split(".")
        assert pieces[0] != "apsgo_v7_service", f"Forbidden dependency: {module} -> {target}"
        if pieces[0] == "apsgo_scheduler" and len(pieces) > 1:
            assert pieces[1] in ALLOWED[layer], f"Forbidden dependency: {module} -> {target}"


def test_package_import_directions():
    for path in PACKAGE.rglob("*.py"):
        check_dependencies(
            path.read_text(encoding="utf-8"), module_name(path, PACKAGE), path.name == "__init__.py"
        )


@pytest.mark.parametrize(
    "module,source,is_package",
    [
        ("apsgo_scheduler.core.model", "import apsgo_scheduler.api", False),
        ("apsgo_scheduler.core.model", "from apsgo_scheduler.app import service", False),
        ("apsgo_scheduler.core.model", "from apsgo_scheduler import api", False),
        ("apsgo_scheduler.core.model", "from .. import api", False),
        ("apsgo_scheduler.core.model", "from ..api import request", False),
        ("apsgo_scheduler.core.rules.base", "from ...api import request", False),
        ("apsgo_scheduler.core", "from ..api import request", True),
        ("apsgo_scheduler.api.request", "from .. import app", False),
        ("apsgo_scheduler.api.request", "import apsgo_scheduler.app.service as service", False),
        ("apsgo_scheduler.core.model", "import apsgo_scheduler as root; root.app.service()", False),
        ("apsgo_scheduler.app.service", "import apsgo_v7_service", False),
    ],
)
def test_dependency_guard_rejects_reversed_imports(module, source, is_package):
    with pytest.raises(AssertionError, match="Forbidden dependency"):
        check_dependencies(source, module, is_package)


@pytest.mark.parametrize(
    "module,source,is_package",
    [
        ("apsgo_scheduler", "", True),
        ("apsgo_scheduler.core.model", "from . import contracts", False),
        ("apsgo_scheduler.core.rules.base", "from ..model import Node", False),
        ("apsgo_scheduler.api.request", "from ..core import contracts", False),
        ("apsgo_scheduler.app.service", "from ..api import request", False),
        ("apsgo_scheduler.app.service", "import apsgo_scheduler.core.solver", False),
    ],
)
def test_dependency_guard_accepts_documented_directions(module, source, is_package):
    check_dependencies(source, module, is_package)
