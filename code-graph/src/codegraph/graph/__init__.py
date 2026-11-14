"""The code graph artifact: nodes, edges, and their SQLite store.

``graph_store`` holds the implementation; this package re-exports the names
other modules import so ``from codegraph.graph import GraphStore`` keeps working.
"""

from __future__ import annotations

from .graph_store import (
    FlowAdjacency,
    GraphEdge,
    GraphNode,
    GraphStore,
    _sanitize_name,
    edge_to_dict,
    node_to_dict,
)

__all__ = [
    "FlowAdjacency",
    "GraphEdge",
    "GraphNode",
    "GraphStore",
    "edge_to_dict",
    "node_to_dict",
    "_sanitize_name",
]
