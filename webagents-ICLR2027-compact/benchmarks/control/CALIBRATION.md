# Control calibration log

## 2026-08-30: copy-known-value revision 4

The four-class validation `01M19QFRA7FFQN6ZTXNJC2RB3G` completed 72 trials under control-spec SHA-256
`14e9724dd8590d1c3eaa1c4feca296d1689272bcbe64d20cd0b8997b1c16d5aa` and ended with the verdict
`task_failure`. Its report, results, transcripts, screenshots, and model requests remain unchanged under
`artifacts/control/01M19QFRA7FFQN6ZTXNJC2RB3G` and the referenced run-archive directories.

All screenshot-only read trials passed. In the copy task, both models repeatedly recovered the exact source value,
but their coordinate actions often landed roughly 8–10 CSS pixels above the browser's approximately 20-pixel-high
native text field. GPT-5.6 Terra passed 0/6 screenshot-only copy trials across revisions 2 and 3; Kimi K2.6 passed
2/6. The corresponding DOM and CDP copy cells passed, and the validation contained no blocking infrastructure,
configuration, fixture, or evidence classification.

Revision 4 is therefore a task-wide control simplification, added after the exhausted revision-3 ladder. It keeps
the semantic goal, source value, labels, expected answer, checker behavior, and prompt from revision 3. Its only
change is the versioned `copy_value_form_large_target` presentation: a vertically separated input and button with
minimum 56 CSS-pixel heights. The presentation applies identically to every architecture and model and propagates
to every structural condition generated from revision 4, including content inside shadow roots.

This is a post-failure calibration decision, not a preregistered result. Analyses must not pool revisions 2 or 3
with revision 4, and must identify revision 4 as the admitted copy task if it passes. The earlier failed validation
remains part of the audit trail. A new control validation and receipt are required before revision-4 sweep data can
be scheduled.

## 2026-08-30: copy-known-value revision 5 and explicit AX references

The four-class validation `01M19T5WSA8EV0TB84G070NZH7` completed 48 trials under control-spec SHA-256
`50c79f05fad5181210dad65eb4f83ad085e414e8172a851915c2c52c0241c450` and ended with the verdict
`task_failure`. Its 48 result rows and referenced run archives remain unchanged. Forty-three trials passed, and
there were no blocking infrastructure, configuration, fixture, or evidence classifications.

The revision-4 presentation resolved the Kimi K2.6 screenshot-only copy cell from 1/3 at revision 3 to 3/3.
GPT-5.6 Terra also read and entered the exact source value in every revision-4 screenshot trial, but then clicked
repeatedly in the 12 CSS-pixel gap immediately above the Submit button until the 30-step limit. To remove button
coordinate precision from the flat admission task, revision 5 retains revision 4's semantic goal, values, checker,
labels, and large-target presentation but instructs every architecture and model to press Enter after typing.

Two GPT-5.6 Terra AX trials also exposed an action-interface defect: the raw protocol results contained no explicit
`ax-N` fields, so the model had to count nodes manually and selected adjacent references. The capture now retains
Chromium's protocol results byte-for-byte while adding an `action_references` catalog derived only from those AX
nodes. Each catalog entry provides the exact step-local reference, role, accessible name, value, source location,
and actionability flag. The capture identity advances from `ax-cdp-full-v1` to
`ax-cdp-full-action-refs-v2`, and the prompt identity advances from `ax-cdp-full-action-v2` to
`ax-cdp-full-action-v3`. The executor and reference lifetime rules are unchanged.

Both corrections are architecture-wide and model-independent. Revision 5 is a post-failure simplification and the
AX catalog is a versioned action-interface repair; neither changes the success checker or relaxes the required 3/3
admission threshold. Analyses must not pool copy-task revisions 2–4 with revision 5. A new complete control receipt
is required before scheduling revision-5 sweeps.

## 2026-08-30: revision-5 infrastructure-timeout recovery

Validation `01M19VM8VVN0JR56BZCXJ6XXA3` ran the complete 48-trial revision-5 matrix. Forty-seven trials passed.
The remaining Kimi K2.6 CDP trial successfully clicked and typed the expected value, then its runner reached the
frozen 180-second wall-clock limit while awaiting the next model step. The transcript status was `timeout`, the
checker state remained unset, and the archive was valid, complete, and reachable. The validator incorrectly fell
through to `TASK_FAILURE` instead of applying the predeclared infrastructure retry policy.

The source validation and all 48 source result rows remain immutable. The hash-preserving recovery utility at
`scripts/retry_control_infrastructure.py` accepts only verified archived wall-clock timeouts, verifies that the
current resolved control spec and hashed runner implementation exactly match the source report, and rejects any
other failed classification. It retries only the affected logical trial, up to the already-frozen maximum of two
infrastructure retries, and writes a new validation directory with source lineage and a pass-only receipt. Files in
`scripts/` are intentionally excluded from the runner implementation digest; no experimental runner, prompt,
fixture, task, model, threshold, or source validation evidence is changed by this recovery.

## 2026-08-30: four-class per-run evidence scanner correction

The first revision-5 Vision sweep at `artifacts/sweeps-r5/vision-full/vision-full-v1` stopped after one live Kimi
K2.6 trial with an `EVIDENCE_FAILURE`. The trial's eight committed screenshot calls, model-request bytes,
transcript, manifest, and completion record were internally valid. The archive-wide verifier also recognized all
eight calls as screenshots. However, the sweep-only `scan_run_observations` helper still mapped every non-DOM
agent to `ax_tree`, so it falsely rejected `screenshot` metadata as an identity mismatch. The blocked sweep has
zero admitted outcomes and remains unchanged as an audit artifact.

The helper now uses the same canonical `observation_kind_for_agent` mapping as archive-wide verification:
serialized DOM, AX tree, screenshot, and CDP DOM snapshot. Regression coverage requires per-run Vision and CDP
scans to retain their correct evidence kinds. Under the corrected scanner, the failed trial archive yields all
eight screenshot records with no verification error.

This correction changes the hashed implementation source even though it does not change any prompt, action
interface, model request, task, fixture, or success checker. The prior control receipt cannot admit new sweeps under
the corrected source identity. A new complete four-class control validation is required before rerunning Vision or
starting CDP. Existing provider-backed sweep archives remain immutable and must be joined across the version
boundary only with this correction explicitly recorded.

Validation `01M1A74305J62F8XRKTCHJ1NWQ` subsequently passed the corrected 48-trial four-class control matrix at
3/3 in every task, class, and model cell. The first attempted post-fix launch retained the original
`vision-full-v1` sweep identifier, whose deterministic run ID collided with the preserved pre-fix trial archive.
The runner refused to overwrite that archive and the scheduler stopped after one zero-token
`CONFIGURATION_FAILURE`. No provider call or admissible outcome was produced by that launch. The corrected Vision
schedule is therefore versioned as `vision-full-scan-v1`; its tasks, models, conditions, allocation, randomization
seed, limits, prompts, and runner are unchanged, while its distinct sweep ID gives every new archive a distinct
run identity.

The `vision-full-scan-v1` launch then exposed a second sweep-only projection defect after 13 valid trials. The raw
Vision metadata correctly archived the privileged scorer probe as `playwright-visible-target-v1` with
`visible: true`, but `scan_run_observations` failed to copy that field into its `ObservationRecord`. Consequently,
live screenshot trials could succeed while being scored unreachable; the two reachable rows at the first status
check were reused controls rather than live evidence. The run was stopped and remains a non-admitted diagnostic
artifact.

The per-run scanner now propagates the archived reachability record exactly as the archive-wide scanner already
did. Regression coverage asserts both the screenshot evidence kind and the visible probe, and the actual partial
archive rescans with its visibility values restored and no verification errors. This repair changes the hashed
source identity and therefore requires another four-class control receipt. The next schedule is versioned
`vision-full-scan-v2` to avoid every earlier deterministic run ID; no task, model, condition, allocation, seed,
limit, prompt, action interface, or success checker changes.

Validation `01M1A8637ZKPTHA88QSAP71X0X` then passed all 48 trials under the reachability-propagation source identity,
with every task, class, and model cell at 3/3. A provider-free prelaunch audit rescanned all 12 Vision control
archives through the exact sweep scanner: every archive retained records without verification errors and every
trial contained at least one `visible: true` task-page observation. The `vision-full-scan-v2` matrix contains 440
rows and none of its attempt-0 run IDs existed at launch time.
