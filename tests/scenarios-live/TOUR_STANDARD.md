# Tour quality standard

What "a good tour" means — measurable, per stop. Encoded in the
`gev_*_tour*.yaml` scenario expectations; scored by scenario-live.py and
trended by scenario-benchmark.py (suite `casting`).

## Per-stop criteria

| # | Standard | Check |
|---|----------|-------|
| 1 | The fly actually happened — `gev_command` delivered ≥1, no `no GEV clients` / relay errors | `calls_any` + `result_not_contains` |
| 2 | The map is where it claims to be — coordinates or query match the announced stop | arg inspection in transcript review |
| 3 | The visual matches the stop — PiP/pane camera frame is that location's own cam, not a leftover | scenario per-stop frame swap |
| 4 | Narration exists and is on-topic — `vcast_say` fired with area + camera context | `calls_any: [vcast_say]` (implicit in prompt turns) |
| 5 | Claims match actions — no refusal without an attempt (`no_unattempted_refusal`), no success claim over a failed result | runner check |
| 6 | Motion is smooth — long legs use `fly_route` (drawn path, accel/decel ramp); country-scale hops use `fly_to_location` | scenario design |
| 7 | Honest failure — dead cam/failed fly is said as failed, never painted as shown | transcript review + `result_not_contains` |

## Tour-level

- Display state respected: conflicting camwall zones stopped first,
  screen split/pip set before flying, cleanup at the end.
- Snapshot verification mid-tour (vcast_snapshot — captures the GEV
  canvas; PiP panes live outside the canvas and verify via vcast_list
  `panes` count instead).
- Score = passed expectations / total, per scenario file; the weekly
  casting timer trends it.

## Known limits (don't score against these)

- `vcast_snapshot` only captures the main canvas — the floating pane is
  invisible to it.
- Direct iTIC/DOH cams are flaky by upstream nature — tours use cached
  camwall frames; a cam refresh miss is upstream, not Ada.
