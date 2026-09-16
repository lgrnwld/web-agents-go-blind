# Core-grid analysis v1

All four architecture sweeps are complete and balanced: 440 valid outcomes per class (1,760 total), with 10 trials in every task × model × condition cell.

## Primary outputs

- `cell-estimates.csv`: reachability, task success, self-reported blindness, joint outcomes, Wilson 95% intervals, and cost telemetry.
- `headline-curves.csv`: pooled architecture-class curves (N=40 per plotted point).
- `curve-summaries.csv`: normalized AUC, mean drop, total drop, largest single-level drop, the preregistered cliff ratio, and bootstrap intervals.
- `k-of-n-reliability.csv`: observed k-of-N status and the binomial Wilson interval for every cell.
- `variance-decomposition.csv`: architecture, model, and architecture×model curve-shape shares with stratified bootstrap intervals.
- `cost-by-level.csv`: actions, tokens, and tokens/action. Provider billing cost is unavailable and is not imputed.

## Overall variance shares

### Reachable

- architecture: 100.0% (bootstrap 95% CI 100.0%–100.0%)
- model: 0.0% (bootstrap 95% CI 0.0%–0.0%)
- interaction: 0.0% (bootstrap 95% CI 0.0%–0.0%)

### Task Success

- architecture: 48.7% (bootstrap 95% CI 43.0%–54.9%)
- model: 13.9% (bootstrap 95% CI 11.2%–17.0%)
- interaction: 37.4% (bootstrap 95% CI 31.7%–43.1%)

