"""Candidate-private run facts; only the accepted snapshot survives a candidate."""

from itertools import groupby

from .contracts import sum_weights


class RuleRuns:
    def __init__(self, previous=None):
        self.previous = previous
        self.keys = {}
        self.weights = {}
        self.key_hits = 0
        self.weight_hits = 0

    def groups(self, rule, nodes, group_key):
        def key(item):
            _, node = item
            identity = (id(rule), id(node))
            old = None if self.previous is None else self.previous.keys.get(identity)
            if old is not None and old[0] is rule and old[1] is node:
                value = old[2]
                self.key_hits += 1
            else:
                value = group_key(item)
            self.keys[identity] = (rule, node, value)
            return value

        return groupby(enumerate(nodes), key)

    def total(self, members):
        nodes = tuple(node for _, node in members)
        identity = tuple(id(node) for node in nodes)
        old = None if self.previous is None else self.previous.weights.get(identity)
        if old is not None and all(a is b for a, b in zip(nodes, old[0])):
            total = old[1]
            self.weight_hits += 1
        else:
            total = sum_weights(node.weight for node in nodes)
        self.weights[identity] = (nodes, total)
        return total

    def detach(self):
        # Do not retain a chain of all historical accepted plans.
        self.previous = None
