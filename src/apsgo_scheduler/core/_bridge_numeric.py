"""Private, task-bound numerical inputs for the virtual-only bridge search."""

from dataclasses import dataclass, fields
from decimal import Decimal

import numpy as np

from .compatibility import RuleEdgeDecisionCache, _finite_projection
from .contracts import RuleScope
from .model import MaterialRole, Node, SchedulingProblem, VirtualMaterialPrototype
from .rules.base import RuleEvaluationContext
from .rules.concrete import (
    SoftHardConnectionRule,
    TemperatureOverlapRule,
    ThicknessTransitionRule,
    WidthTransitionRule,
)
from .rules.rule_set import ProcessRuleSet

_BLOCK_SIZE = 64
_GEOMETRY_FIELDS = ("width", "thickness")
_PHYSICAL_FIELDS = (*_GEOMETRY_FIELDS, "min_temperature", "max_temperature")
_NODE_FIELDS = frozenset(item.name for item in fields(Node))
_RULE_TYPES = (
    WidthTransitionRule, ThicknessTransitionRule, TemperatureOverlapRule, SoftHardConnectionRule,
)
_WIDTH_RULE, _THICKNESS_RULE, _TEMPERATURE_RULE, _SOFT_HARD_RULE = range(len(_RULE_TYPES))
_PARAMETER_NAMES = ("virtual_width_limit", "thickness_fallback", "temperature_minimum")
_WIDTH_LIMIT, _THICKNESS_FALLBACK, _TEMPERATURE_MINIMUM = range(len(_PARAMETER_NAMES))
_SWITCH_NAMES = ("thicker", "ignore_temperature", "adaptive_temperature", "virtual_soft_bridge")
_THICKER, _IGNORE_TEMPERATURE, _ADAPTIVE_TEMPERATURE, _VIRTUAL_SOFT_BRIDGE = range(
    len(_SWITCH_NAMES)
)
_BAND_FIELDS = ("min", "max", "tolerance")
_BAND_FLAG_NAMES = ("has_min", "has_max", "include_min", "include_max", "relative")
_LEFT_ROW, _RIGHT_ROW = range(2)
_ANCHOR_COUNT = 2


@dataclass(frozen=True, slots=True, eq=False)
class _NumericCatalog:
    geometry: np.ndarray
    missing: np.ndarray
    rule_kinds: np.ndarray
    parameters: np.ndarray
    switches: np.ndarray
    bands: np.ndarray
    band_flags: np.ndarray
    problem: SchedulingProblem
    rule_set: ProcessRuleSet
    context: RuleEvaluationContext

    def matches(self, cache: RuleEdgeDecisionCache) -> bool:
        return (
            type(cache) is RuleEdgeDecisionCache
            and self.problem is cache.problem
            and self.rule_set is cache.rule_set
            and self.context is cache.context
        )


def _empty(shape, dtype, budget):
    if not budget.allows_search():
        return None
    result = np.empty(shape, dtype=dtype)
    return result if budget.allows_search() else None


def _project_row(values, missing, raw_values) -> bool:
    for column, value in enumerate(raw_values):
        absent = value is None
        if not absent and type(value) is not Decimal:
            return False
        try:
            projected = _finite_projection(value, "virtual bridge numeric input")
        except ValueError:
            # The original traversal, not preparation, decides if this error is reached.
            return False
        values[column] = 0.0 if absent else projected
        if missing is not None:
            missing[column] = absent
    return True


def _readonly(*arrays):
    for array in arrays:
        array.setflags(write=False)


def prepare_catalog(factory) -> _NumericCatalog | None:
    """Return unsupported/stop without materializing or evaluating any prototype."""
    from .virtual_material import VirtualFactory

    if type(factory) is not VirtualFactory or type(factory.cache) is not RuleEdgeDecisionCache:
        return None
    cache, budget = factory.cache, factory.budget
    if type(cache.rule_set) is not ProcessRuleSet or not budget.allows_search():
        return None
    rules = cache.rule_set.rules_for_scope(RuleScope.EDGE)
    kinds = []
    for rule in rules:
        if type(rule) not in _RULE_TYPES:
            return None
        kind = _RULE_TYPES.index(type(rule))
        if kind in kinds:
            return None
        kinds.append(kind)

    thickness = next((rule for rule in rules if type(rule) is ThicknessTransitionRule), None)
    ranges = () if thickness is None else thickness.parameters["ranges"]
    prototypes = cache.problem.virtual_prototypes
    geometry = _empty((len(prototypes), len(_GEOMETRY_FIELDS)), np.float64, budget)
    missing = _empty((len(prototypes), len(_GEOMETRY_FIELDS)), np.bool_, budget)
    rule_kinds = _empty(len(kinds), np.int64, budget)
    parameters = _empty(len(_PARAMETER_NAMES), np.float64, budget)
    switches = _empty(len(_SWITCH_NAMES), np.bool_, budget)
    bands = _empty((len(ranges), len(_BAND_FIELDS)), np.float64, budget)
    band_flags = _empty((len(ranges), len(_BAND_FLAG_NAMES)), np.bool_, budget)
    arrays = (geometry, missing, rule_kinds, parameters, switches, bands, band_flags)
    if any(array is None for array in arrays):
        return None
    rule_kinds[:] = kinds
    switches.fill(False)
    parameter_values = [Decimal(0)] * len(_PARAMETER_NAMES)
    for rule, kind in zip(rules, kinds):
        config = rule.parameters
        if kind == _WIDTH_RULE:
            parameter_values[_WIDTH_LIMIT] = config["virtual_width_tolerance"]
        elif kind == _THICKNESS_RULE:
            parameter_values[_THICKNESS_FALLBACK] = config["fallback_tolerance"]
            switches[_THICKER] = config["basis"].strip().lower() == "thicker"
        elif kind == _TEMPERATURE_RULE:
            parameter_values[_TEMPERATURE_MINIMUM] = config["min_overlap"]
            switches[_IGNORE_TEMPERATURE] = config["ignore_temperature"]
            switches[_ADAPTIVE_TEMPERATURE] = config["virtual_temperature_adaptive"]
        else:
            # Every edge in this numeric search has at least one generated virtual endpoint.
            switches[_VIRTUAL_SOFT_BRIDGE] = config["virtual_sphc_allows_bridge"]
    if not _project_row(parameters, None, parameter_values) or not budget.allows_search():
        return None

    for start in range(0, len(prototypes), _BLOCK_SIZE):
        if not budget.allows_search():
            return None
        for index in range(start, min(start + _BLOCK_SIZE, len(prototypes))):
            prototype = prototypes[index]
            if type(prototype) is not VirtualMaterialPrototype or any(
                name in prototype.rule_attributes for name in _NODE_FIELDS
            ):
                return None
            # Node validates these even when the corresponding edge rule is disabled.
            if not _project_row(
                geometry[index], missing[index],
                (getattr(prototype, name) for name in _GEOMETRY_FIELDS),
            ):
                return None
        if not budget.allows_search():
            return None
    for start in range(0, len(ranges), _BLOCK_SIZE):
        if not budget.allows_search():
            return None
        for index in range(start, min(start + _BLOCK_SIZE, len(ranges))):
            band = ranges[index]
            if not _project_row(bands[index], None, (band[name] for name in _BAND_FIELDS)):
                return None
            band_flags[index] = (
                band["min"] is not None, band["max"] is not None,
                band["include_min"], band["include_max"],
                band["calculation_mode"].strip().lower() == "relative",
            )
        if not budget.allows_search():
            return None
    _readonly(*arrays)
    if not budget.allows_search():
        return None
    return _NumericCatalog(*arrays, cache.problem, cache.rule_set, cache.context)


def prepare_bridge(catalog: _NumericCatalog, left: Node, right: Node, budget):
    """Rows are left, right, then prototypes; temperatures use the original anchors."""
    if type(catalog) is not _NumericCatalog or any(type(node) is not Node for node in (left, right)):
        return None
    if not budget.allows_search():
        return None
    for node in (left, right):
        if node.material_role is MaterialRole.GENERATED_VIRTUAL:
            if node.virtual_lineage.prototype_id not in catalog.context.virtual_prototype_ids:
                return None
        elif node.source_period not in catalog.context.period_index:
            return None
    # Keep Decimal comparisons before float conversion: rounded endpoints can conceal reversal.
    lower = tuple(node.min_temperature for node in (left, right) if node.min_temperature is not None)
    upper = tuple(node.max_temperature for node in (left, right) if node.max_temperature is not None)
    if any(type(value) is not Decimal for value in (*lower, *upper)):
        return None
    minimum, maximum = min(lower) if lower else None, max(upper) if upper else None
    if minimum is not None and maximum is not None and minimum > maximum:
        return None
    count = len(catalog.problem.virtual_prototypes)
    shape = (count + _ANCHOR_COUNT, len(_PHYSICAL_FIELDS))
    values = _empty(shape, np.float64, budget)
    missing = _empty(shape, np.bool_, budget)
    if values is None or missing is None:
        return None
    for index, node in enumerate((left, right)):
        if not _project_row(
            values[index], missing[index], (getattr(node, name) for name in _PHYSICAL_FIELDS),
        ):
            return None
    temperature_values = _empty(len(_GEOMETRY_FIELDS), np.float64, budget)
    temperature_missing = _empty(len(_GEOMETRY_FIELDS), np.bool_, budget)
    if temperature_values is None or temperature_missing is None:
        return None
    if not _project_row(temperature_values, temperature_missing, (minimum, maximum)):
        return None
    for start in range(0, count, _BLOCK_SIZE):
        if not budget.allows_search():
            return None
        stop = min(start + _BLOCK_SIZE, count)
        rows = slice(start + _ANCHOR_COUNT, stop + _ANCHOR_COUNT)
        values[rows, :len(_GEOMETRY_FIELDS)] = catalog.geometry[start:stop]
        missing[rows, :len(_GEOMETRY_FIELDS)] = catalog.missing[start:stop]
        values[rows, len(_GEOMETRY_FIELDS):] = temperature_values
        missing[rows, len(_GEOMETRY_FIELDS):] = temperature_missing
        if not budget.allows_search():
            return None
    _readonly(values, missing)
    return (values, missing) if budget.allows_search() else None
