"""Follow-up commands offered by commands that report findings."""

from __future__ import annotations

#: Producing command -> ``<command> -- <why>`` suggestions.
SUGGESTIONS: dict[str, list[str]] = {
    "arch hubs": [
        "change impact -- check blast radius of a hub",
        "query callers_of -- see what calls a hub",
        "arch bridges -- find architectural chokepoints",
    ],
    "arch bridges": [
        "arch hubs -- find most connected nodes",
        "change impact -- check blast radius",
        "change risk -- see if bridges are affected",
    ],
    "arch surprises": [
        "arch overview -- community structure",
        "query callers_of -- trace the coupling",
        "arch bridges -- find chokepoints",
    ],
    "health knowledge-gaps": [
        "health dead-code -- find unused symbols",
        "arch hubs -- find high-impact nodes",
        "health suggested-questions -- review questions",
    ],
    "health dead-code": [
        "change impact -- confirm nothing depends on it",
        "query callers_of -- double-check the call sites",
    ],
    "health suggested-questions": [
        "health knowledge-gaps -- structural weaknesses",
        "change risk -- risk-scored review",
        "arch overview -- community map",
    ],
}


def for_command(command: str) -> list[str]:
    """Suggestions to attach to *command*'s payload, if it reports findings."""
    return SUGGESTIONS.get(command, [])
