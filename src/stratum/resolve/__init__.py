"""The resolve stage: the block read path, masks, the gather, scoring, the snapshot writer
(12 section 2, 04, 13).

    gather            12 section 2 step 14 - sensor window -> block through a GLT window
    read_observation  steps 5-15 for one granule over one block -> block-space ObsWindow
    resolve_block     one work item: streaming argmax, snapshot directory, cache key
    write_snapshot / read_snapshot / stack_snapshots   snapshot IO (13, 11 section 8)
    PlanContext       what a worker needs beyond the work item (plan section 4)
"""
from __future__ import annotations

from stratum.resolve.block import (
    Resolved,
    ResolveItem,
    candidates,
    glt_key_for,
    observation_inputs,
    reached_observations,
    resolve_block,
    resolve_window,
    snapshot_key,
    window_lonlat_bounds,
)
from stratum.resolve.context import (
    AUX_NOT_IN_SLICE,
    AliasBinding,
    NullAux,
    PlanContext,
    PluginBinding,
    RoleBinding,
    plugin_version,
)
from stratum.resolve.gather import Gathered, gather
from stratum.resolve.observation import (
    ObsContext,
    Observation,
    apply_remap,
    block_coords,
    read_observation,
)
from stratum.resolve.snapshot import (
    CATEGORICAL_DTYPE,
    CATEGORICAL_NODATA,
    CONTINUOUS_DTYPE,
    SCORE_NAME,
    VALID_NAME,
    empty_layer,
    layer_dtype,
    layer_nodata,
    read_snapshot,
    stack_snapshots,
    write_snapshot,
)

__all__ = [
    "AUX_NOT_IN_SLICE", "CATEGORICAL_DTYPE", "CATEGORICAL_NODATA", "CONTINUOUS_DTYPE",
    "SCORE_NAME", "VALID_NAME", "AliasBinding", "Gathered", "NullAux", "ObsContext",
    "Observation", "PlanContext", "PluginBinding", "ResolveItem", "Resolved", "RoleBinding",
    "apply_remap", "block_coords", "candidates", "empty_layer", "gather", "glt_key_for",
    "layer_dtype", "layer_nodata", "observation_inputs", "plugin_version", "reached_observations",
    "read_observation", "read_snapshot", "resolve_block", "resolve_window", "snapshot_key",
    "stack_snapshots", "window_lonlat_bounds", "write_snapshot",
]
