"""Diff-driven analysis: change risk, impact, and touched flows."""

from __future__ import annotations

from typing import Annotated, Optional

import typer

from ..analysis import get_impact_radius
from ..changes import detect_changes
from ..flows import affected_flows
from .common import _emit, _root, _store, _with_hints
from .options import FormatOption, NoColorOption, PrettyOption, RootOption

change_app = typer.Typer(
    name="change",
    help="Diff-driven analysis: what changed, what it touches, how risky it is.",
    no_args_is_help=True,
)

@change_app.command("impact")
def change_impact(
    changed_files: Annotated[
        Optional[list[str]], typer.Argument(help="Changed files (auto-detected if omitted).")
    ] = None,
    base: Annotated[str, typer.Option(help="Git ref to diff against.")] = "HEAD~1",
    max_depth: Annotated[int, typer.Option(help="Traversal depth.")] = 2,
    root: RootOption = None,
    output: FormatOption = "json",
    pretty: PrettyOption = False,
    no_color: NoColorOption = False,
) -> None:
    """Show the blast radius of changed files."""
    repo_root = _root(root)
    _emit(
        get_impact_radius(
            changed_files=changed_files or None,
            max_depth=max_depth,
            repo_root=str(repo_root),
            base=base,
        ),
        output_format=output,
        pretty=pretty,
        no_color=no_color,
    )
@change_app.command("risk")
def change_risk(
    changed_files: Annotated[
        Optional[list[str]], typer.Argument(help="Changed files (auto-detected if omitted).")
    ] = None,
    base: Annotated[str, typer.Option(help="Git ref to diff against.")] = "HEAD~1",
    max_depth: Annotated[int, typer.Option(help="Impact traversal depth.")] = 2,
    include_source: Annotated[bool, typer.Option(help="Include source snippets.")] = False,
    root: RootOption = None,
    output: FormatOption = "json",
    pretty: PrettyOption = False,
    no_color: NoColorOption = False,
) -> None:
    """Risk-scored change detection across functions, flows, and tests."""
    repo_root = _root(root)
    with _store(repo_root) as store:
        result = detect_changes(
            store,
            repo_root,
            base=base,
            changed_files=changed_files or None,
            include_source=include_source,
            max_depth=max_depth,
        )
    _emit(
        _with_hints(result, "change risk"),
        output_format=output,
        pretty=pretty,
        no_color=no_color,
    )
@change_app.command("flows")
def change_flows(
    changed_files: Annotated[
        Optional[list[str]], typer.Argument(help="Changed files (auto-detected if omitted).")
    ] = None,
    base: Annotated[str, typer.Option(help="Git ref to diff against.")] = "HEAD~1",
    root: RootOption = None,
    output: FormatOption = "json",
    pretty: PrettyOption = False,
    no_color: NoColorOption = False,
) -> None:
    """Find execution flows touched by changed files."""
    repo_root = _root(root)
    with _store(repo_root) as store:
        result = affected_flows(
            store, repo_root, changed_files=changed_files or None, base=base
        )
    _emit(
        _with_hints(result, "change flows"),
        output_format=output,
        pretty=pretty,
        no_color=no_color,
    )
