"""Type-scoped export projections.

Each graph type is a deterministic projection of the same SQLite graph, so
adding a type needs no storage change and no second selection path: the store
is read once, the projection picks which rows and which edge kinds matter.

The vocabulary is shared with ``ctx-sync --type``; canonical names are
``call-graph``, ``community``, and ``flows``.
"""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any

from .query.communities import get_architecture_overview
from .query.flows import get_flows
from .graph import GraphStore, edge_to_dict, node_to_dict

CALL_GRAPH = "call-graph"
COMMUNITY = "community"
FLOWS = "flows"

GRAPH_TYPES = (CALL_GRAPH, COMMUNITY, FLOWS)

GRAPH_TYPE_ALIASES: dict[str, str] = {
    "call-graph": CALL_GRAPH,
    "callgraph": CALL_GRAPH,
    "ctx": CALL_GRAPH,
    "community": COMMUNITY,
    "communities": COMMUNITY,
    "architecture": COMMUNITY,
    "flows": FLOWS,
    "flow": FLOWS,
}

# Members are summarized rather than dumped, so a 400-node graph does not turn
# an architecture document into a member listing.
_TOP_FILES = 5

# Length of the path digest embedded in a flow key. Twelve hex chars keeps
# collisions out of reach for any realistic flow count while staying readable.
_FLOW_KEY_DIGEST = 12


class GraphTypeError(ValueError):
    """Raised for an unknown or unsupported ``--type``."""


def canonical_graph_type(value: str) -> str:
    """Canonicalize a ``--type`` spelling, or raise :class:`GraphTypeError`."""
    canonical = GRAPH_TYPE_ALIASES.get(value.strip().lower())
    if canonical is None:
        supported = ", ".join(GRAPH_TYPES)
        aliases = ", ".join(
            sorted(name for name in GRAPH_TYPE_ALIASES if name not in GRAPH_TYPES)
        )
        raise GraphTypeError(
            f"unknown --type '{value}'; supported: {supported} (aliases: {aliases})"
        )
    return canonical


def flow_key(entry_point: str, path: list[str]) -> str:
    """Stable text identity for one flow.

    The digest covers the ordered qualified names of the traced path, so the key
    survives rebuilds, where the integer flow id is reissued. Tracing emits one
    flow per entry point, so the path digest is a stability measure rather than
    a disambiguator.
    """
    digest = hashlib.sha1(
        "\n".join(path).encode("utf-8"), usedforsecurity=False
    ).hexdigest()[:_FLOW_KEY_DIGEST]
    return f"{entry_point}#{digest}" if entry_point else digest


def community_key(name: str, members: list[str]) -> str:
    """Stable text identity for one community.

    Community names come from keyword extraction and collide (two communities
    can both be called ``codegraph-find``), while the numeric id is reissued
    on every full rebuild. Digesting the sorted membership gives an identity
    that is both unique and stable.
    """
    digest = hashlib.sha1(
        "\n".join(sorted(members)).encode("utf-8"), usedforsecurity=False
    ).hexdigest()[:_FLOW_KEY_DIGEST]
    return f"{name}#{digest}" if name else digest


def build_payload(
    repo_root: Path,
    store: GraphStore,
    graph_type: str = CALL_GRAPH,
    *,
    limit: int = 50,
) -> dict[str, Any]:
    """Project the graph into the payload for one graph type."""
    canonical = canonical_graph_type(graph_type)
    metadata = {
        key: store.get_metadata(key)
        for key in ("last_updated", "last_build_type", "vcs_branch", "vcs_revision")
    }
    if canonical == COMMUNITY:
        return _community_payload(repo_root, store, metadata)
    if canonical == FLOWS:
        return _flows_payload(repo_root, store, metadata, limit=limit)
    return _call_graph_payload(repo_root, store, metadata)


def _call_graph_payload(
    repo_root: Path,
    store: GraphStore,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    nodes = [node_to_dict(node) for node in store.get_all_nodes(exclude_files=False)]
    edges = [edge_to_dict(edge) for edge in store.get_all_edges()]
    return {
        "status": "ok",
        "root": str(repo_root),
        "type": CALL_GRAPH,
        "summary": f"Graph: {len(nodes)} nodes, {len(edges)} edges",
        "nodes": nodes,
        "edges": edges,
        "metadata": metadata,
    }


def _community_payload(
    repo_root: Path,
    store: GraphStore,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    overview = get_architecture_overview(store)
    raw_communities = overview["communities"]
    files_by_qn = {
        node.qualified_name: node.file_path for node in store.get_all_nodes()
    }

    communities: list[dict[str, Any]] = []
    for community in raw_communities:
        members = community.get("members") or []
        name = community.get("name", "")
        file_counts = Counter(
            files_by_qn[qn] for qn in members if qn in files_by_qn
        )
        communities.append({
            "id": community.get("id", 0),
            "key": community_key(name, members),
            "name": name,
            "level": community.get("level", 0),
            "cohesion": community.get("cohesion", 0.0),
            "size": community.get("size", 0),
            "dominant_language": community.get("dominant_language", ""),
            "description": community.get("description", ""),
            "member_count": len(members),
            "files": [path for path, _ in file_counts.most_common(_TOP_FILES)],
        })

    pairs: Counter[tuple[int, int]] = Counter()
    kinds: dict[tuple[int, int], set[str]] = defaultdict(set)
    for edge in overview["cross_community_edges"]:
        pair = tuple(  # type: ignore[assignment]
            sorted((edge["source_community"], edge["target_community"]))
        )
        pairs[pair] += 1
        kinds[pair].add(edge["edge_kind"])

    inter_community_edges = [
        {
            "source_community": source,
            "target_community": target,
            "edge_count": count,
            "edge_kinds": sorted(kinds[(source, target)]),
        }
        for (source, target), count in pairs.most_common()
    ]

    return {
        "status": "ok",
        "root": str(repo_root),
        "type": COMMUNITY,
        "summary": (
            f"Architecture: {len(communities)} communities, "
            f"{len(inter_community_edges)} inter-community pairs"
        ),
        "communities": communities,
        "inter_community_edges": inter_community_edges,
        "warnings": overview["warnings"],
        "metadata": metadata,
    }


def _flows_payload(
    repo_root: Path,
    store: GraphStore,
    metadata: dict[str, Any],
    *,
    limit: int,
) -> dict[str, Any]:
    calls_out = store.load_flow_adjacency().calls_out
    flows: list[dict[str, Any]] = []
    for row in get_flows(store, limit=limit):
        qns = [
            node.qualified_name
            for node_id in row["path"]
            if (node := store.get_node_by_id(node_id)) is not None
        ]
        if not qns:
            continue
        root = _flow_tree(qns, calls_out)
        flows.append({
            "key": flow_key(qns[0], qns),
            "name": row["name"],
            "entry_point": qns[0],
            "criticality": round(float(row["criticality"]), 4),
            "unique_nodes": len(qns),
            "tree_nodes": _tree_size(root),
            "root": root,
        })

    return {
        "status": "ok",
        "root": str(repo_root),
        "type": FLOWS,
        "summary": f"Flows: {len(flows)} execution paths",
        "flows": flows,
        "metadata": metadata,
    }


def _flow_tree(qns: list[str], calls_out: dict[str, list[str]]) -> dict[str, Any]:
    """Rebuild the BFS tree the trace flattened.

    Children are appended in call order, so a level-order walk of the tree
    reproduces the original BFS sequence. A target that was already expanded
    becomes ``{"ref": qn}``: the edge is kept, the subtree is not duplicated.
    """
    members = set(qns)
    root: dict[str, Any] = {"qn": qns[0], "calls": []}
    expanded = {qns[0]}
    queue = deque([root])
    while queue:
        current = queue.popleft()
        seen_here: set[str] = set()
        for target in calls_out.get(current["qn"], ()):
            if target not in members or target in seen_here:
                continue
            seen_here.add(target)
            if target in expanded:
                current["calls"].append({"ref": target})
                continue
            expanded.add(target)
            child: dict[str, Any] = {"qn": target, "calls": []}
            current["calls"].append(child)
            queue.append(child)
    return root


def _tree_size(node: dict[str, Any]) -> int:
    total = 1
    for child in node.get("calls", ()):
        total += _tree_size(child)
    return total
