"""Provider-free, completeness-gated analysis of the frozen instance study."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from scripts.analyze_revision_experiments import aggregate, paired_effect, write_csv
from scripts.analyze_revision_experiments import MODELS

METRICS = (
    "delimitation_error",
    "content_exact",
    "exact_match",
    "no_answer",
    "target_token_in_candidate",
    "marker_in_candidate",
)


def load_complete(root):
    spec = json.loads((root / "resolved-spec.json").read_text())
    matrix = json.loads((root / "matrix.json").read_text())
    attempts = [json.loads(s) for s in (root / "results.jsonl").read_text().splitlines()]
    allocated = {r["id"]: r for r in matrix}
    latest = {}
    for row in attempts:
        assert row["id"] in allocated, "Unallocated result"
        assert not (row["id"] in latest and latest[row["id"]]["valid"]), "A valid trial was rerun"
        assert all(row[k] == v for k, v in allocated[row["id"]].items()), "Instance mismatch"
        latest[row["id"]] = row
    assert len(latest) == spec["planned_trials"] == len(matrix), "Incomplete allocation"
    assert all(r["valid"] for r in latest.values()), "Infrastructure failures remain"
    assert hashlib.sha256((root / "runner-source.py").read_bytes()).hexdigest() == spec["source_sha256"]
    rows = [latest[r["id"]] for r in matrix]
    for row in rows:
        row["arm"] = (
            ("format" if row["intervention"] == "format" else "baseline")
            if row["layout"] == "canvas-inline-bar"
            else ("labeled" if row["boundary"] == "visible" else f"gap-{row['gap']}")
        )
        assert row["geometry"]["target_region_visible"]
    return spec, rows, attempts


def ratio(rows, metric):
    return f"{sum(bool(r[metric]) for r in rows)}/{len(rows)}"


def analyze(root, out, paper):
    spec, rows, attempts = load_complete(root)
    out.mkdir(parents=True, exist_ok=True)
    cells = aggregate(rows, ("model", "task_id", "layout", "arm"), metrics=METRICS)
    write_csv(out / "cells.csv", cells)
    pooled = aggregate(rows, ("model", "layout", "arm"), metrics=METRICS)
    write_csv(out / "task-pooled-cells.csv", pooled)
    effects, summary = [], []
    for model in spec["models"]:
        natural = [r for r in rows if r["model"] == model and r["layout"] != "canvas-inline-bar"]
        canvas = [r for r in rows if r["model"] == model and r["layout"] == "canvas-inline-bar"]
        for name, chosen, before, after, keys in (
            ("pixel-gap", natural, "gap-8", "gap-96", ("task_id", "repeat", "layout")),
            ("labeled-components", natural, "gap-8", "labeled", ("task_id", "repeat", "layout")),
            ("format-instruction", canvas, "baseline", "format", ("task_id", "repeat")),
        ):
            for metric in METRICS:
                effect = paired_effect(chosen, "arm", before, after, keys, metric=metric)
                # Layouts share instances: do not publish an unclustered McNemar p-value.
                effect.pop("mcnemar_exact_p")
                effects.append(dict(model=model, contrast=name, metric=metric, **effect))
        arms = {}
        for arm, pool in (
            ("gap-8", natural),
            ("gap-96", natural),
            ("labeled", natural),
            ("baseline", canvas),
            ("format", canvas),
        ):
            subset = [r for r in pool if r["arm"] == arm]
            arms[arm] = dict(n=len(subset), **{m: sum(bool(r[m]) for r in subset) for m in METRICS})
        summary.append(dict(model=model, arms=arms))
    write_csv(out / "paired-effects.csv", effects)
    manifest = dict(
        planned=spec["planned_trials"],
        valid=len(rows),
        unique_instances=len({r["instance_id"] for r in rows}),
        first_started=min(r["started_at"] for r in rows),
        last_finished=max(r["finished_at"] for r in rows),
        total_attempts=sum(len(r["attempts"]) for r in attempts),
        infrastructure_attempts=sum(a["status"] == "infrastructure_error" for r in attempts for a in r["attempts"]),
        statuses=dict(Counter(r["runner_status"] for r in rows)),
        non_success_terminal_with_exact_match=sum(
            r["runner_status"] != "success" and r["exact_match"] for r in rows
        ),
        input_tokens=sum(r["input_tokens"] for r in rows),
        output_tokens=sum(r["output_tokens"] for r in rows),
        paper_sha256=spec["source_paper_sha256"],
        source_sha256=spec["source_sha256"],
        results_sha256=hashlib.sha256((root / "results.jsonl").read_bytes()).hexdigest(),
        matrix_sha256=spec["matrix_sha256"],
        summary=summary,
        effects=effects,
    )
    (out / "summary.json").write_text(json.dumps(manifest, indent=2) + "\n")
    report = [
        "# Instance variation: completed results",
        "",
        f"All {len(rows)} allocated trials are valid. There are {manifest['unique_instances']} independent task/instance blocks: ten new instances for each of two tasks, paired across four models and eleven conditions.",
        "",
        "Targets, markers, positions, fonts, and spacing vary across instances. The original token shape is retained. No generic-instruction control was run. Model failure and timeout remain outcomes; only infrastructure failures are retried.",
        "",
        "| Deployment | Delimitation: 8px | Delimitation: 96px | Delimitation: labeled | Canvas success: baseline | Canvas success: format |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for item in summary:
        a = item["arms"]
        report.append(
            "| "
            + item["model"].split("/")[1]
            + " | "
            + " | ".join(
                f"{a[arm][metric]}/{a[arm]['n']}"
                for arm, metric in (
                    ("gap-8", "delimitation_error"),
                    ("gap-96", "delimitation_error"),
                    ("labeled", "delimitation_error"),
                    ("baseline", "exact_match"),
                    ("format", "exact_match"),
                )
            )
            + " |"
        )
    report += [
        "",
        "## Paired effects",
        "",
        "| Deployment | Contrast | Endpoint | Change (percentage points) | Paired 95% bootstrap interval |",
        "|---|---|---|---:|---:|",
    ]
    for e in effects:
        if e["metric"] not in ("delimitation_error", "exact_match", "no_answer"):
            continue
        report.append(
            f"| {e['model'].split('/')[1]} | {e['contrast']} | {e['metric']} | {100 * e['difference']:+.1f} | [{100 * e['bootstrap_low']:+.1f}, {100 * e['bootstrap_high']:+.1f}] |"
        )
    report += [
        "",
        "## Interpretation and audit",
        "",
        "- These are within-instance comparisons in synthetic shells. They do not establish real-site generalization, a universal separation threshold, or equivalence across deployments.",
        "- Content categories, target-token recovery, missing answers, and submitted task success are reported separately. Lower delimitation error can coexist with another failure mode. Task success uses the recorded exact answer or submitted field value, independently of the runner's final status. A copy trial that already submitted the correct value remains a checked success if the runner later times out or fails to finish; terminal status is retained separately.",
        "- Paired intervals resample complete instance blocks within task, retaining layouts that share an instance. These exploratory comparisons have no multiplicity adjustment. Cell estimates include Wilson intervals, including at zero/ceiling.",
        "- Geometry checks verify full target/distractor boxes inside the screenshot; they do not certify text readability. No OCR or human legibility metric is asserted.",
        "- The format instruction narrows the answer space and supplies boundary guidance simultaneously; its effect does not isolate those mechanisms.",
        f"- Execution: {manifest['first_started']} to {manifest['last_finished']}.",
        f"- Infrastructure attempts excluded: {manifest['infrastructure_attempts']}; valid statuses: {manifest['statuses']}.",
        f"- Checked exact successes with a later non-success terminal status: {manifest['non_success_terminal_with_exact_match']}.",
        f"- Frozen runner SHA-256: `{manifest['source_sha256']}`.",
        f"- Results SHA-256: `{manifest['results_sha256']}`.",
        f"- Raw screenshots, request/response bytes, transcripts, and actions: `{root / 'runs'}`.",
    ]
    labeled_errors = [r for r in rows if r["arm"] == "labeled" and r["delimitation_error"]]
    if len(labeled_errors) == 1:
        error = labeled_errors[0]
        note = (
            f"The single labeled-component error was a {error['layout']} {error['task_id']} trial on "
            f"`{error['model']}`: `{error['candidate_value']}` was submitted instead of `{error['value']}`. "
            "The frozen proper-substring rule retains this one-character omission in the delimitation endpoint. "
            "It is compatible with a transcription omission; no candidate in the labeled-component arm included its marker. "
            "The original fixed-token study remains 0/240, while the varied-instance endpoint is 1/240."
        )
        index = report.index("## Interpretation and audit") + 2
        report[index:index] = [note, ""]
    (out / "report.md").write_text("\n".join(report) + "\n")
    if paper:
        write_paper(paper, rows, summary, effects, manifest)
    print(json.dumps({k: v for k, v in manifest.items() if k not in ("effects", "summary")}, indent=2))
    print("\n".join(report[:12]))


def write_paper(paper, rows, summary, effects, manifest):
    # Results are mechanically inserted only after all preallocated cells validate.
    def model(s):
        return r"\texttt{" + s.split("/")[1] + "}"

    successes = [e for e in effects if e["contrast"] == "format-instruction" and e["metric"] == "exact_match"]
    gap = [e for e in effects if e["contrast"] == "pixel-gap" and e["metric"] == "delimitation_error"]
    gap_success = [e for e in effects if e["contrast"] == "pixel-gap" and e["metric"] == "exact_match"]
    assert all(e["bootstrap_high"] < 0 for e in gap)
    labeled = [r for r in rows if r["arm"] == "labeled"]
    label_errors = [r for r in labeled if r["delimitation_error"]]
    assert len(label_errors) == 1 and label_errors[0]["candidate_value"].strip() in label_errors[0]["value"]
    assert not any(r["marker_in_candidate"] for r in labeled)
    main = [
        r"\subsection{Instance variation tests the failure beyond fixed strings}",
        r"\label{sec:instances}", "",
        "A separately frozen 880-trial study uses ten new instances per task, varying target values, marker strings, positions, and typography while pairing instances across deployments and conditions. Increasing the gap from 8 to 96 pixels reduced delimitation errors by "
        + f"{min(-100*e['difference'] for e in gap):.1f}--{max(-100*e['difference'] for e in gap):.1f} percentage points across the four deployments (all paired 95\\% intervals excluded zero), while exact task success rose by "
        + f"{min(100*e['difference'] for e in gap_success):.1f}--{max(100*e['difference'] for e in gap_success):.1f} points. "
        + "Labeled components yielded 1/240 errors under the subset/superset endpoint: one truncated target, with no marker appended. This broad score includes character omissions and does not by itself identify a latent boundary mechanism.", "",
        "On varied inline canvas, explicit-format prompts raised exact task success from "
        + f"{sum(e['baseline'] for e in successes)}/{sum(e['pairs'] for e in successes)} to {sum(e['treatment'] for e in successes)}/{sum(e['pairs'] for e in successes)} "
        + f"({min(e['treatment'] for e in successes)}--{max(e['treatment'] for e in successes)}/20 per deployment). "
        + r"Appendix~\ref{app:instance-variation} reports deployment-resolved counts, paired intervals, content and submission outcomes, and design details. The failure and mitigation effects persist across these sampled synthetic instances; real-site generalization remains untested.", "",
    ]
    (paper / "instance-results.tex").write_text("\n".join(main))
    appendix = (paper / "instance-appendix.tex").read_text().split("% BEGIN COMPLETED RESULTS")[0]
    lines = [
        "% BEGIN COMPLETED RESULTS",
        "",
        r"\subsection{Completed instance-study results}",
        f"All {len(rows)} allocated trials were obtained across {manifest['unique_instances']} unique task--instance blocks. "
        "All target and distractor text boxes passed viewport checks. "
        + f"Infrastructure errors accounted for {manifest['infrastructure_attempts']} excluded attempts. "
        "All model failures and timeouts remain in the denominators.",
        "",
        r"\begin{table}[ht]",
        r"\centering\small",
        r"\caption{Instance variation: delimitation errors in familiar shells and exact task success on inline canvas. Shell counts pool three layouts and two tasks ($N=60$ per deployment and arm); canvas counts pool tasks ($N=20$).}",
        r"\label{tab:instance-results}",
        r"\begin{tabular}{lrrrrr}",
        r"\toprule",
        r"& \multicolumn{3}{c}{Delimitation errors} & \multicolumn{2}{c}{Canvas task success} \\",
        r"Deployment & 8px & 96px & Labeled & Baseline & Format \\",
        r"\midrule",
    ]
    for i in summary:
        a = i["arms"]
        lines.append(
            model(i["model"])
            + " & "
            + " & ".join(
                f"{a[arm][metric]}/{a[arm]['n']}"
                for arm, metric in (
                    ("gap-8", "delimitation_error"),
                    ("gap-96", "delimitation_error"),
                    ("labeled", "delimitation_error"),
                    ("baseline", "exact_match"),
                    ("format", "exact_match"),
                )
            )
            + r" \\"
        )
    lines += [
        r"\bottomrule",
        r"\end{tabular}",
        r"\end{table}",
        "",
        r"\begin{table}[ht]",
        r"\centering\small",
        r"\caption{Paired instance-study effects in percentage points (treatment minus baseline), with task-stratified instance-block bootstrap 95\% intervals. Gap and labeled contrasts measure delimitation error; format measures task success.}",
        r"\begin{tabular}{lrrr}",
        r"\toprule",
        r"Deployment & 96px minus 8px & Labeled minus 8px & Format minus baseline \\",
        r"\midrule",
    ]
    for i in summary:
        values = []
        for contrast, metric in (
            ("pixel-gap", "delimitation_error"),
            ("labeled-components", "delimitation_error"),
            ("format-instruction", "exact_match"),
        ):
            e = next(
                e for e in effects if e["model"] == i["model"] and e["contrast"] == contrast and e["metric"] == metric
            )
            values.append(
                f"${100 * e['difference']:+.1f}$ [$ {100 * e['bootstrap_low']:+.1f}, {100 * e['bootstrap_high']:+.1f}$]"
            )
        lines.append(model(i["model"]) + " & " + " & ".join(values) + r" \\")
    lines += [
        r"\bottomrule",
        r"\end{tabular}",
        r"\end{table}",
        "",
        r"\paragraph{The single labeled-component error.}",
        "The only labeled-component output classified as delimitation was a copy trial in the card layout: "
        + model(label_errors[0]["model"]) + " submitted " + r"\texttt{" + label_errors[0]["candidate_value"]
        + "} instead of " + r"\texttt{" + label_errors[0]["value"]
        + "}. The fixed scorer includes proper substrings in the delimitation endpoint, so this one-character omission is retained without relabeling. No labeled-component candidate included its reference marker. The observation is compatible with a transcription omission and does not establish an answer-boundary error by itself.",
        r"\paragraph{Content and completion remain distinct.}",
        "The machine-readable cell tables retain each task, layout, deployment, and arm, with Wilson intervals for all endpoints. "
        + "Across the three natural-layout arms, correct candidate content / exact task success / missing-answer counts were: "
        + "; ".join(
            model(m)
            + " "
            + " / ".join(
                ratio([r for r in rows if r["model"] == m and r["layout"] != "canvas-inline-bar"], metric)
                for metric in ("content_exact", "exact_match", "no_answer")
            )
            for m in MODELS
        )
        + ".",
        "No-answer outcomes and substitutions remain distinct from delimitation; zero observed delimitation is not proof of zero risk. "
        + "Instance blocks, rather than model calls or individual layout rows, are the units of bootstrap resampling. Full paired intervals and cell counts are included in the accompanying analysis.",
        "",
    ]
    (paper / "instance-appendix.tex").write_text(appendix + "\n".join(lines))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, default=Path("artifacts/instance-variation-20260919-final"))
    p.add_argument("--output", type=Path, default=Path("analysis/instance-variation-20260919"))
    p.add_argument("--paper", type=Path)
    args = p.parse_args()
    analyze(args.root, args.output, args.paper)


if __name__ == "__main__":
    main()
