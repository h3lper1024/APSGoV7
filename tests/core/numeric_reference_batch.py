"""Retired reference helpers from 7108523; test-only, never a production fallback."""

from dataclasses import dataclass
from collections import namedtuple
from numba import njit
import numpy as np


from apsgo_scheduler.core._numeric_evaluation import _objective_order, _validate_evaluation_inputs, _validated_reuse

from apsgo_scheduler.core._numeric_kernel import KernelResult, TaskColumns, evaluate_kernel, rule_tables, task_columns, STALE
from apsgo_scheduler.core._numeric_state import readonly
from apsgo_scheduler.core._numeric_units import NumericValueError

MAX_BATCH_CANDIDATES = 64
MAX_BATCH_BYTES = 32 * 1024 * 1024
_DERIVED = frozenset(("priority", "narrow", "surface", "spec"))
_SHARED = frozenset(("original_weight", "due", "backlog", "scales"))
_RESOURCE_FIELDS = (
    "resource", "prototype", "purpose", "split_group", "piece_index", "piece_count",
    "accepted_sequence",
)

@dataclass(frozen=True, slots=True)
class NumericCandidateBatch:
    task_fingerprint: str
    rule_fingerprint: str
    quality_fingerprint: str
    plan_fingerprint: str
    generation: int
    columns: object
    rules: object
    objectives: np.ndarray
    reuse: object
    node_rows: np.ndarray
    chain_offsets: np.ndarray
    candidate_offsets: np.ndarray
    chain_ids: np.ndarray
    chain_periods: np.ndarray
    candidate_generation: np.ndarray
    candidate_sequence: np.ndarray
    candidate_action: np.ndarray
    changed_chain_offsets: np.ndarray
    changed_chain_indices: np.ndarray
    resource_row_offsets: np.ndarray
    resource_metadata: np.ndarray
    estimated_bytes: int

    def require_current(self, task, program, quality, plan):
        if (task.fingerprint != self.task_fingerprint
                or program.fingerprint != self.rule_fingerprint
                or quality.fingerprint != self.quality_fingerprint
                or plan.fingerprint != self.plan_fingerprint
                or plan.generation != self.generation):
            raise NumericValueError("candidate_batch", "stale task, rules or plan generation")

def pack_numeric_candidates(task, program, quality, plan, evaluation, candidates,
                            sequences, actions, *, max_bytes=MAX_BATCH_BYTES):
    """Private resource tails get disjoint numeric rows; shared inputs stay read-only."""
    size = len(candidates)
    if not 1 <= size <= MAX_BATCH_CANDIDATES:
        raise NumericValueError("candidate_batch.capacity", "one to 64 candidates required")
    sequence = readonly(sequences, np.int64)
    action = readonly(actions, np.int64)
    if (sequence.shape != (size,) or action.shape != (size,)
            or np.any(sequence <= 0) or np.any(sequence[1:] <= sequence[:-1])):
        raise NumericValueError("candidate_batch", "ordered positive sequences and actions required")
    reuse = _validated_reuse(task, program, quality, task, program, quality, plan, evaluation)
    base = task.nodes.weight.size
    tasks, private, node_count, chain_count = [], [0], 0, 0
    for candidate_task, candidate_program, candidate_quality, overlay in candidates:
        _validate_evaluation_inputs(candidate_task, candidate_program, candidate_quality, overlay)
        if (candidate_task is not task and task.fingerprint not in candidate_task.ancestor_fingerprints
                or candidate_program.rules != program.rules
                or candidate_quality.objectives != quality.objectives
                or overlay.generation != plan.generation + 1
                or candidate_task.start_ms is None):
            raise NumericValueError("candidate_batch", "compatible candidate-private extension required")
        tasks.append(task_columns(candidate_task))
        private.append(private[-1] + candidate_task.nodes.weight.size - base)
        node_count += sum(chain.size for chain in overlay.chains)
        chain_count += overlay.chain_ids.size
    # Bound input/output and one single-candidate scratch allocation before copying.
    estimated = (node_count * 48 + (chain_count + size) * (320 + len(program.rules) * 32)
                 + size * task.originals.weight.size * 64 + size * 1024)
    if private[-1]:
        estimated += sum(v.nbytes for v in task_columns(task)) * 2
        estimated += private[-1] * (512 + (task.priority.shape[0] + task.narrow_matches.shape[0]
                                           + task.surface_matches.shape[0] + task.same_spec_groups.shape[0]) * 16)
    if type(max_bytes) is not int or max_bytes <= 0 or estimated > max_bytes:
        raise NumericValueError("candidate_batch.capacity", "batch exceeds private workspace limit")
    shared = task_columns(task)
    columns = []
    for name, original in zip(TaskColumns._fields, shared):
        if name in _SHARED or not private[-1]:
            columns.append(original)
        else:
            axis = 1 if name in _DERIVED else 0
            tails = [getattr(t, name)[:, base:] if axis else getattr(t, name)[base:] for t in tasks]
            columns.append(readonly(np.concatenate((original, *tails), axis=axis), original.dtype))
    rows, chain_offsets, candidate_offsets = [], [0], [0]
    identities, periods, changed, changed_offsets, resource_meta = [], [], [], [0], []
    old_chains = {int(identity): i for i, identity in enumerate(plan.chain_ids)}
    for i, (candidate_task, _, _, overlay) in enumerate(candidates):
        for j, chain in enumerate(overlay.chains):
            mapped = np.asarray(chain, dtype=np.int64)
            if private[i]:
                mapped = np.where(mapped >= base, mapped + private[i], mapped)
            rows.append(mapped)
            chain_offsets.append(chain_offsets[-1] + mapped.size)
            old = old_chains.get(int(overlay.chain_ids[j]))
            if (old is None or plan.chain_periods[old] != overlay.chain_periods[j]
                    or not np.array_equal(mapped, plan.node_rows[plan.chain_offsets[old]:plan.chain_offsets[old + 1]])):
                changed.append(candidate_offsets[-1] + j)
        identities.extend(overlay.chain_ids)
        periods.extend(overlay.chain_periods)
        candidate_offsets.append(candidate_offsets[-1] + overlay.chain_ids.size)
        changed_offsets.append(len(changed))
        n = candidate_task.nodes
        for row in range(base, n.weight.size):
            group = int(n.split_group[row])
            target = int(candidate_task.split_groups.target_period[group]) if group >= 0 else -1
            resource_meta.append((*[int(getattr(n, f)[row]) for f in _RESOURCE_FIELDS], target))
    return NumericCandidateBatch(
        task.fingerprint, program.fingerprint, quality.fingerprint, plan.fingerprint,
        plan.generation, TaskColumns(*columns), rule_tables(program.rules),
        _objective_order(quality.objectives), reuse,
        readonly(np.concatenate(rows), np.int64), readonly(chain_offsets, np.int64),
        readonly(candidate_offsets, np.int64), readonly(identities, np.int64),
        readonly(periods, np.int64), readonly([plan.generation] * size, np.int64),
        sequence, action, readonly(changed_offsets, np.int64), readonly(changed, np.int64),
        readonly(private, np.int64),
        readonly(np.asarray(resource_meta, dtype=np.int64).reshape((-1, len(_RESOURCE_FIELDS) + 1)), np.int64),
        estimated,
    )

def evaluate_numeric_batch(batch, task, program, quality, plan, *, cancelled=False):
    batch.require_current(task, program, quality, plan)
    output = evaluate_batch_kernel(
        batch.columns, batch.rules, batch.node_rows, batch.chain_offsets,
        batch.candidate_offsets, batch.chain_ids, batch.chain_periods, batch.objectives,
        batch.candidate_generation, plan.generation, batch.reuse, cancelled,
    )
    for array in output:
        array.setflags(write=False)
    return output

def numeric_batch_result(batch, output, index):
    """Read-only native summary view; never creates rule/violation/metric objects."""
    if not 0 <= index < batch.candidate_sequence.size:
        raise NumericValueError("candidate_batch", "candidate index outside batch")
    first, stop = map(int, batch.candidate_offsets[index:index + 2])
    a, b = int(batch.chain_offsets[first]), int(batch.chain_offsets[stop])
    return KernelResult(
        output.status[index], output.quality[index], output.facts[first:stop],
        output.scores[first + index:stop + index + 1],
        output.hits[first + index:stop + index + 1], output.totals[index], output.ends[a:b],
        output.completion[index], output.late[index], output.waits[index],
        readonly(np.empty((0, 7)), np.int64), readonly(np.empty((0, 5)), np.int64),
        output.counts[index], output.event_counts[first:stop],
    )


BatchResult = namedtuple("BatchResult", (
    "status quality facts scores hits totals ends completion late waits counts event_counts"
))


@njit
def evaluate_batch_kernel(t, r, rows, chain_offsets, candidate_offsets, ids, periods,
                          objective_order, generations, current_generation, reuse,
                          cancelled=False):
    """Bounded serial outer loop; every row calls the authoritative single kernel."""
    size, chains = generations.size, periods.size
    originals = t.original_weight.size
    out = BatchResult(
        np.zeros((size, 5), np.int64), np.zeros((size, 9), np.int64),
        np.zeros((chains, 5), np.int64), np.zeros((chains + size, 4), np.int64),
        np.zeros((chains + size, r.meta.shape[0]), np.int64), np.zeros((size, 7), np.int64),
        np.zeros(rows.size, np.int64), np.zeros((size, originals), np.int64),
        np.zeros((size, originals), np.bool_), np.zeros((size, originals), np.int64),
        np.zeros((size, 4), np.int64), np.zeros((chains, 2), np.int64),
    )
    for i in range(size):
        if generations[i] != current_generation:
            out.status[i, 0] = STALE
            continue
        first, stop = candidate_offsets[i], candidate_offsets[i + 1]
        start_row, stop_row = chain_offsets[first], chain_offsets[stop]
        result = evaluate_kernel(
            t, r, rows[start_row:stop_row], chain_offsets[first:stop + 1] - start_row,
            periods[first:stop], objective_order, False, 0, 0, cancelled,
            ids[first:stop], reuse,
        )
        out.status[i] = result.status
        out.quality[i] = result.quality
        out.facts[first:stop] = result.facts
        out.scores[first + i:stop + i + 1] = result.scores
        out.hits[first + i:stop + i + 1] = result.hits
        out.totals[i] = result.totals
        out.ends[start_row:stop_row] = result.ends
        out.completion[i] = result.completion
        out.late[i] = result.late
        out.waits[i] = result.waits
        out.counts[i] = result.counts
        out.event_counts[first:stop] = result.event_counts
    return out
