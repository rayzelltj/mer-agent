"""Small helpers used by the agent_framework-based backend.

This module exists primarily as a stable import target (e.g. `src.backend.utils_af`)
for codepaths and tests that monkeypatch helper functions.
"""

from __future__ import annotations

from typing import Any


def retrieve_all_agent_tools() -> list[dict[str, Any]]:
    """Return a summary of all available agent tools.

    The concrete implementation is app-specific. By default we return an empty
    list so callers can operate (or tests can monkeypatch) without requiring
    optional tool backends.
    """

    return []
