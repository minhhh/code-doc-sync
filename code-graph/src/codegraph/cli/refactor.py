"""Rename previews and refactor suggestions."""

from __future__ import annotations

from typing import Annotated, Any

import typer

from ..refactor import apply_refactor, rename_preview, suggest_refactorings
from .common import _emit, _root, _store, _with_hints
from .options import FormatOption, NoColorOption, PrettyOption, RootOption

refactor_app = typer.Typer(
    name="refactor",
    help="Rename previews and refactor suggestions.",
    no_args_is_help=True,
)

@refactor_app.command("rename")
def refactor_rename(
    old_name: Annotated[str, typer.Argument(help="Current symbol name.")],
    new_name: Annotated[str, typer.Argument(help="New symbol name.")],
    root: RootOption = None,
    output: FormatOption = "json",
    pretty: PrettyOption = False,
    no_color: NoColorOption = False,
) -> None:
    """Preview a symbol rename (no files are modified)."""
    repo_root = _root(root)
    with _store(repo_root) as store:
        preview = rename_preview(store, old_name, new_name)

    if preview is None:
        payload: dict[str, Any] = {
            "status": "not_found",
            "summary": f"No node found matching '{old_name}'.",
        }
    else:
        payload = {
            "status": "ok",
            "summary": (
                f"Rename preview: {old_name} -> {new_name}, "
                f"{len(preview['edits'])} edit(s). "
                f"Use `code-graph refactor apply {preview['refactor_id']}` to apply."
            ),
            **preview,
        }
        payload = _with_hints(payload, "refactor rename")
    _emit(payload, output_format=output, pretty=pretty, no_color=no_color)
@refactor_app.command("suggest")
def refactor_suggest(
    root: RootOption = None,
    output: FormatOption = "json",
    pretty: PrettyOption = False,
    no_color: NoColorOption = False,
) -> None:
    """Suggest refactorings from community structure."""
    repo_root = _root(root)
    with _store(repo_root) as store:
        suggestions = suggest_refactorings(store)
    payload: dict[str, Any] = {
        "status": "ok",
        "summary": f"Generated {len(suggestions)} refactoring suggestion(s).",
        "suggestions": suggestions,
        "total": len(suggestions),
    }
    _emit(
        _with_hints(payload, "refactor suggest"),
        output_format=output,
        pretty=pretty,
        no_color=no_color,
    )
@refactor_app.command("apply")
def refactor_apply(
    refactor_id: Annotated[str, typer.Argument(help="Id from a rename preview.")],
    dry_run: Annotated[bool, typer.Option(help="Show a diff without writing.")] = False,
    root: RootOption = None,
    output: FormatOption = "json",
    pretty: PrettyOption = False,
    no_color: NoColorOption = False,
) -> None:
    """Apply a previously previewed rename."""
    repo_root = _root(root)
    _emit(
        apply_refactor(refactor_id, repo_root, dry_run=dry_run),
        output_format=output,
        pretty=pretty,
        no_color=no_color,
    )
