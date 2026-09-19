# Instance variation: completed results

All 880 allocated trials are valid. There are 20 independent task/instance blocks: ten new instances for each of two tasks, paired across four models and eleven conditions.

Targets, markers, positions, fonts, and spacing vary across instances. The original token shape is retained. No generic-instruction control was run. Model failure and timeout remain outcomes; only infrastructure failures are retried.

| Deployment | Delimitation: 8px | Delimitation: 96px | Delimitation: labeled | Canvas success: baseline | Canvas success: format |
|---|---:|---:|---:|---:|---:|
| gpt-5.6-terra | 55/60 | 6/60 | 1/60 | 0/20 | 19/20 |
| Kimi-K2.6 | 51/60 | 0/60 | 0/60 | 0/20 | 15/20 |
| gpt-5.4 | 48/60 | 0/60 | 0/60 | 0/20 | 12/20 |
| gpt-5-mini | 49/60 | 0/60 | 0/60 | 0/20 | 11/20 |

## Paired effects

| Deployment | Contrast | Endpoint | Change (percentage points) | Paired 95% bootstrap interval |
|---|---|---|---:|---:|
| gpt-5.6-terra | pixel-gap | delimitation_error | -81.7 | [-90.0, -73.3] |
| gpt-5.6-terra | pixel-gap | exact_match | +80.0 | [+70.0, +88.3] |
| gpt-5.6-terra | pixel-gap | no_answer | +0.0 | [+0.0, +0.0] |
| gpt-5.6-terra | labeled-components | delimitation_error | -90.0 | [-96.7, -81.7] |
| gpt-5.6-terra | labeled-components | exact_match | +90.0 | [+83.3, +96.7] |
| gpt-5.6-terra | labeled-components | no_answer | +0.0 | [+0.0, +0.0] |
| gpt-5.6-terra | format-instruction | delimitation_error | -95.0 | [-100.0, -85.0] |
| gpt-5.6-terra | format-instruction | exact_match | +95.0 | [+85.0, +100.0] |
| gpt-5.6-terra | format-instruction | no_answer | +0.0 | [+0.0, +0.0] |
| Kimi-K2.6 | pixel-gap | delimitation_error | -85.0 | [-93.3, -75.0] |
| Kimi-K2.6 | pixel-gap | exact_match | +70.0 | [+61.7, +78.3] |
| Kimi-K2.6 | pixel-gap | no_answer | +16.7 | [+8.3, +25.0] |
| Kimi-K2.6 | labeled-components | delimitation_error | -85.0 | [-93.3, -75.0] |
| Kimi-K2.6 | labeled-components | exact_match | +91.7 | [+83.3, +100.0] |
| Kimi-K2.6 | labeled-components | no_answer | -3.3 | [-18.3, +11.7] |
| Kimi-K2.6 | format-instruction | delimitation_error | -90.0 | [-100.0, -75.0] |
| Kimi-K2.6 | format-instruction | exact_match | +75.0 | [+55.0, +90.0] |
| Kimi-K2.6 | format-instruction | no_answer | +0.0 | [-15.0, +15.0] |
| gpt-5.4 | pixel-gap | delimitation_error | -80.0 | [-88.3, -71.7] |
| gpt-5.4 | pixel-gap | exact_match | +73.3 | [+61.7, +85.0] |
| gpt-5.4 | pixel-gap | no_answer | +5.0 | [-3.3, +15.0] |
| gpt-5.4 | labeled-components | delimitation_error | -80.0 | [-88.3, -71.7] |
| gpt-5.4 | labeled-components | exact_match | +78.3 | [+70.0, +86.7] |
| gpt-5.4 | labeled-components | no_answer | +0.0 | [-8.3, +10.0] |
| gpt-5.4 | format-instruction | delimitation_error | -75.0 | [-90.0, -60.0] |
| gpt-5.4 | format-instruction | exact_match | +60.0 | [+45.0, +75.0] |
| gpt-5.4 | format-instruction | no_answer | +10.0 | [-15.0, +30.0] |
| gpt-5-mini | pixel-gap | delimitation_error | -81.7 | [-88.3, -75.0] |
| gpt-5-mini | pixel-gap | exact_match | +66.7 | [+55.0, +80.0] |
| gpt-5-mini | pixel-gap | no_answer | -10.0 | [-20.0, +0.0] |
| gpt-5-mini | labeled-components | delimitation_error | -81.7 | [-88.3, -75.0] |
| gpt-5-mini | labeled-components | exact_match | +73.3 | [+65.0, +81.7] |
| gpt-5-mini | labeled-components | no_answer | -11.7 | [-18.3, -5.0] |
| gpt-5-mini | format-instruction | delimitation_error | -90.0 | [-100.0, -75.0] |
| gpt-5-mini | format-instruction | exact_match | +55.0 | [+50.0, +65.0] |
| gpt-5-mini | format-instruction | no_answer | +20.0 | [+5.0, +35.0] |

## Interpretation and audit

The single labeled-component error was a cards copy-known-value trial on `foundry/gpt-5.6-terra`: `ZZBH-1585` was submitted instead of `ZZZBH-1585`. The frozen proper-substring rule retains this one-character omission in the delimitation endpoint. It is compatible with a transcription omission; no candidate in the labeled-component arm included its marker. The original fixed-token study remains 0/240, while the varied-instance endpoint is 1/240.

- These are within-instance comparisons in synthetic shells. They do not establish real-site generalization, a universal separation threshold, or equivalence across deployments.
- Content categories, target-token recovery, missing answers, and submitted task success are reported separately. Lower delimitation error can coexist with another failure mode. Task success uses the recorded exact answer or submitted field value, independently of the runner's final status. A copy trial that already submitted the correct value remains a checked success if the runner later times out or fails to finish; terminal status is retained separately.
- Paired intervals resample complete instance blocks within task, retaining layouts that share an instance. These exploratory comparisons have no multiplicity adjustment. Cell estimates include Wilson intervals, including at zero/ceiling.
- Geometry checks verify full target/distractor boxes inside the screenshot; they do not certify text readability. No OCR or human legibility metric is asserted.
- The format instruction narrows the answer space and supplies boundary guidance simultaneously; its effect does not isolate those mechanisms.
- Execution: 2026-09-19T00:11:40.820077+00:00 to 2026-09-19T00:47:29.064310Z.
- Infrastructure attempts excluded: 0; valid statuses: {'success': 754, 'failure': 81, 'timeout': 45}.
- Checked exact successes with a later non-success terminal status: 8.
- Frozen runner SHA-256: `4ab933fc46f111590163f7c4cdb06ce17e55797d8009c2ba739a0eb3cf9af0d1`.
- Results SHA-256: `5efab87e9002bb216e76f155c6aa6bb03f186c8179ae05fd48bce66f50d1fc0a`.
- Raw screenshots, request/response bytes, transcripts, and actions: `artifacts/instance-variation-20260919-final/runs`.
