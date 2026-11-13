"""Graph shape: communities, hubs, bridges, and surprising coupling."""

from __future__ import annotations

from typing import Annotated, Any, Optional

import typer

from ..analysis import (
    find_bridge_nodes,
    find_hub_nodes,
    find_surprising_connections,
)
from ..communities import (
    find_community,
    get_architecture_overview,
    get_communities,
)
from ..incremental import get_db_path
from .common import _emit, _root, _store, _with_hints
from .options import FormatOption, NoColorOption, PrettyOption, RootOption
from .suggestions import for_command

arch_app = typer.Typer(
    name="arch",
    help="Graph shape: communities, hubs, bridges, and surprising coupling.",
    no_args_is_help=True,
)


def _require_graph(repo_root: Any) -> None:
    """Fail cleanly instead of opening an empty database."""
    if not get_db_path(repo_root).exists():
        typer.echo("error: graph not built yet. Run `code-graph build` first.", err=True)
        raise typer.Exit(code=1)


@arch_app.command("communities")
def arch_communities(
    sort_by: Annotated[str, typer.Option(help="size|cohesion|name")] = "size",
    min_size: Annotated[int, typer.Option(help="Minimum community size.")] = 0,
    root: RootOption = None,
    output: FormatOption = "json",
    pretty: PrettyOption = False,
    no_color: NoColorOption = False,
) -> None:
    """List detected code communities."""
    repo_root = _root(root)
    _require_graph(repo_root)
    with _store(repo_root) as store:
        communities = get_communities(store, sort_by=sort_by, min_size=min_size)
    payload: dict[str, Any] = {
        "status": "ok",
        "summary": f"{len(communities)} community(ies)",
        "communities": communities,
    }
    _emit(
        _with_hints(payload, "arch communities"),
        output_format=output,
        pretty=pretty,
        no_color=no_color,
    )
@arch_app.command("community")
def arch_community(
    community_id: Annotated[Optional[int], typer.Option("--id", help="Community id.")] = None,
    name: Annotated[Optional[str], typer.Option("--name", help="Name substring.")] = None,
    members: Annotated[bool, typer.Option(help="Include member details.")] = False,
    root: RootOption = None,
    output: FormatOption = "json",
    pretty: PrettyOption = False,
    no_color: NoColorOption = False,
) -> None:
    """Show one community by id or name."""
    repo_root = _root(root)
    _require_graph(repo_root)
    with _store(repo_root) as store:
        community = find_community(
            store, name=name, community_id=community_id, include_members=members
        )

    if community is None:
        payload: dict[str, Any] = {
            "status": "not_found",
            "summary": "No community found matching the given criteria.",
        }
    else:
        payload = {
            "status": "ok",
            "summary": (
                f"Community '{community['name']}': {community['size']} nodes, "
                f"cohesion {community['cohesion']:.4f}"
            ),
            "community": community,
        }
        payload = _with_hints(payload, "arch community")
    _emit(payload, output_format=output, pretty=pretty, no_color=no_color)
@arch_app.command("overview")
def arch_overview(
    root: RootOption = None,
    output: FormatOption = "json",
    pretty: PrettyOption = False,
    no_color: NoColorOption = False,
) -> None:
    """Show the architecture overview derived from communities."""
    repo_root = _root(root)
    _require_graph(repo_root)
    with _store(repo_root) as store:
        overview = get_architecture_overview(store)
    payload: dict[str, Any] = {
        "status": "ok",
        "summary": (
            f"Architecture: {len(overview['communities'])} communities, "
            f"{len(overview['cross_community_edges'])} cross-community edges, "
            f"{len(overview['warnings'])} warning(s)"
        ),
        **overview,
    }
    _emit(
        _with_hints(payload, "arch overview"),
        output_format=output,
        pretty=pretty,
        no_color=no_color,
    )
@arch_app.command("hubs")
def arch_hubs(
    top_n: Annotated[int, typer.Option(help="Number of hubs.")] = 10,
    root: RootOption = None,
    output: FormatOption = "json",
    pretty: PrettyOption = False,
    no_color: NoColorOption = False,
) -> None:
    """Find the most connected nodes (architectural hotspots)."""
    repo_root = _root(root)
    with _store(repo_root) as store:
        hubs = find_hub_nodes(store, top_n=top_n)
    payload: dict[str, Any] = {
        "hub_nodes": hubs,
        "count": len(hubs),
        "next_command_suggestions": for_command("arch hubs"),
    }
    _emit(_with_hints(payload, "arch hubs"), output_format=output, pretty=pretty, no_color=no_color)
@arch_app.command("bridges")
def arch_bridges(
    top_n: Annotated[int, typer.Option(help="Number of bridges.")] = 10,
    root: RootOption = None,
    output: FormatOption = "json",
    pretty: PrettyOption = False,
    no_color: NoColorOption = False,
) -> None:
    """Find architectural chokepoints via betweenness centrality."""
    repo_root = _root(root)
    with _store(repo_root) as store:
        bridges = find_bridge_nodes(store, top_n=top_n)
    payload: dict[str, Any] = {
        "bridge_nodes": bridges,
        "count": len(bridges),
        "next_command_suggestions": for_command("arch bridges"),
    }
    _emit(_with_hints(payload, "arch bridges"), output_format=output, pretty=pretty, no_color=no_color)
@arch_app.command("surprises")
def arch_surprises(
    top_n: Annotated[int, typer.Option(help="Number of results.")] = 15,
    root: RootOption = None,
    output: FormatOption = "json",
    pretty: PrettyOption = False,
    no_color: NoColorOption = False,
) -> None:
    """Find unexpected cross-community or cross-language coupling."""
    repo_root = _root(root)
    with _store(repo_root) as store:
        surprises = find_surprising_connections(store, top_n=top_n)
    payload: dict[str, Any] = {
        "surprising_connections": surprises,
        "count": len(surprises),
        "next_command_suggestions": for_command("arch surprises"),
    }
    _emit(_with_hints(payload, "arch surprises"), output_format=output, pretty=pretty, no_color=no_color)
