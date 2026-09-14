"""Keep the diagnostic's material filter and exact output independent of a full run."""

from dataclasses import replace
import json

import pytest

from apsgo_scheduler.core.contracts import CoreCandidateSnapshot
from apsgo_scheduler.core.model import VirtualPurpose
from tests.core.search.test_width_optimization_guard import bridge, guard_case
from tools.probe_critical_delivery_witnesses import _write_json, ordinary_bridge, move_recipe


def test_only_unrelated_ordinary_generated_bridge_is_a_deletion_witness():
    state, context = guard_case()
    node = bridge(state, context)
    assert ordinary_bridge(node)
    assert not ordinary_bridge(state.current_plan.chains[0].nodes[0])
    for purpose in (VirtualPurpose.WEIGHT_FILL, VirtualPurpose.SPLIT_SEPARATOR):
        protected = replace(node, virtual_lineage=replace(node.virtual_lineage,
                            purpose=purpose, related_partition_id="partition" if purpose is VirtualPurpose.SPLIT_SEPARATOR else None))
        assert not ordinary_bridge(protected)


def test_snapshot_output_uses_existing_exact_carrier_serialization(tmp_path):
    state, _ = guard_case()
    path = tmp_path / "snapshot.json"
    _write_json(path, CoreCandidateSnapshot(state.current_plan, state.current_evaluation))
    value = json.loads(path.read_text())
    assert value["plan"]["chains"][0]["chain_id"] == state.current_plan.chains[0].chain_id
    with pytest.raises(FileExistsError):
        _write_json(path, {})


def test_witness_cannot_silently_use_a_different_source_plan():
    state, _ = guard_case()
    with pytest.raises(StopIteration):
        move_recipe(state.current_plan)
