"""Resumable numeric enumeration for the phased refinement entry.

Cursors contain loop coordinates only. Rule predicates and owner/rank tables
come from the original refinement scan. A step does bounded raw work and yields
one description, MORE, or DONE; it never evaluates a candidate or charges quota.
"""
from dataclasses import dataclass, field

import numpy as np
from numba import njit

from . import _numeric_refinement_scan as scan

MORE, ERROR, DONE = -1, -2, -3
_CURSOR_SIZE = 10


@njit
def _piece(x, source, offset):
    index = x.piece_offsets[source + 1] - 1 - offset
    return -1 if index < x.piece_offsets[source] else x.piece_rows[index]


@njit
def _clear(c, start):
    for i in range(start, c.size):
        c[i] = 0


@njit
def intra_step(x, source, lane, c, work=256):
    for _ in range(work):
        row = _piece(x, source, c[0])
        if row < 0:
            return scan.description(DONE)
        chain, start = x.row_chain[row], x.row_position[row]
        if c[1] >= start:
            c[0] += 1
            c[1] = 0
            continue
        target = c[1]
        c[1] += 1
        base = x.offsets[chain]
        critical = (scan.has_critical(x, chain, start, start + 1)
                    or x.early[base + start + 1] > x.early[base + target])
        if critical == lane:
            return scan.description(scan.INTRA, x.ids[chain], x.ids[chain], start, start + 1, target)
    return scan.description(MORE)


@njit
def node_move_step(x, t, rules, source, lane, c, work=256):
    status = np.array((scan.OK, -1, -1, -1), np.int64)
    for _ in range(work):
        row = _piece(x, source, c[0])
        if row < 0:
            return scan.description(DONE)
        chain, start = x.row_chain[row], x.row_position[row]
        if (scan.has_critical(x, chain, start, start + 1) != lane
                or x.offsets[chain + 1] - x.offsets[chain] < 2 or c[1] >= x.ids.size):
            c[0] += 1
            _clear(c, 1)
            continue
        target = c[1]
        if target == chain or c[2] == 3:
            c[1] += 1
            _clear(c, 2)
            continue
        first, last = x.offsets[target], x.offsets[target + 1]
        if c[3] > last - first:
            c[2] += 1
            c[3] = 0
            continue
        slot = c[3]
        left = slot == 0 or scan.all_edges_allowed_values(
            scan.edge_node(t, x.rows[first + slot - 1]), scan.edge_node(t, row), rules, status)
        right = slot == last - first or scan.all_edges_allowed_values(
            scan.edge_node(t, row), scan.edge_node(t, x.rows[first + slot]), rules, status)
        if status[0] != scan.OK:
            return scan.description(ERROR)
        c[3] += 1
        # Pass 0 validates the full target before emitting, as the old bitmap
        # precheck did. Passes 1/2 recompute the same pure predicate, direct first.
        # This trades bounded recomputation for not retaining a bitmap in progress.
        if c[2] and (left and right) == (c[2] == 1):
            return scan.description(scan.NODE_MOVE, x.ids[chain], x.ids[target], start, start + 1, slot)
    return scan.description(MORE)


@njit
def node_exchange_step(x, source, lane, c, work=256):
    for _ in range(work):
        row = _piece(x, source, c[0])
        if row < 0:
            return scan.description(DONE)
        if c[1] >= x.sources.size:
            c[0] += 1
            _clear(c, 1)
            continue
        other = _piece(x, x.sources[c[1]], c[2])
        if other < 0:
            c[1] += 1
            c[2] = 0
            continue
        c[2] += 1
        chain, start = x.row_chain[row], x.row_position[row]
        target, slot = x.row_chain[other], x.row_position[other]
        if target == chain or x.ranks[x.offsets[chain] + start] >= x.ranks[x.offsets[target] + slot]:
            continue
        if (scan.has_critical(x, chain, start, start + 1)
                or scan.has_critical(x, target, slot, slot + 1)) == lane:
            return scan.description(scan.NODE_SWAP, x.ids[chain], x.ids[target],
                                    start, start + 1, slot, slot + 1)
    return scan.description(MORE)


@njit
def block_step(x, source, lane, c, work=256):
    # piece, length-2, start-offset, target-rank, turn, move-slot,
    # exchange-size-1, exchange-slot. Turn 0/1 preserves move/swap alternation.
    for _ in range(work):
        row = _piece(x, source, c[0])
        if row < 0:
            return scan.description(DONE)
        chain, anchor = x.row_chain[row], x.row_position[row]
        size = x.offsets[chain + 1] - x.offsets[chain]
        length = c[1] + 2
        if length >= (min(4, size) if lane else size):
            c[0] += 1
            _clear(c, 1)
            continue
        start = max(0, anchor - length + 1) + c[2]
        if start >= min(anchor + 1, size - length + 1):
            c[1] += 1
            _clear(c, 2)
            continue
        stop = start + length
        source_interval = scan.interval(x, chain, start, stop)
        critical = scan.has_critical(x, chain, start, stop)
        if (x.interval_slots[source_interval] != anchor or (not lane and critical)
                or c[3] >= x.ranked_chains.size):
            c[2] += 1
            _clear(c, 3)
            continue
        target = x.ranked_chains[c[3]]
        target_size = x.offsets[target + 1] - x.offsets[target]
        maximum = min(4, target_size) if lane else target_size
        if target == chain:
            c[3] += 1
            _clear(c, 4)
            continue
        if c[4] == 0:
            c[4] = 1
            if critical == lane and c[5] <= target_size:
                slot = c[5]
                c[5] += 1
                return scan.description(scan.BLOCK_MOVE, x.ids[chain], x.ids[target], start, stop, slot)
        current_size = c[6] + 1
        if current_size >= maximum:
            c[4] = 0
            if critical != lane or c[5] > target_size:
                c[3] += 1
                _clear(c, 4)
            continue
        slot, end = c[7], c[7] + current_size
        c[7] += 1
        if c[7] > target_size - current_size:
            c[6] += 1
            c[7] = 0
        if (critical or scan.has_critical(x, target, slot, end)) != lane:
            continue
        if current_size > 1 and (x.interval_ranks[source_interval], chain, start, stop) > (
                x.interval_ranks[scan.interval(x, target, slot, end)], target, slot, end):
            continue
        c[4] = 0
        return scan.description(scan.BLOCK_SWAP, x.ids[chain], x.ids[target], start, stop, slot, end)
    return scan.description(MORE)


@njit
def chain_step(x, family, chain, c, work=256):
    for _ in range(work):
        if family == scan.ORDER:
            if c[0] >= x.ids.size:
                return scan.description(DONE)
            position = c[0]
            c[0] += 1
            if position != chain and x.periods[position] == x.periods[chain]:
                return scan.description(scan.ORDER, x.ids[chain], x.ids[position], -1, -1, position)
        else:
            cut = c[0] + 1
            if cut >= x.offsets[chain + 1] - x.offsets[chain]:
                return scan.description(DONE)
            if c[1] > x.ids.size:
                c[0] += 1
                _clear(c, 1)
                continue
            if c[2] > x.ids.size:
                c[1] += 1
                c[2] = 0
                continue
            prefix, suffix = c[1], c[2]
            c[2] += 1
            if prefix != suffix:
                identity = -1 if x.ids.max() == np.iinfo(np.int64).max else x.ids.max() + 1
                return scan.description(scan.CUT, x.ids[chain], identity, cut, -1, prefix, suffix)
    return scan.description(MORE)


@njit
def reclaim_step(x, role, purpose, group, c, work=256):
    # chain, local position, mode (find/end/full/single), start, end, single.
    for _ in range(work):
        chain = c[0]
        if chain >= x.ids.size:
            return scan.description(DONE)
        base, length = x.offsets[chain], x.offsets[chain + 1] - x.offsets[chain]
        if c[2] == 0:
            if c[1] >= length:
                c[0] += 1
                _clear(c, 1)
                continue
            row = x.rows[base + c[1]]
            if role[row] == scan._VIRTUAL and purpose[row] == scan._BRIDGE and group[row] < 0:
                c[3], c[2] = c[1], 1
            else:
                c[1] += 1
        elif c[2] == 1:
            if c[1] < length:
                row = x.rows[base + c[1]]
                if role[row] == scan._VIRTUAL and purpose[row] == scan._BRIDGE and group[row] < 0:
                    c[1] += 1
                    continue
            c[4], c[5], c[2] = c[1], c[3], 2
        elif c[2] == 2:
            c[2] = 3 if c[4] - c[3] > 1 else 0
            return scan.description(scan.RECLAIM, x.ids[chain], x.ids[chain], c[3], c[4])
        else:
            position = c[5]
            c[5] += 1
            if c[5] >= c[4]:
                c[2] = 0
            return scan.description(scan.RECLAIM, x.ids[chain], x.ids[chain], position, position + 1)
    return scan.description(MORE)


@dataclass
class FamilyCursor:
    """Journal compact changes during prefetch; roll back only unconsumed suffixes.

    No candidate workspace, scan view or generator survives a slice. Entries
    store numeric loop coordinates and fair-source queue state only.
    """
    family: str
    lane: bool
    sources: tuple[int, ...]
    data: dict = field(default_factory=dict)
    journal: list = field(default_factory=list, repr=False)

    def put(self, key, value):
        previous = self.data.get(key)
        if key not in self.data or previous != value:
            self.journal.append((key, key in self.data, previous))
            self.data[key] = value

    def rollback(self, marker):
        if not 0 <= marker <= len(self.journal):
            raise ValueError("invalid refinement cursor checkpoint")
        for key, existed, old in reversed(self.journal[marker:]):
            if existed:
                self.data[key] = old
            else:
                self.data.pop(key, None)
        del self.journal[marker:]

    def commit(self):
        self.journal.clear()

    @property
    def complete(self):
        return self.data.get(("complete",), False)

    def _raw(self, key, function, *args):
        if self.data.get(("done", *key), False):
            return scan.description(DONE)
        values = np.array(self.data.get(key, (0,) * _CURSOR_SIZE), np.int64)
        result = function(*args, values)
        self.put(key, tuple(map(int, values)))
        if int(result[scan.ACTION]) == DONE:
            self.put(("done", *key), True)
        return result

    def step(self, x, columns, rules, roles, purposes, groups):
        """Bounded raw progress. The caller polls its phase before every call."""
        if self.complete:
            return scan.description(DONE)
        if self.family == "reclaim":
            result = self._raw(("reclaim",), reclaim_step, x, roles, purposes, groups)
            if result[scan.ACTION] == DONE:
                self.put(("complete",), True)
            return result
        if not self.sources or self.data.get(("exhausted_count",), 0) == len(self.sources):
            self.put(("complete",), True)
            return scan.description(DONE)
        index = self.data.get(("index",), 0)
        source = self.sources[index]
        if self.data.get(("source_done", source), False):
            self.put(("index",), (index + 1) % len(self.sources))
            self.put(("quantum",), 0)
            return scan.description(MORE)
        if self.family == "intra":
            result = self._raw(("intra", source), intra_step, x, source, self.lane)
        elif self.family == "block":
            result = self._raw(("block", source), block_step, x, source, self.lane)
        elif self.family == "node":
            turn = self.data.get(("turn", source), 0)
            name = "move" if turn == 0 else "swap"
            args = (x, columns, rules, source, self.lane) if turn == 0 else (x, source, self.lane)
            result = self._raw((name, source), node_move_step if turn == 0 else node_exchange_step, *args)
            if result[scan.ACTION] != MORE:
                self.put(("turn", source), 1 - turn)
            if result[scan.ACTION] == DONE and not self.data.get(("done", "swap" if turn == 0 else "move", source), False):
                return scan.description(MORE)
        else:
            chains = scan.source_chains(x, source, self.lane)
            position = self.data.get(("source_chain", source), 0)
            if position >= len(chains):
                result = scan.description(DONE)
            else:
                chain = int(chains[position])
                result = self._raw(("chain", chain), chain_step, x,
                                   scan.CUT if self.family == "cut" else scan.ORDER, chain)
                if result[scan.ACTION] == DONE:
                    self.put(("source_chain", source), position + 1)
                    return scan.description(MORE)
        action = int(result[scan.ACTION])
        if action == DONE:
            self.put(("source_done", source), True)
            self.put(("exhausted_count",), self.data.get(("exhausted_count",), 0) + 1)
            self.put(("index",), (index + 1) % len(self.sources))
            self.put(("quantum",), 0)
            return scan.description(MORE)
        if action >= 0:
            result[scan.OWNER] = source
            quantum = self.data.get(("quantum",), 0) + 1
            self.put(("quantum",), quantum % 4)
            if quantum == 4:
                self.put(("index",), (index + 1) % len(self.sources))
        return result
