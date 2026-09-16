# Completed revision experiments

Reference: webagents1.a.pdf. Original trials and new runs remain separate.
All new cells have ten allocated valid trials; timeouts count as failures. Infrastructure attempts are excluded.

## Separation extension

| Layout | gpt-5.6-terra | Kimi-K2.6 | gpt-5.4 | gpt-5-mini |
|---|---:|---:|---:|---:|
| canvas-inline-bar | 0/20 | 0/20 | 0/20 | 0/20 |
| canvas-inline-two-spaces | 0/20 | 0/20 | 0/20 | 0/20 |
| canvas-inline-gap-40px | 0/20 | 10/20 | 18/20 | 10/20 |
| canvas-separate-line | 13/20 | 18/20 | 10/20 | 13/20 |
| canvas-opposite-corner | 20/20 | 18/20 | 13/20 | 13/20 |
| dom-inline-bar | 0/20 | 0/20 | 0/20 | 0/20 |

The original two deployments were measured September 4; the additional two were measured in this revision. The fixture hash, task instructions, model-facing prompt and budgets match. This is a deployment comparison across run windows.

## Generalization and DOM separation

Three self-authored interface shells (table, cards, form) cross measured text-box edge gaps of 8/40/96 pixels with DOM graph distances of 0/4 edges. There are 1,440 trials. DOM variants are visually matched negative controls. The vision agent receives no DOM. These shells broaden layout coverage but remain controlled synthetic tasks.

| Model | Exact success | Delimitation errors | No answer |
|---|---:|---:|---:|
| gpt-5.6-terra | 108/360 | 150/360 | 3/360 |
| Kimi-K2.6 | 116/360 | 100/360 | 119/360 |
| gpt-5.4 | 115/360 | 102/360 | 82/360 |
| gpt-5-mini | 41/360 | 198/360 | 86/360 |

Delimitation is measured from the final read answer or last attempted typed content. Correct content with a failed submission is kept separate from delimitation. No-answer outcomes are reported separately; a lower delimitation rate alone is not evidence of improved task success. Per-cell CSVs retain task, deployment, layout, gap, and DOM boundary.

| Model | Error rate: 8px | Error rate: 96px | Change (pp), paired 95% interval | Hidden DOM change (pp), paired 95% interval |
|---|---:|---:|---|---|
| gpt-5.6-terra | 119/120 | 12/120 | -89.2 [-93.3, -85.0] | +0.0 [-3.3, +3.3] |
| Kimi-K2.6 | 100/120 | 0/120 | -83.3 [-89.2, -77.5] | -2.2 [-6.7, +2.2] |
| gpt-5.4 | 95/120 | 0/120 | -79.2 [-85.8, -71.7] | -1.1 [-5.6, +3.3] |
| gpt-5-mini | 92/120 | 27/120 | -54.2 [-63.3, -45.8] | +3.3 [-4.4, +11.1] |

## Paired format intervention

The baseline and explicit-format arms use the same inline fixture, 768x768 viewport, eight-action/90-second budget, and paired trials. The format prompt describes the token shape and excludes adjacent metadata without supplying the answer. The inline canvas is 730px wide to fit the calibrated viewport; the contemporaneous baseline is the comparator.

| Model | Baseline | Format | Change (pp) | Paired bootstrap 95% interval |
|---|---:|---:|---:|---|
| gpt-5.6-terra | 0/20 | 20/20 | +100.0 | +100.0 to +100.0 |
| Kimi-K2.6 | 0/20 | 15/20 | +75.0 | +60.0 to +90.0 |
| gpt-5.4 | 1/20 | 11/20 | +50.0 | +35.0 to +65.0 |
| gpt-5-mini | 0/20 | 11/20 | +55.0 | +50.0 to +65.0 |

## Calibrated, randomized iframe rerun

560 trials: four deployments, two tasks, seven unique conditions, ten repeats. Depth zero is shared across origins. Positions are randomized by task/repeat and paired across deployments, origins and depths. Each screenshot is 768x768 at device scale 1; the prompt states the coordinate dimensions. Privileged pixel-center click/submit checks pass for every unique fixture. Preflight asserts byte-identical screenshots across depths and origins at each paired position.

| Model | Read success | Copy success |
|---|---:|---:|
| gpt-5.6-terra | 70/70 | 70/70 |
| Kimi-K2.6 | 70/70 | 56/70 |
| gpt-5.4 | 70/70 | 44/70 |
| gpt-5-mini | 70/70 | 10/70 |

Depth comparisons are paired against the shared flat cell; complete estimates and intervals are in iframe-paired-effects.csv. A nonsignificant difference is not an equivalence result. Ten paired repeats per task/model limit sensitivity to small effects. This removes the deterministic fixture scale/position confound; it does not guarantee that models localize every field correctly.

## Statistical and provenance notes

- Cell rates use Wilson 95% intervals. Paired differences resample complete randomized-position repeat blocks within task strata 10,000 times with a fixed seed.
- A paired bootstrap interval can be degenerate when all observed pairs agree. This reflects resampling the observed sample, not certainty about future trials; use the cell Wilson intervals as well.
- Paired tests are exploratory and are not adjusted for multiple comparisons; do not use isolated p-values as confirmatory evidence.
- Generalization snapshots use the original scorer selector, which does not identify the new `pair` element; availability is established independently from saved text-range boxes and viewport checks, not those legacy reachability flags.
- The old variance decomposition is retained in the appendix as a confounded descriptive audit. The new vision-only runs do not support a balanced architecture-by-model variance decomposition.
- Trial archives retain exact screenshots, prompts, responses, actions, timings and token counts. The new scripts and results are hash-bound.


## Visible natural-boundary controls

An additional 240 trials place Source and Reference in separate table columns, neighboring cards, or labeled form fields. The same tokens, task/repeat positions, model roster and ten-repeat allocation are retained. Measured pixel and DOM distances are saved per trial. This is a compound layout/label/boundary control, not an isolated causal effect of DOM distance.

| Model | Exact success | Delimitation error |
|---|---:|---:|
| gpt-5.6-terra | 59/60 | 0/60 |
| Kimi-K2.6 | 45/60 | 0/60 |
| gpt-5.4 | 47/60 | 0/60 |
| gpt-5-mini | 33/60 | 0/60 |

## Exact prompt additions

Format intervention:

> Treat the adjacent reference/control marker as a separate value. The Source value has the format five uppercase letters, a hyphen, and four digits. Return or enter only that value, without the label, separator, marker, or explanation.

Coordinate calibration (both intervention arms and all new matched-layout/iframe trials):

> The screenshot is exactly 768 pixels wide and 768 pixels tall. Use those pixel coordinates for actions.
