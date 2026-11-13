"""Context-aware hints system for CLI responses.

Tracks session state (in-memory only) and generates intelligent
next-step suggestions after each command.  Hints are appended as
``_hints`` to a response so the CLI can suggest follow-up commands
without the user having to discover them.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any

# ---- intent categories and their characteristic command names ----

_INTENT_COMMANDS: dict[str, set[str]] = {
    "reviewing": {"change risk", "change flows", "change impact"},
    "debugging": {"query callers_of", "flows show", "search"},
    "refactoring": {"refactor rename", "health dead-code", "refactor suggest"},
    "exploring": {"arch communities", "arch overview", "flows list", "status"},
}

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
}

# Maximum items per hints category returned to the caller.
_MAX_PER_CATEGORY = 3

# Session history caps.
_MAX_COMMAND_HISTORY = 100
_MAX_NODES_TRACKED = 1000


# ---------------------------------------------------------------------------
# SessionState
# ---------------------------------------------------------------------------


class SessionState:
    """In-memory session state for a single CLI session."""

    def __init__(self) -> None:
        self.commands_run: deque[str] = deque(maxlen=_MAX_COMMAND_HISTORY)
        self.nodes_queried: set[str] = set()
        self.files_touched: set[str] = set()
        self.inferred_intent: str | None = None
        self.last_command_time: float = 0.0

    def record_command(self, command: str) -> None:
        """Record a command invocation (FIFO, capped at 100)."""
        self.commands_run.append(command)
        self.last_command_time = time.time()

    def record_nodes(self, node_ids: list[str]) -> None:
        """Record queried node identifiers (capped at 1000)."""
        for nid in node_ids:
            if len(self.nodes_queried) >= _MAX_NODES_TRACKED:
                break
            self.nodes_queried.add(nid)

    def record_files(self, files: list[str]) -> None:
        """Record touched file paths."""
        self.files_touched.update(files)


# ---------------------------------------------------------------------------
# Intent inference
# ---------------------------------------------------------------------------


def infer_intent(session: SessionState) -> str:
    """Classify the user's likely intent from the commands already run.

    Returns one of: ``"reviewing"``, ``"debugging"``, ``"refactoring"``,
    ``"exploring"`` (default).
    """
    if not session.commands_run:
        return "exploring"

    # Score each intent by how many of the last N commands match its commands.
    recent = list(session.commands_run)[-10:]
    scores: dict[str, int] = {intent: 0 for intent in _INTENT_COMMANDS}
    for command in recent:
        for intent, commands in _INTENT_COMMANDS.items():
            if command in commands:
                scores[intent] += 1

    best = max(scores, key=lambda k: scores[k])
    if scores[best] == 0:
        return "exploring"
    return best


# ---------------------------------------------------------------------------
# Hints generation
# ---------------------------------------------------------------------------


def generate_hints(
    command: str,
    result: dict[str, Any],
    session: SessionState,
) -> dict[str, Any]:
    """Build context-aware hints for a command result.

    Returns::

        {
            "next_steps": [{"command": ..., "suggestion": ...}, ...],
            "related": [...],
            "warnings": [...],
        }

    At most ``_MAX_PER_CATEGORY`` items per list.  Commands already run
    in this session are suppressed from ``next_steps``.
    """
    # Update session state.
    session.record_command(command)
    session.inferred_intent = infer_intent(session)

    next_steps = _build_next_steps(command, session)
    warnings = _extract_warnings(result)
    # Build related BEFORE tracking, so that the current result's files
    # are not yet in files_touched and can appear as suggestions.
    related = _build_related(result, session)

    # Collect files/nodes from result for session tracking.
    _track_result(result, session)

    next_steps = next_steps[:_MAX_PER_CATEGORY]

    return {
        "next_steps": next_steps,
        "related": related[:_MAX_PER_CATEGORY],
        "warnings": warnings[:_MAX_PER_CATEGORY],
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _track_result(result: dict[str, Any], session: SessionState) -> None:
    """Extract node IDs and file paths from a command result and record them."""
    # Files
    for key in ("changed_files", "impacted_files"):
        files = result.get(key)
        if isinstance(files, list):
            session.record_files([f for f in files if isinstance(f, str)])

    # Nodes — look in common result shapes
    node_ids: list[str] = []
    for key in ("results", "changed_nodes", "impacted_nodes"):
        items = result.get(key)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    qn = item.get("qualified_name")
                    if qn:
                        node_ids.append(qn)
    if node_ids:
        session.record_nodes(node_ids)


def _build_next_steps(
    command: str, session: SessionState
) -> list[dict[str, str]]:
    """Return next-step suggestions, filtering already-run commands."""
    called = set(session.commands_run)
    candidates = _WORKFLOW.get(command, [])
    out: list[dict[str, str]] = []
    for c in candidates:
        if c["command"] not in called:
            out.append(c)
    return out


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


def _build_related(
    result: dict[str, Any],
    session: SessionState,
) -> list[str]:
    """Suggest related node/file identifiers from the result."""
    related: list[str] = []
    seen: set[str] = set()

    # Suggest impacted files the user hasn't touched yet
    impacted = result.get("impacted_files")
    if isinstance(impacted, list):
        for f in impacted:
            if isinstance(f, str) and f not in session.files_touched and f not in seen:
                related.append(f)
                seen.add(f)
                if len(related) >= _MAX_PER_CATEGORY:
                    break

    return related


# ---------------------------------------------------------------------------
# Module-level session singleton
# ---------------------------------------------------------------------------

_session = SessionState()


def get_session() -> SessionState:
    """Return the global in-memory session state."""
    return _session

