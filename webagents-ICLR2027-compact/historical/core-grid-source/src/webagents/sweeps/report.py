"""Deterministic human-readable sweep reporting."""

from __future__ import annotations

from webagents.sweeps.schemas import DomLightReport, SweepReport


def report_markdown(report: SweepReport) -> bytes:
    lines = [
        f"# Accessibility-tree full sweep: {report.sweep_id}",
        "",
        f"Verdict: **{report.verdict}**",
        "",
        f"Complete cells: {report.complete_cells}/{report.required_cells}",
        f"Valid trials: {report.valid_trial_count}",
        f"All attempts: {report.attempt_count}",
        f"Framework-surprise candidates: {report.surprise_candidate_count}",
        "",
        "## Cell results",
        "",
        "| Task | Model | Condition | N | Reachability | Success | Reused controls |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for cell in report.aggregates:
        reachability = "—" if cell.reachability.rate is None else f"{cell.reachability.rate:.3f}"
        success = "—" if cell.task_success.rate is None else f"{cell.task_success.rate:.3f}"
        lines.append(
            f"| {cell.task_id} r{cell.task_revision} | {cell.model_id} | {cell.condition_id} | "
            f"{cell.valid_trials}/{cell.required_trials} | {reachability} | {success} | "
            f"{cell.reused_control_trials} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation boundary",
            "",
            report.architecture_variance_status,
            "",
            "Reachability is scored only from verified model-visible AX artifacts. An absent marker in a complete "
            "capture is a valid experimental outcome, not a fixture failure.",
            "",
        ]
    )
    return "\n".join(lines).encode("utf-8")


def dom_report_markdown(report: DomLightReport) -> bytes:
    lines = [
        f"# DOM-extraction light sweep: {report.sweep_id}",
        "",
        f"Verdict: **{report.verdict}**",
        "",
        f"Primary model: `{report.primary_model_id}`",
        f"Primary cells: {report.primary_complete_cells}/{report.primary_required_cells}",
        "Additional models: " + (", ".join(f"`{model}`" for model in report.additional_model_ids) or "none"),
        f"Confirmation cells: {report.confirmation_complete_cells}/{report.confirmation_required_cells}",
        f"Divergent confirmations: {report.divergent_confirmation_count}",
        f"Inherited valid trials: {report.inherited_trial_count}",
        f"Framework-surprise candidates: {report.surprise_candidate_count}",
        "",
        "## Primary-model results",
        "",
        "| Task | Condition | N | Reachability | Success | Reused controls |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for cell in report.primary_aggregates:
        reachability = "—" if cell.reachability.rate is None else f"{cell.reachability.rate:.3f}"
        success = "—" if cell.task_success.rate is None else f"{cell.task_success.rate:.3f}"
        lines.append(
            f"| {cell.task_id} r{cell.task_revision} | {cell.condition_id} | "
            f"{cell.valid_trials}/{cell.required_trials} | {reachability} | {success} | "
            f"{cell.reused_control_trials} |"
        )
    if report.additional_aggregates and report.additional_aggregates[0].required_trials > 1:
        lines.extend(
            [
                "",
                "## Additional-model repeated results",
                "",
                "| Task | Model | Condition | N | Reachability | Success |",
                "| --- | --- | --- | ---: | ---: | ---: |",
            ]
        )
        for cell in report.additional_aggregates:
            reachability = "—" if cell.reachability.rate is None else f"{cell.reachability.rate:.3f}"
            success = "—" if cell.task_success.rate is None else f"{cell.task_success.rate:.3f}"
            lines.append(
                f"| {cell.task_id} r{cell.task_revision} | {cell.model_id} | {cell.condition_id} | "
                f"{cell.valid_trials}/{cell.required_trials} | {reachability} | {success} |"
            )
    else:
        lines.extend(
            [
                "",
                "## Additional-model confirmations",
                "",
                "| Task | Model | Condition | Reachable | Successful | Agreement |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
        )
        for item in report.model_confirmations:
            agreement = "divergent" if item.divergent else "consistent"
            lines.append(
                f"| {item.task_id} r{item.task_revision} | {item.model_id} | {item.condition_id} | "
                f"{'yes' if item.reachable else 'no'} | {'yes' if item.task_success else 'no'} | {agreement} |"
            )
    lines.extend(["", "## Interpretation boundary", "", report.interpretation_limit, ""])
    return "\n".join(lines).encode("utf-8")
