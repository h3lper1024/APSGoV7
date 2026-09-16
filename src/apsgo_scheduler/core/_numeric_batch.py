"""Private flat batch preparation and native result views, not another evaluator."""

import numpy as np
from numba.typed import List as NativeList

from . import _numeric_candidate_kernel as candidate_kernel
from ._numeric_evaluation import NumericEvaluationContext
from ._numeric_candidate_kernel import (
    capture_candidate_result, NumericDeferredCandidateFailure, VARIANT,
)
from ._numeric_state import (
    readonly, NumericCandidateWorkspace, NumericCandidateDescriptors, DESCRIPTOR_FIELDS, OK, CAPACITY,
)
from ._numeric_units import NumericValueError

MAX_BATCH_CANDIDATES = 64
MAX_BATCH_BYTES = 32 * 1024 * 1024


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
