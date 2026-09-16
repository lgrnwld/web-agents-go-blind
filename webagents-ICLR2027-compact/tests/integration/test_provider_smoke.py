from __future__ import annotations

import os

import pytest

from webagents.dom.runner import run_dom_agent


@pytest.mark.integration
@pytest.mark.provider
@pytest.mark.asyncio
async def test_opt_in_synthetic_provider_smoke() -> None:
    page_url = os.getenv("WEBAGENT_PROVIDER_SMOKE_URL")
    model = os.getenv("WEBAGENT_PROVIDER_SMOKE_MODEL")
    if not page_url or not model:
        pytest.skip("set WEBAGENT_PROVIDER_SMOKE_URL and WEBAGENT_PROVIDER_SMOKE_MODEL")
    assert page_url is not None and model is not None
    transcript = await run_dom_agent(
        page_url,
        "Read the synthetic marker and report it, then finish.",
        model,
        max_steps=5,
    )
    assert transcript.status in {"success", "failure"}
    assert transcript.steps
    assert all(step.observation.kind == "serialized_dom" for step in transcript.steps)
