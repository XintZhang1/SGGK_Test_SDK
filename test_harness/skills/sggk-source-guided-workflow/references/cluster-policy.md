# Source-Guided Cluster Policy

This is the host-owned expansion policy used by the integrated pipeline when an
untrusted `cluster_seed` candidate should become a compact cluster. A seed file
outside the pipeline is prompt context or a checked-in deterministic fixture,
not an accepted model output.

## Bands

Always emit these contact bands:

- `overlap_topo`: `contact_value - topo_tol`
- `overlap_geom`: `contact_value - geom_tol`
- `exact`: `contact_value`
- `gap_geom`: `contact_value + geom_tol`
- `gap_topo`: `contact_value + topo_tol`

If a source literal threshold is relevant, emit `source_<name>` at `contact_value + literal`.

## Boolean Oracles

For subtraction, default `expectations.result_bodies.min` to `1` for every band.

For intersection, default:

- `overlap_topo`: `1`
- `overlap_geom`: `0`
- `exact`: `0`
- `gap_geom`: `0`
- `gap_topo`: `0`
- source literal bands: caller-specified, default `0`

The fixed gate and later qualification must check these defaults against the
source predicate before any candidate bug report is promoted for review.

## Siblings

Add at most two siblings per seed unless a larger campaign is explicitly requested:

- `generated_sibling`: changes the target/tool builders to generated topology, such as extrude, sweep, thicken, revolve, or pre-boolean.
- `large_coordinate_sibling`: shifts the same contact under `max_model_size`; keep all coordinates comfortably below `5e5`.

## Determinism

- Keep case IDs stable.
- Preserve operation `id` values.
- Do not randomize dimensions.
- Keep `topo_track=false` for broad tolerance clusters unless localization is the goal.
- Run geometry audit after execution to confirm bands are not accidental duplicates.
