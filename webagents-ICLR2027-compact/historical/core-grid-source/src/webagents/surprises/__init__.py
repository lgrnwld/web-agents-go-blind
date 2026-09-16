"""Append-only framework-surprise evidence log."""

from webagents.surprises.log import (
    add_sweep_candidate,
    adjudicate_surprise,
    append_surprise_event,
    correct_surprise,
    load_expectation_registry,
    verify_surprise_log,
)
from webagents.surprises.render import render_surprise_views

__all__ = [
    "add_sweep_candidate",
    "adjudicate_surprise",
    "append_surprise_event",
    "correct_surprise",
    "load_expectation_registry",
    "render_surprise_views",
    "verify_surprise_log",
]
