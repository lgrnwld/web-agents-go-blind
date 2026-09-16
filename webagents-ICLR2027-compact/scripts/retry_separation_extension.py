"""Finish rate-limited separation trials without changing fixtures or valid outcomes."""

import asyncio

from scripts import run_canvas_delimitation_followup as study

original = study.run_one


async def with_cooldown(**kwargs):
    attempts = []
    for retry in range(10):
        row = await original(**{**kwargs, "run_namespace": kwargs["run_namespace"] + f"-R{retry}"})
        attempts.extend(row["attempt_run_ids"])
        if row["runner_status"] != "infrastructure_error":
            row["attempt_run_ids"] = attempts
            row["infrastructure_retries"] = len(attempts) - 1
            return row
        error = row.get("runner_final_error") or ""
        if "429" not in error:
            return row
        await asyncio.sleep(60 * min(retry + 1, 3))
    return row


if __name__ == "__main__":
    args = study.parser().parse_args()
    study.run_one = with_cooldown
    raise SystemExit(asyncio.run(study.execute(args)))
