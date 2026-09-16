# Changes from the supplied paper

Baseline: the supplied `webagents1.a.pdf`, whose extracted page text matches the preserved `webagents1.7` source/PDF. Current version: the ICLR 2027 manuscript distributed here.

**Yes: the revision includes substantial prose changes, not only formatting.** It retains the original study and adds the completed follow-up experiments. Changes also shorten the main narrative to fit the ICLR nine-page main-text limit.

| Part | Change |
|---|---|
| Abstract | Rewritten to integrate the new separation, generalization, intervention, and calibrated iframe results. |
| Introduction and methods | Updated to describe the expanded scope; detailed new design and calibration information is in the appendix. |
| Original results | Original primary-grid outcomes and historical follow-up/model-extension results are retained; surrounding interpretation is updated where the new experiments bear on it. |
| New results | Added 2,640 completed trials, with ten valid repeats per cell: 240 separation-extension, 1,440 generalization, 160 intervention, 560 iframe, and 240 natural-boundary trials. |
| Figure 3 | Expanded separation curves to include gpt-5.4 and gpt-5-mini and both tasks. |
| Figure 4 | Replaced the main-text variance decomposition with the new generalization/intervention experiment. The variance audit remains in the appendix as Figure 6 with its confounding caveat. |
| Discussion and conclusion | Condensed and reworded to incorporate the new evidence and keep main text within the page limit. This is more than a light copy edit. |
| Limitations | Added or clarified the hidden-DOM negative control, compound visible-boundary controls, last-typed candidate versus submission distinction, and limits of the iframe comparison. |
| AI disclosure | The original paper already had an AI-use disclosure. It was moved and expanded into the dedicated AI use statement; it was not newly invented or duplicated. |
| Format | Converted from NeurIPS to the official ICLR 2027 anonymous template. Nine main-text pages, then statements, references, and appendix; 17 pages total. |
| Bibliography | `references.bib` is byte-for-byte identical to the baseline bibliography. |

The original anonymous reference PDF is included in the reproducibility package as `paper/reference-original.pdf`. `MANUSCRIPT-DIFF.html` provides a side-by-side source comparison, with the new input files expanded; LaTeX formatting and moved passages contribute to the diff, so it is not a percentage measure of substantive change.

This package has not been uploaded to the existing anonymous repository. Replace the repository link in the manuscript after the new artifact is hosted. The existing AI statement remains present in the ICLR version.
