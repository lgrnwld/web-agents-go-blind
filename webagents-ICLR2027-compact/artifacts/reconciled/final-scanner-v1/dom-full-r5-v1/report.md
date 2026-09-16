# DOM-extraction light sweep: dom-full-r5-v1-reachability-v2

Verdict: **complete**

Primary model: `foundry-gpt-5-6-terra`
Primary cells: 22/22
Additional models: `foundry-kimi-k2-6`
Confirmation cells: 220/220
Divergent confirmations: 0
Inherited valid trials: 0
Framework-surprise candidates: 160

## Primary-model results

| Task | Condition | N | Reachability | Success | Reused controls |
| --- | --- | ---: | ---: | ---: | ---: |
| copy-known-value r5 | level-0 | 10/10 | 1.000 | 1.000 | 3 |
| copy-known-value r5 | iframe-same-depth-1 | 10/10 | 1.000 | 1.000 | 0 |
| copy-known-value r5 | iframe-same-depth-2 | 10/10 | 1.000 | 1.000 | 0 |
| copy-known-value r5 | iframe-same-depth-3 | 10/10 | 1.000 | 1.000 | 0 |
| copy-known-value r5 | iframe-cross-depth-1 | 10/10 | 1.000 | 1.000 | 0 |
| copy-known-value r5 | iframe-cross-depth-2 | 10/10 | 1.000 | 1.000 | 0 |
| copy-known-value r5 | iframe-cross-depth-3 | 10/10 | 1.000 | 1.000 | 0 |
| copy-known-value r5 | shadow-open | 10/10 | 1.000 | 1.000 | 0 |
| copy-known-value r5 | shadow-closed | 10/10 | 1.000 | 1.000 | 0 |
| copy-known-value r5 | shadow-nested-open-open | 10/10 | 1.000 | 1.000 | 0 |
| copy-known-value r5 | rendered-canvas | 10/10 | 0.000 | 0.000 | 0 |
| read-known-value r2 | level-0 | 10/10 | 1.000 | 1.000 | 3 |
| read-known-value r2 | iframe-same-depth-1 | 10/10 | 1.000 | 1.000 | 0 |
| read-known-value r2 | iframe-same-depth-2 | 10/10 | 1.000 | 1.000 | 0 |
| read-known-value r2 | iframe-same-depth-3 | 10/10 | 1.000 | 1.000 | 0 |
| read-known-value r2 | iframe-cross-depth-1 | 10/10 | 1.000 | 1.000 | 0 |
| read-known-value r2 | iframe-cross-depth-2 | 10/10 | 1.000 | 1.000 | 0 |
| read-known-value r2 | iframe-cross-depth-3 | 10/10 | 1.000 | 1.000 | 0 |
| read-known-value r2 | shadow-open | 10/10 | 1.000 | 1.000 | 0 |
| read-known-value r2 | shadow-closed | 10/10 | 1.000 | 1.000 | 0 |
| read-known-value r2 | shadow-nested-open-open | 10/10 | 1.000 | 1.000 | 0 |
| read-known-value r2 | rendered-canvas | 10/10 | 0.000 | 0.000 | 0 |

## Additional-model repeated results

| Task | Model | Condition | N | Reachability | Success |
| --- | --- | --- | ---: | ---: | ---: |
| copy-known-value r5 | foundry-kimi-k2-6 | level-0 | 10/10 | 1.000 | 1.000 |
| copy-known-value r5 | foundry-kimi-k2-6 | iframe-same-depth-1 | 10/10 | 1.000 | 1.000 |
| copy-known-value r5 | foundry-kimi-k2-6 | iframe-same-depth-2 | 10/10 | 1.000 | 1.000 |
| copy-known-value r5 | foundry-kimi-k2-6 | iframe-same-depth-3 | 10/10 | 1.000 | 1.000 |
| copy-known-value r5 | foundry-kimi-k2-6 | iframe-cross-depth-1 | 10/10 | 1.000 | 1.000 |
| copy-known-value r5 | foundry-kimi-k2-6 | iframe-cross-depth-2 | 10/10 | 1.000 | 1.000 |
| copy-known-value r5 | foundry-kimi-k2-6 | iframe-cross-depth-3 | 10/10 | 1.000 | 1.000 |
| copy-known-value r5 | foundry-kimi-k2-6 | shadow-open | 10/10 | 1.000 | 1.000 |
| copy-known-value r5 | foundry-kimi-k2-6 | shadow-closed | 10/10 | 1.000 | 1.000 |
| copy-known-value r5 | foundry-kimi-k2-6 | shadow-nested-open-open | 10/10 | 1.000 | 1.000 |
| copy-known-value r5 | foundry-kimi-k2-6 | rendered-canvas | 10/10 | 0.000 | 0.000 |
| read-known-value r2 | foundry-kimi-k2-6 | level-0 | 10/10 | 1.000 | 1.000 |
| read-known-value r2 | foundry-kimi-k2-6 | iframe-same-depth-1 | 10/10 | 1.000 | 1.000 |
| read-known-value r2 | foundry-kimi-k2-6 | iframe-same-depth-2 | 10/10 | 1.000 | 1.000 |
| read-known-value r2 | foundry-kimi-k2-6 | iframe-same-depth-3 | 10/10 | 1.000 | 1.000 |
| read-known-value r2 | foundry-kimi-k2-6 | iframe-cross-depth-1 | 10/10 | 1.000 | 1.000 |
| read-known-value r2 | foundry-kimi-k2-6 | iframe-cross-depth-2 | 10/10 | 1.000 | 1.000 |
| read-known-value r2 | foundry-kimi-k2-6 | iframe-cross-depth-3 | 10/10 | 1.000 | 1.000 |
| read-known-value r2 | foundry-kimi-k2-6 | shadow-open | 10/10 | 1.000 | 1.000 |
| read-known-value r2 | foundry-kimi-k2-6 | shadow-closed | 10/10 | 1.000 | 1.000 |
| read-known-value r2 | foundry-kimi-k2-6 | shadow-nested-open-open | 10/10 | 1.000 | 1.000 |
| read-known-value r2 | foundry-kimi-k2-6 | rendered-canvas | 10/10 | 0.000 | 0.000 |

## Interpretation boundary

Both model arms have N=10 per task-condition cell with Wilson intervals; the crossed DOM design supports a fixed two-model robustness comparison. Reachability was rescored from immutable archives using initial-task-page-marker-v2.
