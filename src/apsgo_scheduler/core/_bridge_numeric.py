"""Private, task-bound numerical inputs for the virtual-only bridge search."""

from dataclasses import dataclass, fields
from decimal import Decimal
from math import isfinite

import numpy as np
from numba import njit

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
_WIDTH_COLUMN, _THICKNESS_COLUMN, _MIN_TEMP_COLUMN, _MAX_TEMP_COLUMN = range(len(_PHYSICAL_FIELDS))
_LOWER, _UPPER, _TOLERANCE = range(len(_BAND_FIELDS))
_HAS_LOWER, _HAS_UPPER, _INCLUDE_LOWER, _INCLUDE_UPPER, _RELATIVE = range(len(_BAND_FLAG_NAMES))
_SINGLE_MODE, _DOUBLE_MODE = range(2)
_ALLOWED, _DENIED, _UNSAFE = 1, 0, -1


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


@njit(cache=False, fastmath=False, parallel=False, boundscheck=True)
def _edge(left, right, values, missing, rule_kinds, parameters, switches, bands, band_flags):
    if not (0 <= left < len(values) and 0 <= right < len(values)):
        raise ValueError("virtual bridge edge row is outside its input")
    if left < _ANCHOR_COUNT and right < _ANCHOR_COUNT:
        raise ValueError("numeric bridge edges require a virtual prototype endpoint")
    denied = False
    for kind in rule_kinds:
        if kind == _WIDTH_RULE:
            if missing[left, _WIDTH_COLUMN] or missing[right, _WIDTH_COLUMN]:
                denied = True
                continue
            delta = abs(values[right, _WIDTH_COLUMN] - values[left, _WIDTH_COLUMN])
            limit = parameters[_WIDTH_LIMIT]
            if delta <= limit + 1e-9:
                continue
            severity = max(1.0, (delta - limit) / max(limit, 1.0))
            if not isfinite(delta) or not isfinite(severity):
                return _UNSAFE
            denied = True
        elif kind == _THICKNESS_RULE:
            if missing[left, _THICKNESS_COLUMN] or missing[right, _THICKNESS_COLUMN]:
                continue
            first, second = values[left, _THICKNESS_COLUMN], values[right, _THICKNESS_COLUMN]
            basis = max(first, second) if switches[_THICKER] else min(first, second)
            tolerance = parameters[_THICKNESS_FALLBACK]
            for index in range(len(bands)):
                lower_ok = not band_flags[index, _HAS_LOWER] or (
                    basis >= bands[index, _LOWER]
                    if band_flags[index, _INCLUDE_LOWER] else basis > bands[index, _LOWER]
                )
                upper_ok = not band_flags[index, _HAS_UPPER] or (
                    basis <= bands[index, _UPPER]
                    if band_flags[index, _INCLUDE_UPPER] else basis < bands[index, _UPPER]
                )
                if lower_ok and upper_ok:
                    tolerance = bands[index, _TOLERANCE]
                    if band_flags[index, _RELATIVE]:
                        tolerance *= basis
                    break
            difference = abs(first - second)
            if not isfinite(tolerance) or not isfinite(difference):
                return _UNSAFE
            if difference <= tolerance + 1e-9:
                continue
            severity = max(1.0, (difference - tolerance) / max(tolerance, 1e-9))
            if not isfinite(severity):
                return _UNSAFE
            denied = True
        elif kind == _TEMPERATURE_RULE:
            if switches[_IGNORE_TEMPERATURE] or switches[_ADAPTIVE_TEMPERATURE]:
                continue
            if (
                missing[left, _MIN_TEMP_COLUMN] or missing[left, _MAX_TEMP_COLUMN]
                or missing[right, _MIN_TEMP_COLUMN] or missing[right, _MAX_TEMP_COLUMN]
            ):
                continue
            overlap = min(values[left, _MAX_TEMP_COLUMN], values[right, _MAX_TEMP_COLUMN]) - max(
                values[left, _MIN_TEMP_COLUMN], values[right, _MIN_TEMP_COLUMN]
            )
            if not isfinite(overlap):
                return _UNSAFE
            minimum = parameters[_TEMPERATURE_MINIMUM]
            if overlap + 1e-9 >= minimum:
                continue
            severity = max(1.0, (minimum - overlap) / max(minimum, 1e-9))
            if not isfinite(severity):
                return _UNSAFE
            denied = True
        elif kind == _SOFT_HARD_RULE:
            if not switches[_VIRTUAL_SOFT_BRIDGE]:
                denied = True
        else:
            raise ValueError("unknown numeric bridge rule kind")
    # A prohibited rule must not hide an unsafe calculation in a later rule of the same edge.
    return _DENIED if denied else _ALLOWED


@njit(cache=False, fastmath=False, parallel=False, boundscheck=True)
def _score(left, middle, right, values, missing):
    if not (0 <= left < len(values) and 0 <= middle < len(values) and 0 <= right < len(values)):
        raise ValueError("virtual bridge score row is outside its input")
    width_cost, thickness_cost = 0.0, 0.0
    for column in (_WIDTH_COLUMN, _THICKNESS_COLUMN):
        first = 0.0 if missing[left, column] else values[left, column] or 0.0
        center = 0.0 if missing[middle, column] else values[middle, column] or 0.0
        last = 0.0 if missing[right, column] else values[right, column] or 0.0
        cost = abs(first - center) + abs(center - last)
        if not isfinite(cost):
            return False, 0.0
        if column == _WIDTH_COLUMN:
            width_cost = cost
        else:
            thickness_cost = cost
    score = width_cost + 100.0 * thickness_cost
    return isfinite(score), score


@njit(cache=False, fastmath=False, parallel=False, boundscheck=True)
def _scan_block(
    mode, first_index, start, stop, values, missing, rule_kinds, parameters, switches,
    bands, band_flags, best_i, best_j, best_score,
):
    count = len(values) - _ANCHOR_COUNT
    if mode != _SINGLE_MODE and mode != _DOUBLE_MODE:
        raise ValueError("unknown numeric bridge scan mode")
    if not (0 <= start <= stop <= count) or stop - start > _BLOCK_SIZE:
        raise ValueError("numeric bridge block is outside its input or exceeds the block limit")
    if not (-1 <= best_i < count and -1 <= best_j < count):
        raise ValueError("numeric bridge best index is outside its input")
    first_row = _LEFT_ROW
    if mode == _SINGLE_MODE:
        if first_index != -1 or best_j != -1:
            raise ValueError("single bridge scan cannot have a second position")
    else:
        if not 0 <= first_index < count:
            raise ValueError("double bridge first index is outside its input")
        first_row = first_index + _ANCHOR_COUNT
        edge = _edge(
            _LEFT_ROW, first_row, values, missing, rule_kinds, parameters, switches, bands, band_flags,
        )
        if edge != _ALLOWED:
            return edge != _UNSAFE, False, best_i, best_j, best_score
    for index in range(start, stop):
        current_row = index + _ANCHOR_COUNT
        edge = _edge(
            first_row, current_row, values, missing, rule_kinds, parameters, switches, bands,
            band_flags,
        )
        if edge == _UNSAFE:
            return False, True, best_i, best_j, best_score
        if edge == _DENIED:
            continue
        edge = _edge(
            current_row, _RIGHT_ROW, values, missing, rule_kinds, parameters, switches, bands,
            band_flags,
        )
        if edge == _UNSAFE:
            return False, True, best_i, best_j, best_score
        if edge == _DENIED:
            continue
        if mode == _SINGLE_MODE:
            safe, score = _score(_LEFT_ROW, current_row, _RIGHT_ROW, values, missing)
            selected_i, selected_j = index, -1
        else:
            safe, head = _score(_LEFT_ROW, first_row, current_row, values, missing)
            if not safe:
                return False, True, best_i, best_j, best_score
            safe, tail = _score(first_row, current_row, _RIGHT_ROW, values, missing)
            score = head + tail
            safe = safe and isfinite(score)
            selected_i, selected_j = first_index, index
        if not safe:
            return False, True, best_i, best_j, best_score
        if best_i < 0 or score < best_score:
            best_i, best_j, best_score = selected_i, selected_j, score
    return True, True, best_i, best_j, best_score


def _require_array(value, dtype, shape):
    if (
        type(value) is not np.ndarray or value.dtype != dtype or value.shape != shape
        or not value.flags.c_contiguous or value.flags.writeable
    ):
        raise ValueError("numeric bridge arrays require the expected readonly contiguous layout")


def _validate_scan_inputs(catalog, values, missing):
    if type(catalog) is not _NumericCatalog:
        raise ValueError("numeric bridge scan requires its prepared catalog")
    count = len(catalog.problem.virtual_prototypes)
    _require_array(catalog.geometry, np.float64, (count, len(_GEOMETRY_FIELDS)))
    _require_array(catalog.missing, np.bool_, (count, len(_GEOMETRY_FIELDS)))
    _require_array(catalog.rule_kinds, np.int64, (len(catalog.rule_kinds),))
    if len(catalog.rule_kinds) > len(_RULE_TYPES):
        raise ValueError("numeric bridge catalog has too many edge rules")
    _require_array(catalog.parameters, np.float64, (len(_PARAMETER_NAMES),))
    _require_array(catalog.switches, np.bool_, (len(_SWITCH_NAMES),))
    _require_array(catalog.bands, np.float64, (len(catalog.bands), len(_BAND_FIELDS)))
    _require_array(catalog.band_flags, np.bool_, (len(catalog.bands), len(_BAND_FLAG_NAMES)))
    _require_array(values, np.float64, (count + _ANCHOR_COUNT, len(_PHYSICAL_FIELDS)))
    _require_array(missing, np.bool_, values.shape)
    return count


def _scan(mode, catalog, values, missing, budget):
    if not budget.allows_search():
        return False, None
    count = _validate_scan_inputs(catalog, values, missing)
    best_i, best_j, best_score = -1, -1, float("inf")
    first_indices = (-1,) if mode == _SINGLE_MODE else range(count)
    for first_index in first_indices:
        if not budget.allows_search():
            return False, None
        for start in range(0, count, _BLOCK_SIZE):
            if not budget.allows_search():
                return False, None
            safe, row_open, best_i, best_j, best_score = _scan_block(
                mode, first_index, start, min(start + _BLOCK_SIZE, count), values, missing,
                catalog.rule_kinds, catalog.parameters, catalog.switches, catalog.bands,
                catalog.band_flags, best_i, best_j, best_score,
            )
            if not budget.allows_search() or not safe:
                return False, None
            if not row_open:
                break
    if not budget.allows_search():
        return False, None
    if best_i < 0:
        return True, None
    return True, (best_i,) if mode == _SINGLE_MODE else (best_i, best_j)


def scan_single(catalog, values, missing, budget):
    """Complete the ordered single pass; never return a partial best after stopping."""
    return _scan(_SINGLE_MODE, catalog, values, missing, budget)


def scan_double(catalog, values, missing, budget):
    """Scan fixed first-prototype rows, preserving the original nested order and ties."""
    return _scan(_DOUBLE_MODE, catalog, values, missing, budget)
