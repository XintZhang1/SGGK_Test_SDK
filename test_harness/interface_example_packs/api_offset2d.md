# api_offset2d Flat Recipes

Use exactly one of `offset2d_distance` or `offset2d_distances`, and provide a
non-empty `offset2d_path`. Each path segment is either a `line` with `start`
and `end`, or an `arc` with `center`, positive `radius`, `start_angle`, and
`end_angle`. Numeric expressions such as `pi` and `tau` are accepted.

Supported options include `offset2d_connect_type`,
`offset2d_allow_crv_degenerated`, `offset2d_allow_crv_reversed`,
`offset2d_allow_self_intersections`, `offset2d_extend_type`,
`offset2d_dist_tol`, and `offset2d_angle_tol`.

Use `expectations.offset2d_status`, `offset2d_result_path_count`, or
`offset2d_result_paths` to distinguish successful output from an expected SDK
diagnostic such as `CanNotConnect` or `CrvDegenToPoint`.
