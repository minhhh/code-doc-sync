"""Context-aware hints for CLI responses.

Generates next-step suggestions after a command by looking up the command in
``_WORKFLOW``. Hints are appended as ``_hints`` so the CLI can point at follow-up
commands the user doesn't have to discover.

The lookup is keyed by the command's name alone: there is no history, no
accumulated state, and no dependence on anything but the current payload.
"""

from __future__ import annotations

from typing import Any

# ---- workflow adjacency: for each command, which commands are useful next ----

_WORKFLOW: dict[str, list[dict[str, str]]] = {
    "flows list": [
        {
            "command": "flows show",
            "suggestion": "Drill into a specific flow for step-by-step details",
        },
        {
            "command": "change flows",
            "suggestion": "Check which flows are affected by recent changes",
        },
        {
            "command": "arch overview",
            "suggestion": "See the high-level architecture",
        },
    ],
    "flows show": [
        {
            "command": "query callers_of",
            "suggestion": "Inspect callers/callees of a step in this flow",
        },
        {
            "command": "change flows",
            "suggestion": "Check if changes affect this flow",
        },
        {
            "command": "flows list",
            "suggestion": "Browse other execution flows",
        },
    ],
    "change flows": [
        {
            "command": "change risk",
            "suggestion": "Get risk-scored change analysis",
        },
        {
            "command": "flows show",
            "suggestion": "Inspect a specific affected flow",
        },
        {
            "command": "change impact",
            "suggestion": "Expand the blast radius analysis",
        },
    ],
    "arch communities": [
        {
            "command": "arch community",
            "suggestion": "Inspect a specific community's members",
        },
        {
            "command": "arch overview",
            "suggestion": "See cross-community coupling and warnings",
        },
        {
            "command": "flows list",
            "suggestion": "See execution flows across communities",
        },
    ],
    "arch community": [
        {
            "command": "query callers_of",
            "suggestion": "Explore callers/callees of community members",
        },
        {
            "command": "arch communities",
            "suggestion": "Browse other communities",
        },
        {
            "command": "arch overview",
            "suggestion": "See how this community fits the architecture",
        },
    ],
    "arch overview": [
        {
            "command": "arch communities",
            "suggestion": "Drill into individual communities",
        },
        {
            "command": "change risk",
            "suggestion": "See how recent changes affect the architecture",
        },
        {
            "command": "flows list",
            "suggestion": "Explore execution flows",
        },
    ],
    "change risk": [
        {
            "command": "change flows",
            "suggestion": "See which execution flows are affected",
        },
        {
            "command": "change impact",
            "suggestion": "Expand the blast radius analysis",
        },
        {
            "command": "refactor suggest",
            "suggestion": "Look for refactoring opportunities in changed code",
        },
    ],
    "refactor rename": [
        {
            "command": "query callers_of",
            "suggestion": "Verify call sites before applying a rename",
        },
        {
            "command": "change risk",
            "suggestion": "Check risk of the refactored code",
        },
        {
            "command": "search",
            "suggestion": "Find related symbols to also rename",
        },
    ],
    "search": [
        {
            "command": "query callers_of",
            "suggestion": "Inspect callers/callees of a search result",
        },
        {
            "command": "flows show",
            "suggestion": "See the execution flow through a matched node",
        },
        {
            "command": "change impact",
            "suggestion": "Check the blast radius from matched nodes",
        },
    ],
    "arch hubs": [
        {
            "command": "change impact",
            "suggestion": "Check blast radius of a hub",
        },
        {
            "command": "query callers_of",
            "suggestion": "See what calls a hub",
        },
        {
            "command": "arch bridges",
            "suggestion": "Find architectural chokepoints",
        },
    ],
    "arch bridges": [
        {
            "command": "arch hubs",
            "suggestion": "Find most connected nodes",
        },
        {
            "command": "change impact",
            "suggestion": "Check blast radius",
        },
        {
            "command": "change risk",
            "suggestion": "See if bridges are affected",
        },
    ],
    "arch surprises": [
        {
            "command": "arch overview",
            "suggestion": "See the community structure",
        },
        {
            "command": "query callers_of",
            "suggestion": "Trace the coupling",
        },
        {
            "command": "arch bridges",
            "suggestion": "Find chokepoints",
        },
    ],
    "health knowledge-gaps": [
        {
            "command": "health dead-code",
            "suggestion": "Find unused symbols",
        },
        {
            "command": "arch hubs",
            "suggestion": "Find high-impact nodes",
        },
        {
            "command": "health suggested-questions",
            "suggestion": "Review open questions",
        },
    ],
    "health dead-code": [
        {
            "command": "change impact",
            "suggestion": "Confirm nothing depends on it",
        },
        {
            "command": "query callers_of",
            "suggestion": "Double-check the call sites",
        },
    ],
    "health suggested-questions": [
        {
            "command": "health knowledge-gaps",
            "suggestion": "Find structural weaknesses",
        },
        {
            "command": "change risk",
            "suggestion": "Get a risk-scored review",
        },
        {
            "command": "arch overview",
            "suggestion": "See the community map",
        },
    ],
    "refactor suggest": [
        {
            "command": "refactor rename",
            "suggestion": "Preview a rename before applying it",
        },
        {
            "command": "arch communities",
            "suggestion": "See which community the code belongs to",
        },
        {
            "command": "health dead-code",
            "suggestion": "Check whether the target is still called",
        },
    ],
}

# Maximum items per hints category returned to the caller.
_MAX_PER_CATEGORY = 3


# ---------------------------------------------------------------------------
# Hints generation
# ---------------------------------------------------------------------------


def generate_hints(command: str, result: dict[str, Any]) -> dict[str, Any]:
    """Build follow-up hints for a command result.

    Returns::

        {
            "next_steps": [{"command": ..., "suggestion": ...}, ...],
            "related": [...],
            "warnings": [...],
        }

    At most ``_MAX_PER_CATEGORY`` items per list. Commands without a workflow
    entry get an empty ``next_steps``; their warnings and related identifiers
    are still filled in.
    """
    return {
        "next_steps": _build_next_steps(command)[:_MAX_PER_CATEGORY],
        "related": _build_related(result)[:_MAX_PER_CATEGORY],
        "warnings": _extract_warnings(result)[:_MAX_PER_CATEGORY],
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_next_steps(command: str) -> list[dict[str, str]]:
    """Return the workflow suggestions for *command*."""
    return list(_WORKFLOW.get(command, ()))


def _extract_warnings(result: dict[str, Any]) -> list[str]:
    """Pull warning signals from a command result."""
    warnings: list[str] = []

    # Test gaps
    test_gaps = result.get("test_gaps")
    if isinstance(test_gaps, list) and test_gaps:
        names = [g.get("name", g) if isinstance(g, dict) else str(g) for g in test_gaps[:5]]
        warnings.append(
            f"Test coverage gaps: {', '.join(names)}"
        )

    # High risk score
    risk = result.get("risk_score")
    if isinstance(risk, (int, float)) and risk > 0.7:
        warnings.append(f"High risk score ({risk:.2f}) — review carefully")

    # Coupling warnings from architecture overview
    arch_warnings = result.get("warnings")
    if isinstance(arch_warnings, list):
        for w in arch_warnings[:3]:
            if isinstance(w, str):
                warnings.append(w)
            elif isinstance(w, dict) and "message" in w:
                warnings.append(w["message"])

    return warnings


def _build_related(result: dict[str, Any]) -> list[str]:
    """Suggest impacted files from the result."""
    related: list[str] = []
    seen: set[str] = set()

    impacted = result.get("impacted_files")
    if isinstance(impacted, list):
        for f in impacted:
            if isinstance(f, str) and f not in seen:
                related.append(f)
                seen.add(f)
                if len(related) >= _MAX_PER_CATEGORY:
                    break

    return related
