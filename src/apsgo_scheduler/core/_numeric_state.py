"""Authoritative numeric input and plan layouts, prepared at task boundaries.

Text and domain carriers are accepted only by builders. The resulting numeric
columns contain no Nodes, Decimals, dictionaries or object-dtype arrays.
"""

from dataclasses import dataclass, fields, replace
from dataclasses import field as dataclass_field
from hashlib import sha256
from collections import namedtuple
from enum import Enum

import numpy as np

from ._numeric_units import (
    INT64_MAX,
    NumericUnits,
    NumericValueError,
    checked_product,
    checked_sum,
    choose_scale,
    due_milliseconds,
    hours_to_milliseconds,
    int64,
    start_milliseconds,
    to_ticks,
)
from .contracts import fingerprint
from .delivery_timing import DeliveryTimingInput
from .model import MaterialRole, SchedulingProblem
from .rules.rule_set import ProcessRuleSet

_PHYSICAL = ("width", "thickness", "min_temperature", "max_temperature")
_TEXT = ("grade", "hot_roll_grade", "soft_hard_class", "grade_class", "surface_grade")
_WIDTH_PARAMETERS = (
    "maximum_increase",
    "max_reverse_width",
    "virtual_width_tolerance",
    "width_upper_exclusive",
)
_WEIGHT_PARAMETERS = (
    "max_weight",
    "min_weight",
    "target_weight",
    "max_real_weight",
    "future_fill_weight_target",
    "maximum_piece_weight",
    "minimum_piece_weight",
    "maximum_separator_weight",
)
_ROLES = tuple(MaterialRole)

# Shared native status codes; the existing evaluation kernel uses these too.
OK, INVALID, NUMERIC_ERROR, CANCELLED, CAPACITY, STALE = range(6)


class NumericSearchAction(str, Enum):
    WHOLE_CHAIN_APPEND = "whole_chain_append"
    WHOLE_CHAIN_PREPEND = "whole_chain_prepend"
    WHOLE_CHAIN_INSERTION = "whole_chain_insertion"
    REAL_NODE_RELOCATION = "real_node_relocation"
    CHAIN_ORDER_RELOCATION = "chain_order_relocation"
    VIRTUAL_WEIGHT_FILL = "virtual_weight_fill"
    CONTROLLED_ORDER_SPLIT = "controlled_order_split"
    DELIVERY_INTRA_MOVE = "delivery_intra_move"
    NODE_MOVE = "width_node_move"
    NODE_EXCHANGE = "width_node_exchange"
    BLOCK_MOVE = "width_block_move"
    BLOCK_EXCHANGE = "width_block_exchange"
    CHAIN_CUT = "width_chain_cut"
    BRIDGE_RECLAMATION = "width_bridge_reclamation"


def readonly(values, dtype):
    value = np.array(values, dtype=dtype, copy=True)
    value.setflags(write=False)
    return value


def _indices(values, path):
    value = np.asarray(values)
    if value.ndim != 1 or value.dtype.kind not in ("i", "u"):
        raise NumericValueError(path, "one-dimensional integer indices required")
    if value.dtype.kind == "u" and value.size and int(value.max()) > INT64_MAX:
        raise NumericValueError(path, "unsigned index exceeds int64")
    return readonly(value, np.int64)


def split_target_periods_match(task, chains, chain_periods):
    """Return whether every scheduled split piece remains in its authorized period."""
    if not isinstance(task, NumericTask) or len(chains) != len(chain_periods):
        return False
    group_count = task.split_groups.target_period.size
    for chain, period in zip(chains, chain_periods):
        groups = task.nodes.split_group[np.asarray(chain, dtype=np.int64)]
        groups = groups[groups >= 0]
        if groups.size and (
            np.any(groups >= group_count)
            or np.any(task.split_groups.target_period[groups] != int(period))
        ):
            return False
    return True


def _text(material, name):
    value = material.grade if name == "grade" else material.rule_attributes.get(name)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise NumericValueError(name, "text category required")
    return (
        value.strip().upper()
        if name in ("grade", "hot_roll_grade", "surface_grade")
        else value.strip()
    )


def _units(materials, rules):
    values = {name: [getattr(node, name) for node in materials] for name in _PHYSICAL}
    weights = [getattr(node, "weight", getattr(node, "unit_weight", None)) for node in materials]
    for rule in rules:
        parameters = rule.parameters
        values["width"].extend(parameters[key] for key in _WIDTH_PARAMETERS if key in parameters)
        weights.extend(parameters[key] for key in _WEIGHT_PARAMETERS if key in parameters)
        if type(rule).__name__ == "TemperatureOverlapRule":
            values["min_temperature"].append(parameters["min_overlap"])
        if type(rule).__name__ == "ThicknessTransitionRule":
            values["thickness"].append(parameters["fallback_tolerance"])
            for interval in parameters["ranges"]:
                values["thickness"].extend((interval["min"], interval["max"]))
                if interval["calculation_mode"] == "absolute":
                    values["thickness"].append(interval["tolerance"])
    return NumericUnits(
        choose_scale(values["width"], "width"),
        choose_scale(values["thickness"], "thickness"),
        choose_scale(values["min_temperature"] + values["max_temperature"], "temperature"),
        choose_scale(weights, "weight", minimum_scale=100),
    )


@dataclass(frozen=True, slots=True, eq=False)
class NumericNodeColumns:
    width: np.ndarray
    thickness: np.ndarray
    min_temperature: np.ndarray
    max_temperature: np.ndarray
    present: np.ndarray
    weight: np.ndarray
    duration_ms: np.ndarray
    role: np.ndarray
    source: np.ndarray
    resource: np.ndarray
    source_period: np.ndarray
    prototype: np.ndarray
    purpose: np.ndarray
    split_group: np.ndarray
    piece_index: np.ndarray
    piece_count: np.ndarray
    accepted_sequence: np.ndarray
    grade: np.ndarray
    hot_roll_grade: np.ndarray
    soft_hard_class: np.ndarray
    grade_class: np.ndarray
    surface_grade: np.ndarray

    def __post_init__(self):
        count = self.weight.size
        for field in fields(self):
            value = getattr(self, field.name)
            shape = (count, len(_PHYSICAL)) if field.name == "present" else (count,)
            dtype = (
                np.bool_
                if field.name == "present"
                else np.int8
                if field.name == "role"
                else np.int64
            )
            if not isinstance(value, np.ndarray) or value.shape != shape or value.dtype != dtype:
                raise NumericValueError(field.name, "numeric column shape or dtype mismatch")
            if value.flags.writeable or not value.flags.c_contiguous:
                raise NumericValueError(field.name, "columns must be contiguous and read-only")
        if np.any(self.weight <= 0) or np.any(self.duration_ms < 0):
            raise NumericValueError("nodes", "invalid weight or duration")
        if np.any((self.role < 0) | (self.role >= len(_ROLES))):
            raise NumericValueError("role", "unknown material role")


@dataclass(frozen=True, slots=True, eq=False)
class NumericOriginals:
    weight: np.ndarray
    duration_ms: np.ndarray
    due_ms: np.ndarray
    old_backlog: np.ndarray


@dataclass(frozen=True, slots=True, eq=False)
class NumericSplitGroups:
    parent_row: np.ndarray
    source: np.ndarray
    resource: np.ndarray
    parent_weight: np.ndarray
    parent_duration_ms: np.ndarray
    source_period: np.ndarray
    origin_period: np.ndarray
    target_period: np.ndarray
    mode: np.ndarray
    rule_index: np.ndarray
    accepted_sequence: np.ndarray

    def __post_init__(self):
        size = self.parent_row.size
        for field in fields(self):
            value = getattr(self, field.name)
            if (
                not isinstance(value, np.ndarray)
                or value.dtype != np.int64
                or value.shape != (size,)
                or value.flags.writeable
                or not value.flags.c_contiguous
            ):
                raise NumericValueError(
                    f"split_groups.{field.name}", "read-only integer column required"
                )

    @classmethod
    def empty(cls):
        return cls(*(readonly([], np.int64) for _ in fields(cls)))


@dataclass(frozen=True, slots=True)
class NumericDynamicNode:
    """One candidate-private row copied from an original or prototype template."""

    node_id: str
    template_row: int
    weight: int
    duration_ms: int
    role: int
    source: int
    resource: int
    source_period: int
    prototype: int
    purpose: int
    split_group: int
    piece_index: int
    piece_count: int
    accepted_sequence: int
    min_temperature: int
    max_temperature: int
    min_temperature_present: bool
    max_temperature_present: bool

    def __post_init__(self):
        if not isinstance(self.node_id, str) or not self.node_id:
            raise NumericValueError("dynamic_node.node_id", "nonempty identity required")
        for field in fields(self):
            if field.name in ("node_id", "min_temperature_present", "max_temperature_present"):
                continue
            int64(getattr(self, field.name), f"dynamic_node.{field.name}")
        if (
            type(self.min_temperature_present) is not bool
            or type(self.max_temperature_present) is not bool
        ):
            raise NumericValueError("dynamic_node.temperature", "boolean presence flags required")
        if self.template_row < 0 or self.weight <= 0 or self.duration_ms < 0:
            raise NumericValueError(
                "dynamic_node", "valid template, positive weight and nonnegative duration required"
            )


@dataclass(frozen=True, slots=True)
class NumericSplitGroup:
    parent_row: int
    source: int
    resource: int
    parent_weight: int
    parent_duration_ms: int
    source_period: int
    origin_period: int
    target_period: int
    mode: int
    rule_index: int
    accepted_sequence: int

    def __post_init__(self):
        for field in fields(self):
            int64(getattr(self, field.name), f"split_group.{field.name}")
        if (
            min(
                self.parent_row,
                self.source,
                self.resource,
                self.parent_weight,
                self.source_period,
                self.origin_period,
                self.target_period,
                self.mode,
                self.rule_index,
                self.accepted_sequence,
            )
            < 0
            or self.parent_duration_ms < 0
        ):
            raise NumericValueError("split_group", "nonnegative complete split identity required")


@dataclass(frozen=True, slots=True, eq=False)
class NumericTask:
    units: NumericUnits
    nodes: NumericNodeColumns
    originals: NumericOriginals
    split_groups: NumericSplitGroups
    prototype_rows: np.ndarray
    priority: np.ndarray
    narrow_matches: np.ndarray
    surface_matches: np.ndarray
    same_spec_groups: np.ndarray
    start_ms: int | None
    period_ids: tuple
    node_ids: tuple
    source_ids: tuple
    resource_ids: tuple
    prototype_ids: tuple
    text_labels: tuple
    derived_rule_ids: tuple
    rule_set_fingerprint: str
    ancestor_fingerprints: tuple
    fingerprint: str

    def __post_init__(self):
        capacity = self.nodes.weight.size
        if len(self.node_ids) != capacity or len(set(self.node_ids)) != capacity:
            raise NumericValueError("node_ids", "one unique identity per numeric row required")
        if (
            not isinstance(self.ancestor_fingerprints, tuple)
            or any(not isinstance(value, str) or not value for value in self.ancestor_fingerprints)
            or len(set(self.ancestor_fingerprints)) != len(self.ancestor_fingerprints)
        ):
            raise NumericValueError("task_lineage", "unique task ancestor fingerprints required")
        if (
            self.prototype_rows.dtype != np.int64
            or self.prototype_rows.flags.writeable
            or self.prototype_rows.shape != (len(self.prototype_ids),)
        ):
            raise NumericValueError("prototype_rows", "one read-only row per prototype required")

    @classmethod
    def build(cls, problem, rule_set, timing_input=None):
        if not isinstance(problem, SchedulingProblem) or not isinstance(rule_set, ProcessRuleSet):
            raise NumericValueError("task", "validated problem and rule set required")
        if timing_input is not None and not isinstance(timing_input, DeliveryTimingInput):
            raise NumericValueError("timing", "original timing input required, not a derived clock")
        if timing_input is None and (
            problem.delivery_timing is not None
            or any(type(rule).__name__ == "DeliveryDuePerformanceRule" for rule in rule_set.rules)
        ):
            raise NumericValueError("timing", "original total durations must be supplied")
        original_count = len(problem.nodes)
        materials = (*problem.nodes, *problem.virtual_prototypes)
        count = len(materials)
        units = _units(materials, rule_set.rules)
        values = {}
        scales = (units.width, units.thickness, units.temperature, units.temperature)
        for name, scale in zip(_PHYSICAL, scales):
            values[name] = readonly(
                [
                    0
                    if getattr(node, name) is None
                    else to_ticks(getattr(node, name), scale, f"nodes[{i}].{name}")
                    for i, node in enumerate(materials)
                ],
                np.int64,
            )
        for name in ("width", "thickness"):
            column = values[name]
            int64(int(column.max()) - int(column.min()), f"{name}.difference_bound")
        temperatures = [
            int(values[name][i])
            for i, node in enumerate(materials)
            for name in ("min_temperature", "max_temperature")
            if getattr(node, name) is not None
        ] or [0]
        int64(max(temperatures) - min(temperatures), "temperature.difference_bound")
        present = [[getattr(node, name) is not None for name in _PHYSICAL] for node in materials]
        values["present"] = readonly(present, np.bool_)
        weights = [
            to_ticks(node.weight, units.weight, f"orders[{i}].weight")
            for i, node in enumerate(problem.nodes)
        ]
        weights += [
            to_ticks(node.unit_weight, units.weight, f"prototypes[{i}].weight")
            for i, node in enumerate(problem.virtual_prototypes)
        ]
        checked_sum(weights[:original_count], "original_weight_sum")
        values["weight"] = readonly(weights, np.int64)
        source_ids = tuple(node.source_order_id for node in problem.nodes)
        prototype_ids = tuple(node.prototype_id for node in problem.virtual_prototypes)
        periods = {period: i for i, period in enumerate(problem.period_order)}
        values["role"] = readonly(
            [_ROLES.index(node.material_role) for node in problem.nodes]
            + [_ROLES.index(MaterialRole.GENERATED_VIRTUAL)] * len(prototype_ids),
            np.int8,
        )
        values["source_period"] = readonly(
            [periods[node.source_period] for node in problem.nodes] + [-1] * len(prototype_ids),
            np.int64,
        )
        for key in ("source", "resource"):
            values[key] = readonly([*range(original_count), *([-1] * len(prototype_ids))], np.int64)
        values["prototype"] = readonly(
            [-1] * original_count + list(range(len(prototype_ids))), np.int64
        )
        for key in ("purpose", "split_group", "piece_index"):
            values[key] = readonly([-1] * count, np.int64)
        values["piece_count"] = readonly([1] * original_count + [0] * len(prototype_ids), np.int64)
        values["accepted_sequence"] = readonly([0] * count, np.int64)
        text_labels = []
        for name in _TEXT:
            labels, codes = [], {}
            encoded = []
            for i, material in enumerate(materials):
                value = _text(material, name)
                if not isinstance(value, str):
                    raise NumericValueError(f"nodes[{i}].{name}", "text category required")
                if value not in codes:
                    codes[value] = len(labels)
                    labels.append(value)
                encoded.append(codes[value])
            text_labels.append(tuple(labels))
            values[name] = readonly(encoded, np.int64)
        due, durations, start = [0] * original_count, [0] * count, None
        if timing_input is not None:
            by_source = {item.source_order_id.strip(): item for item in timing_input.orders}
            rates = {
                key.strip(): value for key, value in timing_input.virtual_hours_per_tonne.items()
            }
            if (
                len(by_source) != len(timing_input.orders)
                or set(by_source) != set(source_ids)
                or len(rates) != len(timing_input.virtual_hours_per_tonne)
                or set(rates) != set(prototype_ids)
            ):
                raise NumericValueError(
                    "timing", "timing must cover each original and prototype exactly once"
                )
            start = start_milliseconds(timing_input.schedule_start_at, "schedule_start_at")
            for i, source in enumerate(source_ids):
                due[i] = due_milliseconds(
                    by_source[source].due_date, start, f"orders[{i}].due_date"
                )
                durations[i] = hours_to_milliseconds(
                    by_source[source].duration_hours, f"orders[{i}].duration"
                )
            for i, prototype in enumerate(problem.virtual_prototypes):
                durations[original_count + i] = hours_to_milliseconds(
                    rates[prototype.prototype_id],
                    f"prototypes[{i}].duration",
                    weight=prototype.unit_weight,
                )
            original_time = checked_sum(durations[:original_count], "original_duration_sum")
            # Dynamic candidates must recheck against their actual virtual count.
            maximum_wait_seconds = (original_time + 999) // 1000
            checked_product(
                checked_sum(weights[:original_count], "original_weight_sum"),
                maximum_wait_seconds,
                "original_weight_time_bound",
            )
        values["duration_ms"] = readonly(durations, np.int64)
        originals = NumericOriginals(
            values["weight"][:original_count],
            values["duration_ms"][:original_count],
            readonly(due, np.int64),
            readonly([start is not None and value <= 0 for value in due], np.bool_),
        )
        priority, narrow, surface, groups = [], [], [], []
        priority_ids, narrow_ids, surface_ids, group_ids = [], [], [], []
        for rule in rule_set.rules:
            name, parameters = type(rule).__name__, rule.parameters
            if name in ("StrategicCustomerPriorityRule", "SyntheticNodePriorityRule"):
                priority_ids.append(rule.rule_id)
                priority.append(
                    [
                        int64(rule.construction_priority(node)[0], rule.rule_id)
                        for node in problem.nodes
                    ]
                    + [0] * len(prototype_ids)
                )
            elif name == "ContinuousNarrowSteelWeightRule":
                limit = to_ticks(parameters["width_upper_exclusive"], units.width, rule.rule_id)
                narrow_ids.append(rule.rule_id)
                narrow.append(
                    [
                        i < original_count
                        and values["role"][i] == 0
                        and present[i][0]
                        and values["width"][i] < limit
                        and _text(node, "grade_class").strip() == parameters["grade_class"]
                        for i, node in enumerate(materials)
                    ]
                )
            elif name == "HighSurfaceRunCountRule":
                surface_ids.append(rule.rule_id)
                configured_grades = {
                    grade.strip().upper() for grade in parameters["surface_grades"]
                }
                surface.append(
                    [
                        i < original_count
                        and values["role"][i] == 0
                        and _text(node, "surface_grade").strip().upper() in configured_grades
                        for i, node in enumerate(materials)
                    ]
                )
            elif name == "SameSpecContinuousRealWeightRule":
                group_ids.append(rule.rule_id)
                codes, column = {}, []
                for i in range(count):
                    if i >= original_count or values["role"][i] != 0:
                        column.append(-1)
                        continue
                    key = tuple(
                        (present[i][_PHYSICAL.index(field)], int(values[field][i]))
                        if field in _PHYSICAL
                        else int(values[field][i])
                        for field in parameters["group_by_fields"]
                    )
                    column.append(codes.setdefault(key, len(codes)))
                groups.append(column)
        derived = [
            readonly(np.asarray(rows, dtype=dtype).reshape((len(rows), count)), dtype)
            for rows, dtype in (
                (priority, np.int64),
                (narrow, np.bool_),
                (surface, np.bool_),
                (groups, np.int64),
            )
        ]
        nodes = NumericNodeColumns(**values)
        digest = sha256()
        digest.update(
            fingerprint(
                {
                    "units": units.fingerprint,
                    "problem": problem.input_fingerprint,
                    "rules": rule_set.fingerprint,
                    "text": text_labels,
                    "start_ms": start,
                }
            ).encode("ascii")
        )
        # Fixed field order and little-endian bytes; unused capacity is not hashed.
        for field in fields(nodes):
            array = getattr(nodes, field.name)
            digest.update(fingerprint((field.name, array.shape, array.dtype.str)).encode("ascii"))
            digest.update(array.astype(array.dtype.newbyteorder("<"), copy=False).tobytes())
        for index, array in enumerate((originals.due_ms, originals.old_backlog, *derived)):
            digest.update(fingerprint((index, array.shape, array.dtype.str)).encode("ascii"))
            digest.update(array.astype(array.dtype.newbyteorder("<"), copy=False).tobytes())
        return cls(
            units,
            nodes,
            originals,
            NumericSplitGroups.empty(),
            readonly(range(original_count, count), np.int64),
            *derived,
            start,
            problem.period_order,
            tuple(n.node_id for n in problem.nodes)
            + tuple(f"prototype-template:{value}" for value in prototype_ids),
            source_ids,
            tuple(n.source_resource_id for n in problem.nodes),
            prototype_ids,
            tuple(text_labels),
            (tuple(priority_ids), tuple(narrow_ids), tuple(surface_ids), tuple(group_ids)),
            rule_set.fingerprint,
            (),
            digest.hexdigest(),
        )


def extend_numeric_task(task, dynamic_nodes, *, split_group=None):
    """Create a private task snapshot; only the search commit may publish it."""
    if not isinstance(task, NumericTask):
        raise NumericValueError("task", "numeric task required")
    additions = tuple(dynamic_nodes)
    if not additions or any(not isinstance(item, NumericDynamicNode) for item in additions):
        raise NumericValueError("dynamic_nodes", "one or more numeric dynamic nodes required")
    old_count = task.nodes.weight.size
    if any(item.template_row >= old_count for item in additions):
        raise NumericValueError("dynamic_nodes", "template row is outside the current task")
    identities = tuple(item.node_id for item in additions)
    if len(set(identities)) != len(identities) or any(
        value in task.node_ids for value in identities
    ):
        raise NumericValueError("dynamic_nodes", "dynamic identities must be new and unique")

    def append(name, values):
        current = getattr(task.nodes, name)
        return readonly(
            np.concatenate((current, np.asarray(values, dtype=current.dtype))), current.dtype
        )

    templates = np.asarray([item.template_row for item in additions], dtype=np.int64)
    values = {
        "width": append("width", task.nodes.width[templates]),
        "thickness": append("thickness", task.nodes.thickness[templates]),
        "min_temperature": append("min_temperature", [item.min_temperature for item in additions]),
        "max_temperature": append("max_temperature", [item.max_temperature for item in additions]),
    }
    dynamic_present = np.array(task.nodes.present[templates], copy=True)
    dynamic_present[:, _PHYSICAL.index("min_temperature")] = [
        item.min_temperature_present for item in additions
    ]
    dynamic_present[:, _PHYSICAL.index("max_temperature")] = [
        item.max_temperature_present for item in additions
    ]
    values["present"] = readonly(
        np.concatenate((task.nodes.present, dynamic_present), axis=0), np.bool_
    )
    for name in (
        "weight",
        "duration_ms",
        "role",
        "source",
        "resource",
        "source_period",
        "prototype",
        "purpose",
        "split_group",
        "piece_index",
        "piece_count",
        "accepted_sequence",
    ):
        values[name] = append(name, [getattr(item, name) for item in additions])
    for name in _TEXT:
        values[name] = append(name, getattr(task.nodes, name)[templates])
    nodes = NumericNodeColumns(**values)

    derived = []
    for array in (task.priority, task.narrow_matches, task.surface_matches, task.same_spec_groups):
        dynamic = (
            array[:, templates]
            if array.shape[0]
            else np.empty((0, len(additions)), dtype=array.dtype)
        )
        derived.append(readonly(np.concatenate((array, dynamic), axis=1), array.dtype))
    priority, narrow, surface, same_spec = derived

    groups = task.split_groups
    if split_group is not None:
        if not isinstance(split_group, NumericSplitGroup):
            raise NumericValueError("split_group", "numeric split group required")
        groups = NumericSplitGroups(
            *(
                readonly(
                    np.append(getattr(groups, item.name), getattr(split_group, item.name)),
                    np.int64,
                )
                for item in fields(groups)
            )
        )
    identity = fingerprint(
        {
            "parent": task.fingerprint,
            "nodes": tuple(
                (item.name, getattr(nodes, item.name)[old_count:].tolist())
                for item in fields(nodes)
            ),
            "node_ids": identities,
            "split_group": split_group,
        }
    )
    return replace(
        task,
        nodes=nodes,
        split_groups=groups,
        priority=priority,
        narrow_matches=narrow,
        surface_matches=surface,
        same_spec_groups=same_spec,
        node_ids=task.node_ids + identities,
        ancestor_fingerprints=task.ancestor_fingerprints + (task.fingerprint,),
        fingerprint=identity,
    )


@dataclass(frozen=True, slots=True, eq=False)
class NumericPlan:
    node_rows: np.ndarray
    chain_offsets: np.ndarray
    chain_ids: np.ndarray
    chain_periods: np.ndarray
    row_to_chain: np.ndarray
    row_to_position: np.ndarray
    source_piece_offsets: np.ndarray
    source_piece_rows: np.ndarray
    source_last_position: np.ndarray
    generation: int
    task_fingerprint: str
    fingerprint: str = dataclass_field(init=False)

    def __post_init__(self):
        if not isinstance(self.task_fingerprint, str) or not self.task_fingerprint:
            raise NumericValueError("plan", "nonempty task fingerprint required")
        if int64(self.generation, "generation") < 0:
            raise NumericValueError("generation", "nonnegative generation required")
        arrays = (
            ("rows", self.node_rows),
            ("offsets", self.chain_offsets),
            ("ids", self.chain_ids),
            ("periods", self.chain_periods),
            ("row_chain", self.row_to_chain),
            ("row_position", self.row_to_position),
            ("source_offsets", self.source_piece_offsets),
            ("source_rows", self.source_piece_rows),
            ("source_last", self.source_last_position),
        )
        if any(
            not isinstance(value, np.ndarray)
            or value.ndim != 1
            or value.dtype != np.int64
            or value.flags.writeable
            or not value.flags.c_contiguous
            for _, value in arrays
        ):
            raise NumericValueError("plan", "read-only int64 plan structure required")
        digest = sha256()
        digest.update(fingerprint({"task": self.task_fingerprint, "layout": 1}).encode("ascii"))
        for name, value in arrays:
            digest.update(fingerprint((name, value.shape, value.dtype.str)).encode("ascii"))
            digest.update(value.astype(value.dtype.newbyteorder("<"), copy=False).tobytes())
        object.__setattr__(self, "fingerprint", digest.hexdigest())

    @classmethod
    def build(cls, task, node_rows, chain_offsets, chain_ids, chain_periods, *, generation=0):
        if not isinstance(task, NumericTask):
            raise NumericValueError("task", "numeric task required")
        if int64(generation, "generation") < 0:
            raise NumericValueError("generation", "nonnegative generation required")
        rows, offsets, ids, periods = (
            _indices(value, name)
            for value, name in (
                (node_rows, "node_rows"),
                (chain_offsets, "chain_offsets"),
                (chain_ids, "chain_ids"),
                (chain_periods, "chain_periods"),
            )
        )
        n, chains, capacity = rows.size, ids.size, task.nodes.weight.size
        if chains == 0 or offsets.size != chains + 1 or periods.size != chains:
            raise NumericValueError("chains", "nonempty consistent chain arrays required")
        if offsets[0] != 0 or offsets[-1] != n or np.any(offsets[1:] <= offsets[:-1]):
            raise NumericValueError("chain_offsets", "empty or invalid chain interval")
        if np.any(ids < 0) or np.unique(ids).size != chains:
            raise NumericValueError(
                "chain_ids", "chain identities must be unique nonnegative integers"
            )
        if np.any(periods < 0) or np.any(periods >= len(task.period_ids)):
            raise NumericValueError("chain_periods", "unknown period")
        if np.any(rows < 0) or np.any(rows >= capacity) or np.unique(rows).size != n:
            raise NumericValueError("node_rows", "duplicate or out-of-range node")
        owners = task.nodes.source[rows]
        original_count = task.originals.weight.size
        if np.any(owners < -1) or np.any(owners >= original_count):
            raise NumericValueError("source", "unknown original")
        positions = np.arange(n, dtype=np.int64)
        chains_by_position = np.repeat(np.arange(chains, dtype=np.int64), np.diff(offsets))
        real = owners >= 0
        if np.any((task.nodes.role[rows] != _ROLES.index(MaterialRole.GENERATED_VIRTUAL)) != real):
            raise NumericValueError("role", "material role and original ownership disagree")
        if np.any(task.nodes.resource[rows[real]] != owners[real]):
            raise NumericValueError("resource", "real source and resource ownership disagree")
        virtual_rows = rows[~real]
        if virtual_rows.size and (
            np.any(task.nodes.purpose[virtual_rows] < 0)
            or np.any(task.nodes.accepted_sequence[virtual_rows] <= 0)
        ):
            raise NumericValueError("virtual", "prototype templates are not generated nodes")
        if np.any(np.bincount(chains_by_position[real], minlength=chains) == 0):
            raise NumericValueError("chains", "a chain must contain real material")
        source_counts = np.bincount(owners[real], minlength=original_count)
        if np.any(source_counts == 0):
            raise NumericValueError("source", "missing original order")
        last = np.full(original_count, -1, dtype=np.int64)
        np.maximum.at(last, owners[real], positions[real])
        by_source = np.argsort(owners[real], kind="stable")
        pieces = rows[real][by_source]
        source_offsets = np.concatenate(
            (np.zeros(1, dtype=np.int64), np.cumsum(source_counts, dtype=np.int64))
        )
        for source in range(original_count):
            selection = pieces[source_offsets[source] : source_offsets[source + 1]]
            if checked_sum((int(w) for w in task.nodes.weight[selection]), "source_weight") != int(
                task.originals.weight[source]
            ):
                raise NumericValueError("source_weight", "original weight is not conserved")
            if checked_sum(
                (int(d) for d in task.nodes.duration_ms[selection]), "source_duration"
            ) != int(task.originals.duration_ms[source]):
                raise NumericValueError("source_duration", "original duration is not conserved")
        checked_sum((int(w) for w in task.nodes.weight[rows]), "plan_weight")
        total_time = checked_sum((int(d) for d in task.nodes.duration_ms[rows]), "plan_duration")
        total_weight = checked_sum((int(w) for w in task.originals.weight), "source_total_weight")
        checked_product(total_weight, (total_time + 999) // 1000, "plan_weight_time_bound")
        row_chain, row_position = (
            np.full(capacity, -1, dtype=np.int64),
            np.full(capacity, -1, dtype=np.int64),
        )
        row_chain[rows] = chains_by_position
        row_position[rows] = positions - offsets[chains_by_position]
        return cls(
            rows,
            offsets,
            ids,
            periods,
            readonly(row_chain, np.int64),
            readonly(row_position, np.int64),
            readonly(source_offsets, np.int64),
            readonly(pieces, np.int64),
            readonly(last, np.int64),
            generation,
            task.fingerprint,
        )


@dataclass(frozen=True, slots=True, eq=False)
class NumericPlanOverlay:
    """Candidate-private chain views without formal plan indexes or fingerprint."""

    chains: tuple
    chain_ids: np.ndarray
    chain_periods: np.ndarray
    generation: int
    task_fingerprint: str

    def __post_init__(self):
        if (
            not isinstance(self.chains, tuple)
            or not self.chains
            or any(not isinstance(chain, np.ndarray) or chain.ndim != 1 for chain in self.chains)
            or any(chain.dtype != np.int64 or chain.flags.writeable for chain in self.chains)
            or not isinstance(self.chain_ids, np.ndarray)
            or not isinstance(self.chain_periods, np.ndarray)
            or self.chain_ids.dtype != np.int64
            or self.chain_periods.dtype != np.int64
            or self.chain_ids.flags.writeable
            or self.chain_periods.flags.writeable
            or self.chain_ids.shape != (len(self.chains),)
            or self.chain_periods.shape != (len(self.chains),)
            or not isinstance(self.task_fingerprint, str)
            or not self.task_fingerprint
        ):
            raise NumericValueError("candidate_overlay", "read-only numeric chain overlay required")
        if int64(self.generation, "generation") < 0:
            raise NumericValueError("generation", "nonnegative generation required")

    @classmethod
    def build(cls, task, current, chains, chain_ids, chain_periods):
        if not isinstance(task, NumericTask) or not isinstance(current, NumericPlan):
            raise NumericValueError("candidate_overlay", "numeric task and current plan required")
        values = []
        for chain in chains:
            rows = np.asarray(chain, dtype=np.int64)
            if rows.ndim != 1 or not rows.size:
                raise NumericValueError("candidate_overlay", "nonempty integer chains required")
            if rows.flags.writeable or not rows.flags.c_contiguous:
                rows = readonly(rows, np.int64)
            values.append(rows)
        ids = readonly(chain_ids, np.int64)
        periods = readonly(chain_periods, np.int64)
        if (
            ids.size != len(values)
            or periods.size != len(values)
            or np.unique(ids).size != ids.size
            or np.any(ids < 0)
            or np.any(periods < 0)
            or np.any(periods >= len(task.period_ids))
            or any(np.any(chain < 0) or np.any(chain >= task.nodes.weight.size) for chain in values)
        ):
            raise NumericValueError(
                "candidate_overlay", "candidate chain identity or row is invalid"
            )
        return cls(tuple(values), ids, periods, current.generation + 1, task.fingerprint)


# Descriptions are homogeneous integer records; material fields stay in typed
# columns. Owner is an original-source index, not a chain index or node row.
DESCRIPTOR_FIELDS = (
    "action", "source_chain_id", "target_chain_id", "node_row", "target_position",
    "source_start", "source_stop", "target_start", "target_stop",
    "source_reversed", "target_reversed", "owner_source", "repair_variant",
)


@dataclass(frozen=True, slots=True, eq=False)
class NumericCandidateDescriptors:
    task: NumericTask
    plan: NumericPlan
    values: np.ndarray

    def __post_init__(self):
        _require_numeric_base(self.task, self.plan)
        value = self.values
        if (not isinstance(value, np.ndarray) or value.dtype != np.int64
                or value.ndim != 2 or value.shape[1] != len(DESCRIPTOR_FIELDS)
                or value.flags.writeable or not value.flags.c_contiguous):
            raise NumericValueError("descriptors", "read-only contiguous integer records required")
        action = value[:, DESCRIPTOR_FIELDS.index("action")]
        if np.any(action < 0) or np.any(action >= len(NumericSearchAction)):
            raise NumericValueError("descriptors.action", "unknown action code")
        for name in ("source_reversed", "target_reversed"):
            column = value[:, DESCRIPTOR_FIELDS.index(name)]
            if np.any((column != 0) & (column != 1)):
                raise NumericValueError(f"descriptors.{name}", "integer boolean required")
        # Action-specific position/period authorization belongs to the common
        # attempt, not this transport boundary or a second algorithm here.
        for name in ("source_chain_id", "target_chain_id", "repair_variant"):
            if np.any(value[:, DESCRIPTOR_FIELDS.index(name)] < 0):
                raise NumericValueError(f"descriptors.{name}", "nonnegative code required")
        owners = value[:, DESCRIPTOR_FIELDS.index("owner_source")]
        if np.any(owners < -1) or np.any(owners >= self.task.originals.weight.size):
            raise NumericValueError("descriptors.owner_source", "original index outside task")

    def require_current(self, task, plan):
        if task is not self.task or plan is not self.plan:
            raise NumericValueError("descriptors", "stale task or plan generation")


def _require_numeric_base(task, plan):
    if (not isinstance(task, NumericTask) or not isinstance(plan, NumericPlan)
            or plan.task_fingerprint != task.fingerprint):
        raise NumericValueError("workspace", "matching numeric task and plan required")


# Array-only named tuples are directly consumable by native kernels. Reuse the
# authoritative field names and dtypes instead of defining a second node schema.
PrivateNodeColumns = namedtuple("PrivateNodeColumns", (f.name for f in fields(NumericNodeColumns)))
PrivateSplitColumns = namedtuple("PrivateSplitColumns", (f.name for f in fields(NumericSplitGroups)))
PrivateDerivedColumns = namedtuple("PrivateDerivedColumns", "priority narrow_matches surface_matches same_spec_groups")
NumericChainView = namedtuple("NumericChainView", (
    "base_rows changed_rows starts stops private ids periods count epoch"
))


@dataclass(slots=True, eq=False)
class NumericCandidateWorkspace:
    """One attempt owns writable tails; accepted input arrays are never copied.

    A chain view borrows the workspace until reset/growth. Validate its epoch at
    the Python dispatch boundary; native code uses only arrays/scalars. No view
    may survive acceptance or be reused by a concurrent attempt.
    """

    task: NumericTask
    plan: NumericPlan
    changed_rows: np.ndarray
    starts: np.ndarray
    stops: np.ndarray
    private: np.ndarray
    ids: np.ndarray
    periods: np.ndarray
    nodes: PrivateNodeColumns
    derived: PrivateDerivedColumns
    split_groups: PrivateSplitColumns
    templates: np.ndarray
    event_node_ends: np.ndarray
    event_group_ends: np.ndarray
    chain_count: int = 0
    changed_count: int = 0
    node_count: int = 0
    group_count: int = 0
    event_count: int = 0
    epoch: int = 0

    @classmethod
    def allocate(cls, task, plan, *, changed_capacity, chain_capacity,
                 node_capacity, group_capacity, event_capacity):
        _require_numeric_base(task, plan)
        sizes = (changed_capacity, chain_capacity, node_capacity, group_capacity, event_capacity)
        if any(type(n) is not int or n < 0 for n in sizes):
            raise NumericValueError("workspace.capacity", "nonnegative integer capacities required")
        if chain_capacity < plan.chain_ids.size:
            raise NumericValueError("workspace.capacity", "capacity must contain the base chain map")

        def empty_like(column, count, axis=0):
            shape = list(column.shape)
            shape[axis] = count
            return np.empty(tuple(shape), dtype=column.dtype)

        nodes = PrivateNodeColumns(*(empty_like(getattr(task.nodes, f.name), node_capacity)
                                     for f in fields(NumericNodeColumns)))
        derived = PrivateDerivedColumns(*(empty_like(getattr(task, name), node_capacity, 1)
                                          for name in PrivateDerivedColumns._fields))
        groups = PrivateSplitColumns(*(np.empty(group_capacity, dtype=np.int64)
                                       for _ in fields(NumericSplitGroups)))
        result = cls(task, plan, np.empty(changed_capacity, dtype=np.int64),
                     np.empty(chain_capacity, dtype=np.int64),
                     np.empty(chain_capacity, dtype=np.int64),
                     np.empty(chain_capacity, dtype=np.bool_),
                     np.empty(chain_capacity, dtype=np.int64),
                     np.empty(chain_capacity, dtype=np.int64),
                     nodes, derived, groups, np.empty(node_capacity, dtype=np.int64),
                     np.empty(event_capacity, dtype=np.int64),
                     np.empty(event_capacity, dtype=np.int64))
        result.reset()
        return result

    def reset(self):
        """Invalidate all borrowed views without touching quota, RNG or the base."""
        count = self.plan.chain_ids.size
        self.starts[:count] = self.plan.chain_offsets[:-1]
        self.stops[:count] = self.plan.chain_offsets[1:]
        self.private[:count] = False
        self.ids[:count] = self.plan.chain_ids
        self.periods[:count] = self.plan.chain_periods
        self.chain_count = count
        self.changed_count = self.node_count = self.group_count = self.event_count = 0
        self.epoch += 1

    def require_current(self, task, plan):
        if task is not self.task or plan is not self.plan:
            raise NumericValueError("workspace", "stale task or plan generation")

    def view(self):
        return NumericChainView(self.plan.node_rows, self.changed_rows,
                                self.starts, self.stops, self.private, self.ids,
                                self.periods, self.chain_count, self.epoch)

    def require_view(self, view):
        if (not isinstance(view, NumericChainView) or view.epoch != self.epoch
                or view.changed_rows is not self.changed_rows or view.ids is not self.ids):
            raise NumericValueError("workspace.view", "expired or foreign borrowed view")

    def capacity_status(self, *, changed_rows, chains, nodes, groups, events):
        requested = (changed_rows, chains, nodes, groups, events)
        if any(type(n) is not int or n < 0 for n in requested):
            return INVALID
        capacities = (self.changed_rows.size, self.ids.size, self.templates.size,
                      self.split_groups.parent_row.size, self.event_node_ends.size)
        return CAPACITY if any(n > cap for n, cap in zip(requested, capacities)) else OK

    def grow_for_retry(self, *, changed_capacity, chain_capacity, node_capacity,
                       group_capacity, event_capacity):
        """Replace private buffers and retry from the same base, never publish."""
        capacities = (self.changed_rows.size, self.ids.size, self.templates.size,
                      self.split_groups.parent_row.size, self.event_node_ends.size)
        requested = (changed_capacity, chain_capacity, node_capacity, group_capacity, event_capacity)
        if any(type(n) is not int or n < current for n, current in zip(requested, capacities)):
            raise NumericValueError("workspace.capacity", "retry cannot shrink existing capacities")
        replacement = type(self).allocate(self.task, self.plan,
            changed_capacity=changed_capacity, chain_capacity=chain_capacity,
            node_capacity=node_capacity, group_capacity=group_capacity, event_capacity=event_capacity)
        epoch = self.epoch + 1
        for item in fields(self):
            if item.name not in {"task", "plan", "epoch"}:
                setattr(self, item.name, getattr(replacement, item.name))
        self.epoch = epoch

    @property
    def allocated_bytes(self):
        """Owned buffers only; shared task/plan memory and Python wrappers excluded."""
        arrays = (self.changed_rows, self.starts, self.stops, self.private, self.ids,
                  self.periods, self.templates, self.event_node_ends, self.event_group_ends,
                  *self.nodes, *self.derived, *self.split_groups)
        return sum(array.nbytes for array in arrays)
