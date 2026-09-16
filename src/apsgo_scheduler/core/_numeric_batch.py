"""Private flat batch preparation and native result views, not another evaluator."""

from dataclasses import dataclass

import numpy as np
from numba.typed import List as NativeList

from . import _numeric_candidate_kernel as candidate_kernel

from ._numeric_evaluation import (
    _objective_order, _validate_evaluation_inputs, _validated_reuse, NumericEvaluationContext,
)
from ._numeric_candidate_kernel import (
    capture_candidate_result, NumericDeferredCandidateFailure, VARIANT,
)
from ._numeric_kernel import (
    KernelResult, TaskColumns, evaluate_batch_kernel, rule_tables, task_columns,
)
from ._numeric_state import (
    readonly, NumericCandidateWorkspace, NumericCandidateDescriptors, DESCRIPTOR_FIELDS, OK, CAPACITY,
)
from ._numeric_units import NumericValueError

MAX_BATCH_CANDIDATES = 64
MAX_BATCH_BYTES = 32 * 1024 * 1024
_DERIVED = frozenset(("priority", "narrow", "surface", "spec"))
_SHARED = frozenset(("original_weight", "due", "backlog", "scales"))
_RESOURCE_FIELDS = (
    "resource", "prototype", "purpose", "split_group", "piece_index", "piece_count",
    "accepted_sequence",
)


def allocate_candidate_workspace(task, plan):
    return NumericCandidateWorkspace.allocate(task, plan,
        changed_capacity=max(64, 2 * plan.node_rows.size + 16),
        chain_capacity=plan.chain_ids.size + 1, node_capacity=8,
        group_capacity=0, event_capacity=8)


class NumericCandidateBatchWorkspace:
    """Generation-local, bounded reusable slots for complete numeric attempts.

    A slot remains borrowed until the whole batch is consumed or abandoned. The
    next reset invalidates old views before any buffer is reused. No global pool
    can retain unrelated tasks or feed a different accepted generation.

    max_bytes bounds the estimated multi-candidate envelope. If even one attempt
    exceeds it, the batch becomes single-candidate; it cannot cap the irreducible
    memory of that original candidate or alter its result to fit the batch.
    """
    def __init__(self, task, program, quality, plan, evaluation, *,
                 maximum_candidates=8, max_bytes=MAX_BATCH_BYTES, _native_executor=None):
        if (type(maximum_candidates) is not int or not 1 <= maximum_candidates <= MAX_BATCH_CANDIDATES
                or type(max_bytes) is not int or max_bytes <= 0):
            raise NumericValueError("candidate_batch.capacity", "bounded positive batch capacity required")
        if _native_executor not in (None, "serial", "parallel"):
            raise NumericValueError("candidate_batch.executor", "known internal executor required")
        self.native_executor = _native_executor
        self.context = NumericEvaluationContext(task, program, quality, plan, evaluation)
        self.workspaces = [allocate_candidate_workspace(task, plan)]
        self.used = 0
        # Include the result/scratch envelope, not just private input buffers.
        result_bytes = (plan.chain_ids.size + 1) * (320 + len(program.rules) * 32)
        result_bytes += task.originals.weight.size * 64 + 1024
        result_bytes += (4 * plan.node_rows.size + 2 * self.workspaces[0].templates.size) * 8
        self.maximum_candidates = max(1, min(maximum_candidates,
            max_bytes // (2 * (self.workspaces[0].allocated_bytes + result_bytes))))
        self.allocation_count = 1

    def require_current(self, task, program, quality, plan, evaluation):
        c = self.context
        if (task is not c.task or program is not c.program or quality is not c.quality
                or plan is not c.plan or evaluation is not c.previous_evaluation):
            raise NumericValueError("candidate_batch", "stale generation inputs")

    def acquire(self):
        if self.used == 2 * self.maximum_candidates:
            raise NumericValueError("candidate_batch.capacity", "previous batch must be released")
        if self.used == len(self.workspaces):
            self.workspaces.append(allocate_candidate_workspace(self.context.task, self.context.plan))
            self.allocation_count += 1
        result = self.workspaces[self.used]
        self.used += 1
        return result

    def release_last(self, workspace):
        if not self.used or workspace is not self.workspaces[self.used - 1]:
            raise NumericValueError("candidate_batch", "only the unretained last attempt can be released")
        workspace.reset()
        self.used -= 1

    def release(self):
        for workspace in self.workspaces[:self.used]:
            workspace.reset()
        self.used = 0

    @property
    def allocated_bytes(self):
        return sum(workspace.allocated_bytes for workspace in self.workspaces)

    def attempts(self, descriptor, policy, variants, *, virtual_sequence,
                 split_sequence, allows_continue, capture=capture_candidate_result):
        """The caller declares variants in original order; no quota is used here."""
        descriptions, invalid = None, None
        try:
            if (not isinstance(descriptor, np.ndarray) or descriptor.dtype != np.int64
                    or descriptor.shape != (len(DESCRIPTOR_FIELDS),)):
                raise NumericValueError("descriptors", "integer descriptor row required")
            values = np.repeat(descriptor.reshape(1, -1), len(variants), axis=0)
            values[:, VARIANT] = variants
            values.setflags(write=False)
            descriptions = NumericCandidateDescriptors(self.context.task, self.context.plan, values)
        except NumericValueError as error:
            invalid = error
        for index, variant in enumerate(variants):
            workspace = self.acquire()
            if invalid is not None:
                yield workspace, NumericDeferredCandidateFailure(workspace.view(), invalid)
                return
            while True:
                result = capture(workspace, self.context.program, self.context.quality,
                    descriptions, index, policy, virtual_sequence=virtual_sequence,
                    split_sequence=split_sequence, previous_evaluation=self.context.previous_evaluation,
                    allows_continue=allows_continue, evaluation_context=self.context)
                if isinstance(result, NumericDeferredCandidateFailure) or result.status != CAPACITY:
                    break
                workspace.grow()
            if isinstance(result, NumericDeferredCandidateFailure):
                yield workspace, result
                return
            if variant and not result.cleaned_variant_exists and result.status == OK:
                self.release_last(workspace)
                continue
            yield workspace, result

    def prepare_many(self, entries, *, virtual_sequence, split_sequence, allows_continue):
        """Whole native batches with ordered main-thread consumption."""
        k = candidate_kernel
        if self.used or not 1 <= len(entries) <= self.maximum_candidates:
            raise NumericValueError("candidate_batch.capacity", "released bounded workspace required")
        data = k.native_candidate_input(self.context)
        # Native indirection avoids Numba's parallel-wrapper flattening of nested
        # named tuples. This one immutable entry borrows the same input arrays.
        inputs = NativeList([data])
        frames, policies, frame_refs = NativeList(), NativeList(), []
        descriptions, starts, active, workspaces, errors, originals = [], [0], [], [], [], []
        for descriptor, policy, variants in entries:
            invalid, values = None, None
            try:
                if (not isinstance(descriptor, np.ndarray) or descriptor.dtype != np.int64
                        or descriptor.shape != (len(DESCRIPTOR_FIELDS),)):
                    raise NumericValueError("descriptors", "integer descriptor row required")
                values = np.repeat(descriptor.reshape(1, -1), len(variants), axis=0)
                values[:, VARIANT] = variants
                values.setflags(write=False)
                NumericCandidateDescriptors(self.context.task, self.context.plan, values)
            except NumericValueError as error:
                invalid = error
            count = 1 if invalid is not None else len(variants)
            native_policy = k.native_candidate_policy(self.context.program, policy)
            for index in range(count):
                workspace = self.acquire()
                workspace.reset()
                frame = k.native_candidate_frame(workspace, data, virtual_sequence)
                frames.append(frame)
                frame_refs.append(frame)
                policies.append(native_policy)
                descriptions.append(np.full(len(DESCRIPTOR_FIELDS), -1, np.int64) if invalid else values[index])
                workspaces.append(workspace)
                originals.append(policy)
                errors.append(invalid)
                active.append(invalid is None)
            starts.append(len(workspaces))
        descriptions = readonly(np.array(descriptions, np.int64), np.int64)
        starts = readonly(starts, np.int64)
        active = np.array(active, np.bool_)
        step = (k.native_candidate_batch_parallel_step if self.native_executor == "parallel"
                else k.native_candidate_batch_serial_step)
        while any(active[i] and frame.control[k._NC_PHASE] != k._NC_DONE for i, frame in enumerate(frame_refs)):
            if allows_continue is not None and not allows_continue():
                for i, frame in enumerate(frame_refs):
                    if active[i] and frame.control[k._NC_PHASE] != k._NC_DONE:
                        frame.control[k._NC_STATUS] = k.CANCELLED
                        frame.control[k._NC_PREPARED] = 0
                        frame.control[k._NC_PHASE] = k._NC_DONE
                break
            step(inputs, frames, descriptions, policies, starts, active)
            # Only the main thread may replace buffers, after all workers join.
            for i, frame in enumerate(frame_refs):
                if active[i] and frame.control[k._NC_STATUS] == CAPACITY:
                    workspaces[i].grow()
                    frame_refs[i] = k.native_candidate_frame(workspaces[i], data, virtual_sequence)
                    frames[i] = frame_refs[i]
        prepared = []
        for first, stop in zip(starts[:-1], starts[1:]):
            attempts = []
            for i in range(int(first), int(stop)):
                workspace = workspaces[i]
                if errors[i] is not None:
                    attempts.append((workspace, NumericDeferredCandidateFailure(workspace.view(), errors[i])))
                    break
                if not active[i]:
                    continue
                try:
                    result = k.finish_native_candidate(workspace, frame_refs[i], descriptions[i],
                        self.context.program, self.context.quality, originals[i], split_sequence)
                except NumericValueError as error:
                    attempts.append((workspace, NumericDeferredCandidateFailure(workspace.view(), error)))
                    break
                if descriptions[i, VARIANT] and not result.cleaned_variant_exists and result.status == OK:
                    continue
                attempts.append((workspace, result))
            prepared.append(attempts)
        return prepared


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
