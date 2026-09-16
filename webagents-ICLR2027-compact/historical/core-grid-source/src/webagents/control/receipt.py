"""Hash-bound control receipts and the mandatory sweep admission check."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from webagents.capture.archive import canonical_json_bytes
from webagents.control.schemas import (
    ControlReceipt,
    ControlSpec,
    ControlValidationReport,
    ImplementationIdentity,
    SweepIdentity,
)
from webagents.schemas import ARCHIVE_SCHEMA_VERSION, TRANSCRIPT_SCHEMA_VERSION


class ControlAdmissionError(RuntimeError):
    """The sweep identity is not covered by a passing control receipt."""


def implementation_identity(project_root: Path) -> ImplementationIdentity:
    from webagents.ax.prompt import PROMPT_VERSION as AX_PROMPT_VERSION
    from webagents.ax.runner import CHROMIUM_REVISION, PLAYWRIGHT_VERSION
    from webagents.cdp.prompt import PROMPT_VERSION as CDP_PROMPT_VERSION
    from webagents.dom.browser_use_adapter import (
        FRAMEWORK_COMMIT,
        FRAMEWORK_VERSION,
    )
    from webagents.dom.browser_use_adapter import (
        PROMPT_VERSION as DOM_PROMPT_VERSION,
    )
    from webagents.vision.prompt import PROMPT_VERSION as VISION_PROMPT_VERSION

    digest = hashlib.sha256()
    candidates = [project_root / "pyproject.toml", project_root / "uv.lock"]
    candidates.extend(sorted((project_root / "src" / "webagents").rglob("*.py")))
    for path in candidates:
        if path.is_file():
            digest.update(path.relative_to(project_root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return ImplementationIdentity(
        source_sha256=digest.hexdigest(),
        transcript_schema_version=TRANSCRIPT_SCHEMA_VERSION,
        archive_schema_version=ARCHIVE_SCHEMA_VERSION,
        dom_framework=f"browser-use/{FRAMEWORK_VERSION}@{FRAMEWORK_COMMIT}",
        dom_prompt_version=DOM_PROMPT_VERSION,
        ax_framework=f"playwright/{PLAYWRIGHT_VERSION}@chromium-{CHROMIUM_REVISION}",
        ax_prompt_version=AX_PROMPT_VERSION,
        vision_framework=f"playwright-screenshot/{PLAYWRIGHT_VERSION}@chromium-{CHROMIUM_REVISION}",
        vision_prompt_version=VISION_PROMPT_VERSION,
        cdp_framework=f"playwright-cdp-snapshot/{PLAYWRIGHT_VERSION}@chromium-{CHROMIUM_REVISION}",
        cdp_prompt_version=CDP_PROMPT_VERSION,
    )


def assert_control_admitted(receipt_path: Path, resolved_sweep_spec: SweepIdentity) -> None:
    try:
        receipt_bytes = receipt_path.read_bytes()
        receipt = ControlReceipt.model_validate_json(receipt_bytes)
        if canonical_json_bytes(receipt.model_dump(mode="json")) != receipt_bytes:
            raise ValueError("receipt is not canonically encoded")
        report = ControlValidationReport.model_validate_json((receipt_path.parent / "report.json").read_bytes())
        resolved_bytes = (receipt_path.parent / "resolved-spec.json").read_bytes()
        resolved_hash = hashlib.sha256(resolved_bytes).hexdigest()
        resolved_value = json.loads(resolved_bytes)
        resolved_control = ControlSpec.model_validate(resolved_value["control"])
        matrix_hash = hashlib.sha256((receipt_path.parent / "matrix.jsonl").read_bytes()).hexdigest()
        results_hash = hashlib.sha256((receipt_path.parent / "results.jsonl").read_bytes()).hexdigest()
    except Exception as exc:
        raise ControlAdmissionError(f"control receipt is missing or invalid: {exc}") from exc
    if (
        report.verdict != "pass"
        or report.validation_id != receipt.validation_id
        or report.control_spec_sha256 != receipt.control_spec_sha256
        or report.matrix_sha256 != receipt.matrix_sha256
        or report.results_sha256 != receipt.results_sha256
        or report.implementation != receipt.implementation
        or report.active_tasks != receipt.tasks
        or report.passed_at != receipt.passed_at
        or resolved_hash != receipt.control_spec_sha256
        or [item.id for item in resolved_control.classes] != receipt.classes
        or [item.runner_model for item in resolved_control.models] != receipt.models
        or {
            "repeats": resolved_control.policy.repeats,
            "required_successes": resolved_control.policy.required_successes,
        }
        != receipt.policy
        or matrix_hash != receipt.matrix_sha256
        or results_hash != receipt.results_sha256
    ):
        raise ControlAdmissionError("control receipt does not match its report or evidence files")
    expected = SweepIdentity(
        control_spec_sha256=receipt.control_spec_sha256,
        implementation=receipt.implementation,
        classes=receipt.classes,
        models=receipt.models,
        tasks=receipt.tasks,
        policy=receipt.policy,
    )
    if canonical_json_bytes(expected.model_dump(mode="json")) != canonical_json_bytes(
        resolved_sweep_spec.model_dump(mode="json")
    ):
        raise ControlAdmissionError("control receipt does not match the resolved sweep identity")
