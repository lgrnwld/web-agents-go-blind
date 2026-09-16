# Vision canvas failure coding

The coding universe is the 40 valid `rendered-canvas` trials in the completed
`vision-full-scan-v2` sweep: two tasks, two models, and ten repeats per
task--model cell. Each code was assigned from the archived model response,
action sequence, and checker detail, using an exact-value criterion:

- **Extraction failure:** the emitted answer or typed string was not exactly
  the checker target.
- **Localization failure:** the exact target was stated, but coordinate actions
  did not place it in the Destination field (copy task only).
- **Action failure:** extraction and localization were correct, but the action
  sequence still did not produce a checker-passing state.
- **Refusal/abstention:** the model declined or said it could not find the
  target.

| Failure stage | Trials | Share |
| --- | ---: | ---: |
| Extraction failure | 40 | 100% |
| Localization failure | 0 | 0% |
| Action failure | 0 | 0% |
| Refusal/abstention | 0 | 0% |

All 40 responses included the true token but appended the visible control
marker: `ALPHA-7391 | CONTROL_READ_VALUE_R2` in all 20 read trials, and either
`BRAVO-4826 | CONTROL_COPY_VALUE_R5` or the visually confusable
`BRAVO-4826 I CONTROL_COPY_VALUE_R5` in all 20 copy trials. Thus the errors are
more precisely failures to delimit the exact target value than failures to
recognize its constituent characters. In every copy trial, the checker
recorded the composite string as `submitted_value`, demonstrating that the
Destination field was localized and received the model's intended text. No
trial refused, abstained, or emitted the exact target and then failed later in
the action pipeline.

The trial-level audit is in `vision-canvas-failure-coding.csv`.
