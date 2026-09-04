"""Private, deterministic scanning for the post-repair width optimization phase."""

from itertools import zip_longest

from .contracts import SearchStopReason
from .neighborhoods import _validate_search


def _alternate_recipes(first, second):
    """Interleave lightweight move/exchange descriptions, never built candidates."""
    missing = object()
    for pair in zip_longest(first, second, fillvalue=missing):
        for recipe in pair:
            if recipe is not missing:
                yield recipe


def _scan_width_batch(state, context, recipes, try_recipe, allowance):
    """Return (accepted, exhausted); consume the shared quota before business work."""
    budget = context.factory.budget
    for _ in range(allowance):
        if not budget.allows_search():
            return False, False
        try:
            recipe = next(recipes)
        except StopIteration:
            return False, True
        if not budget.consume_candidate_check():
            return False, False
        if try_recipe(state, context, recipe):
            return True, False
    return False, False


def _scan_width_families(state, context, family_factories, try_recipe):
    """Share remaining checks, resume rejected scans and restart after a real commit.

    Factories only enumerate index/range/placement recipes. The private callback
    builds one candidate and delegates acceptance to the shared candidate entry.
    Only this scanner consumes the quota; no second search state is created.
    Natural-completion continuation belongs to the eventual phase entry, not here.
    """
    _validate_search(state, context)
    budget = context.factory.budget
    while budget.allows_search():
        pending = [iter(factory(state, context)) for factory in family_factories]
        accepted = False
        while pending and budget.allows_search():
            remaining = budget.candidate_check_limit - budget.candidate_check_count
            allowance = max(1, remaining // len(pending))
            unfinished = []
            for recipes in pending:
                accepted, exhausted = _scan_width_batch(
                    state, context, recipes, try_recipe, allowance
                )
                if accepted or budget.must_stop:
                    break
                if not exhausted:
                    unfinished.append(recipes)
            if accepted or budget.must_stop:
                break
            pending = unfinished
        if not accepted:
            if budget.allows_search():
                budget.stop_reason = SearchStopReason.LOCAL_SEARCH_COMPLETE
            return state
        # Accepted edits invalidate every previous iterator and endpoint summary.
    return state
