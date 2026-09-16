from __future__ import annotations

from pathlib import Path

from webagents.dom.browser_use_adapter import (
    DOM_ONLY_EXCLUDED_ACTIONS,
    _disable_display_probe_for_headless,
    configure_browser_use_environment,
)


def test_dom_runner_has_no_secondary_page_perception_actions(tmp_path: Path) -> None:
    configure_browser_use_environment(tmp_path)
    _disable_display_probe_for_headless()

    from browser_use import BrowserProfile
    from browser_use.tools.service import Tools

    profile = BrowserProfile(
        headless=True,
        user_data_dir=None,
        enable_default_extensions=False,
        highlight_elements=False,
    )
    tools = Tools(exclude_actions=DOM_ONLY_EXCLUDED_ACTIONS)

    assert profile.headless is True
    assert profile.enable_default_extensions is False
    assert profile.highlight_elements is False
    assert set(DOM_ONLY_EXCLUDED_ACTIONS).isdisjoint(tools.registry.registry.actions)
    assert {"click", "input", "scroll", "done"}.issubset(tools.registry.registry.actions)
