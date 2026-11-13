"""Shared CLI helpers: root resolution, store lifecycle, payload emission."""

from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

import typer

from ..graph import GraphStore
from .hints import generate_hints, get_session
from ..incremental import find_project_root, get_db_path

def _root(root: Optional[Path]) -> Path:
    """Resolve the repository root; all graph data lives under it."""
    return find_project_root(root.resolve() if root is not None else None)
def _color_enabled(no_color: bool) -> bool:
    """Color only on an interactive terminal that has not opted out."""
    return not no_color and "NO_COLOR" not in os.environ and sys.stdout.isatty()
def _json_text(payload: dict[str, object], *, pretty: bool) -> str:
    if pretty:
        return json.dumps(payload, indent=2, sort_keys=False, ensure_ascii=False)
    return json.dumps(payload, separators=(",", ":"), sort_keys=False, ensure_ascii=False)
def _emit(
    payload: dict[str, object],
    *,
    output_format: str = "json",
    pretty: bool = False,
    no_color: bool = False,
) -> None:
    status = payload.get("status")
    if output_format == "text":
        summary = payload.get("summary")
        text = summary if isinstance(summary, str) else _json_text(payload, pretty=pretty)
        if _color_enabled(no_color):
            typer.secho(text, fg=typer.colors.CYAN)
        else:
            typer.echo(text)
    else:
        typer.echo(_json_text(payload, pretty=pretty))
    if status in ("error", "not_found"):
        raise typer.Exit(code=1)


@contextmanager
def _store(repo_root: Path) -> Iterator[GraphStore]:
    """Open the graph store for a root and close it on the way out."""
    store = GraphStore(get_db_path(repo_root))
    try:
        yield store
    finally:
        store.close()


def _with_hints(payload: dict[str, Any], command: str) -> dict[str, Any]:
    """Attach session-derived follow-up hints to a command payload."""
    payload["_hints"] = generate_hints(command, payload, get_session())
    return payload
