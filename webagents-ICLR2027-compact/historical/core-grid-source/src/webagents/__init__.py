"""Reproducible web-agent experiment runners.

Runner imports deliberately remain lazy so importing the independent AX package
does not pull Browser Use into its dependency graph.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from webagents.schemas import RunTranscript

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    run_dom_agent: Callable[..., Awaitable[RunTranscript]]
    run_ax_agent: Callable[..., Awaitable[RunTranscript]]
    run_vision_agent: Callable[..., Awaitable[RunTranscript]]
    run_cdp_agent: Callable[..., Awaitable[RunTranscript]]

__all__ = ["RunTranscript", "run_ax_agent", "run_cdp_agent", "run_dom_agent", "run_vision_agent"]


def __getattr__(name: str) -> Any:
    if name == "run_dom_agent":
        from webagents.dom.runner import run_dom_agent

        return run_dom_agent
    if name == "run_ax_agent":
        from webagents.ax.runner import run_ax_agent

        return run_ax_agent
    if name == "run_vision_agent":
        from webagents.vision.runner import run_vision_agent

        return run_vision_agent
    if name == "run_cdp_agent":
        from webagents.cdp.runner import run_cdp_agent

        return run_cdp_agent
    raise AttributeError(name)
