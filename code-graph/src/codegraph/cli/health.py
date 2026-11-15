"""Weakness diagnostics over the whole graph."""

from __future__ import annotations

from typing import Annotated, Any, Optional

import typer

from ..query.analysis import find_knowledge_gaps, find_large_functions
from ..query.analysis import generate_suggested_questions
from ..refactor import find_dead_code
from .common import _emit, _root, _store, _with_hints
from .options import FormatOption, NoColorOption, PrettyOption, RootOption

health_app = typer.Typer(
    name="health",
    help="Weakness diagnostics over the whole graph: unused, oversized, untested, uncharted.",
    no_args_is_help=True,
)

@health_app.command("dead-code")
def dead_code(
    kind: Annotated[Optional[str], typer.Option(help="Node kind filter.")] = None,
    file_pattern: Annotated[Optional[str], typer.Option(help="File path substring.")] = None,
    root: RootOption = None,
    output: FormatOption = "json",
    pretty: PrettyOption = False,
    no_color: NoColorOption = False,
) -> None:
    """Find unreferenced functions and classes."""
    repo_root = _root(root)
    with _store(repo_root) as store:
        dead = find_dead_code(store, kind=kind, file_pattern=file_pattern, root=repo_root)
    payload: dict[str, Any] = {
        "status": "ok",
        "summary": f"Found {len(dead)} dead code symbol(s).",
        "dead_code": dead,
        "total": len(dead),
    }
    _emit(_with_hints(payload, "health dead-code"), output_format=output, pretty=pretty, no_color=no_color)
@health_app.command("large-functions")
def large_functions(
    min_lines: Annotated[int, typer.Option(help="Minimum span in lines.")] = 50,
    limit: Annotated[int, typer.Option(help="Maximum results.")] = 20,
    root: RootOption = None,
    output: FormatOption = "json",
    pretty: PrettyOption = False,
    no_color: NoColorOption = False,
) -> None:
    """Find oversized functions and classes."""
    repo_root = _root(root)
    _emit(
        find_large_functions(min_lines=min_lines, limit=limit, repo_root=str(repo_root)),
        output_format=output,
        pretty=pretty,
        no_color=no_color,
    )
@health_app.command("knowledge-gaps")
def knowledge_gaps(
    root: RootOption = None,
    output: FormatOption = "json",
    pretty: PrettyOption = False,
    no_color: NoColorOption = False,
) -> None:
    """Find structural weaknesses: isolated nodes, thin communities, untested hotspots."""
    repo_root = _root(root)
    with _store(repo_root) as store:
        gaps = find_knowledge_gaps(store)
    payload: dict[str, Any] = {
        "gaps": gaps,
        "total_gaps": sum(len(v) for v in gaps.values()),
        "summary": {name: len(items) for name, items in gaps.items()},
    }
    _emit(_with_hints(payload, "health knowledge-gaps"), output_format=output, pretty=pretty, no_color=no_color)
@health_app.command("suggested-questions")
def suggested_questions(
    root: RootOption = None,
    output: FormatOption = "json",
    pretty: PrettyOption = False,
    no_color: NoColorOption = False,
) -> None:
    """Generate review questions from graph structure."""
    repo_root = _root(root)
    with _store(repo_root) as store:
        questions = generate_suggested_questions(store)
    payload: dict[str, Any] = {
        "questions": questions,
        "count": len(questions),
        "by_priority": {
            priority: sum(1 for q in questions if q.get("priority") == priority)
            for priority in ("high", "medium", "low")
        },
    }
    _emit(_with_hints(payload, "health suggested-questions"), output_format=output, pretty=pretty, no_color=no_color)
