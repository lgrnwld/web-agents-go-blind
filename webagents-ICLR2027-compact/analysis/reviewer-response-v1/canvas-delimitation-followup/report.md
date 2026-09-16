# Canvas delimitation follow-up

All estimates below use final non-infrastructure trials; complete timeout transcripts count as task failures, matching the core sweep. Intervals are two-sided Wilson 95% intervals.

| Layout | Exact | Target present, marker absent | Substring |
|---|---:|---:|---:|
| canvas-inline-bar | 0/40 (0.0--8.8%) | 0/40 (0.0--8.8%) | 37/40 (80.1--97.4%) |
| canvas-inline-two-spaces | 0/40 (0.0--8.8%) | 0/40 (0.0--8.8%) | 35/40 (73.9--94.5%) |
| canvas-inline-gap-40px | 10/40 (14.2--40.2%) | 10/40 (14.2--40.2%) | 29/40 (57.2--83.9%) |
| canvas-separate-line | 31/40 (62.5--87.7%) | 31/40 (62.5--87.7%) | 37/40 (80.1--97.4%) |
| canvas-opposite-corner | 38/40 (83.5--98.6%) | 38/40 (83.5--98.6%) | 38/40 (83.5--98.6%) |
| dom-inline-bar | 0/40 (0.0--8.8%) | 0/40 (0.0--8.8%) | 40/40 (91.2--100.0%) |

## Shape checks

- Exact-match rates are monotone nondecreasing across the five ordered canvas layouts: True.
- Target-without-marker rates are monotone nondecreasing: True.
- Final records excluded for infrastructure status: 0.

The DOM inline-bar row is a layout control, not a sixth point on the separation ordering.
