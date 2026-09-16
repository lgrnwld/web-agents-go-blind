# Reviewer follow-up audit

All results here are provider-free derivations from the immutable, receipted
`vision-full-scan-v2` archives.

## Three canvas scoring criteria

Across the 40 vision canvas trials:

| Criterion | Passing trials |
| --- | ---: |
| Exact match after outer-whitespace stripping | 0/40 |
| Target appears as a substring | 40/40 |
| Target appears and the task-specific marker token is absent | 0/40 |

The third criterion is deliberately between exact-match and substring scoring:
it permits explanatory wrapper text, but rejects the specific observed failure
where the response includes the benchmark marker. Trial-level and stratified
results are in `canvas-scoring-trials.csv` and `canvas-scoring-summary.csv`.

## Table 4 audit

The requested 40 cells comprise the 20 `gpt-5.6-terra` copy failures at iframe
depth 1 (same and cross origin) and the 20 read failures at depth 3.

The depth-1 copy failures are coordinate-grounding artifacts. In a provider-free
reproduction of the exact fixture at 1280x900, the destination label occupies
y=195.75..223.75 and the input occupies y=235.75..291.75. Every first type
action used y in [224, 225, 226]; consequently every click fell in the 12-pixel gap
between label and field. The action API reported only that the click/type call
executed, not that an editable control received text. Nineteen trials exhausted
30 steps and one exhausted after 14 recorded actions; no form submission was
recorded. At depth 2, the corresponding biased y=293..295 happens to fall inside
the associated label at y=277.625..305.625, and clicking that label focuses the
field. The apparent recovery is therefore accidental and cannot be interpreted
as a depth effect.

The 20 depth-3 read trials used only 2 distinct initial screenshots
(one per origin-labelled condition, each repeated ten times) and all emitted
the same transcription, `AI PHA-7391`, for the fully visible `ALPHA-7391` source.
The target was at y=325.5..343.5, far inside the 900-pixel viewport. This is a
deterministic raster/OCR-position effect, not below-fold loss. Likewise, the 20
depth-1 copy trials used 2 distinct initial screenshots. Repeated
trials therefore estimate repeated model behavior on identical pixels; they do
not independently vary layout.

Conclusion: Table 4 does not identify a depth mechanism. The manuscript should
remove the depth interpretation, identify the coordinate contract and absolute
layout as confounds, and either rerun vision with calibrated coordinates and
position-matched fixtures or present these cells only as a runner audit.

## Blind recoding packet

`blind-coding-packet/coding-sheet.csv` contains 40 randomized opaque case IDs.
Each case directory contains the initial screenshot plus de-identified response,
action, and checker evidence. The model, condition, repeat, run ID, original
coding, and aggregate result are excluded from the packet. The separate
`blind-coding-key.csv` should remain hidden from the second coder.
