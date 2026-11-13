"""ctx-sync CLI: reconcile code-graph dumps with CONTEXT.md."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Optional

import typer

from . import __version__
from .flows_doc import render_document as render_flow_document
from .graph import (
    ContextTypeError,
    GraphInputError,
    canonical_context_type,
    entities_for_context,
    flows_for_context,
    load_graph,
)
from .markdown import parse_document
from .sync import sync_document

app = typer.Typer(
    name="ctx-sync",
    help="Refresh CONTEXT.md from a code-graph dump without touching human notes.",
    add_completion=False,
    no_args_is_help=False,
)

TargetOption = Annotated[
    Path,
    typer.Option("--target", "-t", help="CONTEXT.md to update."),
]
InputOption = Annotated[
    Optional[Path],
    typer.Option("--input", "-i", help="Read a graph dump JSON file instead of stdin."),
]
RootOption = Annotated[
    Optional[Path],
    typer.Option("--root", help="Repo root for relative paths (default: target's directory)."),
]


@app.callback(invoke_without_command=True)
def sync(
    ctx: typer.Context,
    target: TargetOption = Path("CONTEXT.md"),
    context_type: Annotated[
        str,
        typer.Option(
            "--type",
            "--context-type",
            "-c",
            help="Context projection to sync (call-graph, community, flows).",
        ),
    ] = "call-graph",
    input: InputOption = None,
    update: Annotated[
        bool,
        typer.Option("--update", help="Run `code-graph dump` against --root and use its output."),
    ] = False,
    root: RootOption = None,
    check: Annotated[
        bool,
        typer.Option("--check", help="Exit 1 if CONTEXT.md would change; write nothing."),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print the merged document instead of writing it."),
    ] = False,
    quiet: Annotated[bool, typer.Option("--quiet", "-q")] = False,
) -> None:
    """Merge a code-graph dump into CONTEXT.md."""
    if ctx.invoked_subcommand is not None:
        return
    repo_root = (root or target.parent or Path.cwd()).resolve()
    try:
        context_type = canonical_context_type(context_type)
        payload = _read_payload(
            input=input, update=update, root=repo_root, graph_type=context_type
        )
        if context_type == "flows":
            flows: list[dict] | None = flows_for_context(payload, root=repo_root)
            entities = []
        else:
            flows = None
            entities = entities_for_context(context_type, payload, root=repo_root)
    except (GraphInputError, ContextTypeError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)

    old_text = target.read_text(encoding="utf-8") if target.exists() else ""

    if flows is not None:
        _write_flow_document(
            target,
            old_text,
            flows,
            check=check,
            dry_run=dry_run,
            quiet=quiet,
        )
        return

    result = sync_document(parse_document(old_text), entities, context_type=context_type)
    changed = result.text != old_text

    if dry_run:
        typer.echo(result.text, nl=False)
        return
    if check:
        if changed:
            typer.echo(f"{target} is out of date.", err=True)
            raise typer.Exit(code=1)
        if not quiet:
            typer.echo(f"{target} is up to date.")
        return

    if changed:
        _atomic_write(target, result.text)
    if not quiet:
        typer.echo(
            f"{target} [{context_type}]: {result.updated} updated, {result.added} added, "
            f"{result.unchanged} unchanged, {result.ignored} ignored",
            err=True,
        )


def _write_flow_document(
    target: Path,
    old_text: str,
    flows: list[dict],
    *,
    check: bool,
    dry_run: bool,
    quiet: bool,
) -> None:
    """Splice JSON flow blocks, leaving the rest of the file untouched."""
    result = render_flow_document(old_text, flows)
    if dry_run:
        typer.echo(result.text, nl=False)
        return
    if check:
        if result.changed:
            typer.echo(f"{target} is out of date.", err=True)
            raise typer.Exit(code=1)
        if not quiet:
            typer.echo(f"{target} is up to date.")
        return
    if result.changed:
        _atomic_write(target, result.text)
    if not quiet:
        typer.echo(
            f"{target} [flows]: {result.updated} updated, {result.added} added, "
            f"{result.unchanged} unchanged, {result.ignored} ignored",
            err=True,
        )


def _read_payload(*, input: Path | None, update: bool, root: Path, graph_type: str) -> dict:
    if input is not None:
        return load_graph(path=input)
    if update:
        return load_graph(text=_run_code_graph(root, graph_type))
    if not sys.stdin.isatty():
        return load_graph(text=sys.stdin.read())
    raise GraphInputError(
        "no graph input: pass --input, use --update, or pipe `code-graph export` on stdin"
    )


def _run_code_graph(root: Path, graph_type: str) -> str:
    executable = shutil.which("code-graph")
    if executable is None:
        raise GraphInputError(
            "`--update` needs code-graph on PATH; otherwise pipe `code-graph export` "
            "or use --input"
        )
    command = [executable, "export", "--format", "json", "--root", str(root)]
    # Older code-graph builds only speak call-graph, so leave their default alone.
    if graph_type != "call-graph":
        command.extend(["--type", graph_type])
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise GraphInputError(f"cannot run code-graph: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"exit code {completed.returncode}"
        raise GraphInputError(f"code-graph export failed: {detail}")
    return completed.stdout


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


@app.command()
def version() -> None:
    """Print the ctx-sync version."""
    typer.echo(__version__)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
