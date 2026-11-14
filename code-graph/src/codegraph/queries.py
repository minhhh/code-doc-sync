"""Predefined graph queries: the ``query_graph`` pattern vocabulary."""

from __future__ import annotations

import logging
from typing import Any

from .graph import edge_to_dict, node_to_dict
from .lib.repo import open_store, resolve_graph_file_paths

logger = logging.getLogger(__name__)

# Common JS/TS builtin method names filtered from callers_of results.
# "Who calls .map()?" returns hundreds of hits and is never useful.
# These are kept in the graph (callees_of still shows them) but excluded
# when doing reverse call tracing to reduce noise.
BUILTIN_CALL_NAMES: set[str] = {
    "map", "filter", "reduce", "reduceRight", "forEach", "find", "findIndex",
    "some", "every", "includes", "indexOf", "lastIndexOf",
    "push", "pop", "shift", "unshift", "splice", "slice",
    "concat", "join", "flat", "flatMap", "sort", "reverse", "fill",
    "keys", "values", "entries", "from", "isArray", "of", "at",
    "trim", "trimStart", "trimEnd", "split", "replace", "replaceAll",
    "match", "matchAll", "search", "substring", "substr",
    "toLowerCase", "toUpperCase", "startsWith", "endsWith",
    "padStart", "padEnd", "repeat", "charAt", "charCodeAt",
    "assign", "freeze", "defineProperty", "getOwnPropertyNames",
    "hasOwnProperty", "create", "is", "fromEntries",
    "log", "warn", "error", "info", "debug", "trace", "dir", "table",
    "time", "timeEnd", "assert", "clear", "count",
    "then", "catch", "finally", "resolve", "reject", "all", "allSettled", "race", "any",
    "parse", "stringify",
    "floor", "ceil", "round", "random", "max", "min", "abs", "pow", "sqrt",
    "addEventListener", "removeEventListener", "querySelector", "querySelectorAll",
    "getElementById", "createElement", "appendChild", "removeChild",
    "setAttribute", "getAttribute", "preventDefault", "stopPropagation",
    "setTimeout", "clearTimeout", "setInterval", "clearInterval",
    "toString", "valueOf", "toJSON", "toISOString",
    "getTime", "getFullYear", "now",
    "isNaN", "parseInt", "parseFloat", "toFixed",
    "encodeURIComponent", "decodeURIComponent",
    "call", "apply", "bind", "next",
    "emit", "on", "off", "once",
    "pipe", "write", "read", "end", "close", "destroy",
    "send", "status", "json", "redirect",
    "set", "get", "delete", "has",
    "findUnique", "findFirst", "findMany", "createMany",
    "update", "updateMany", "deleteMany", "upsert",
    "aggregate", "groupBy", "transaction",
    "describe", "it", "test", "expect", "beforeEach", "afterEach",
    "beforeAll", "afterAll", "mock", "spyOn",
    "require", "fetch",
}

_QUERY_PATTERNS = {
    "callers_of": "Find all functions that call a given function",
    "callees_of": "Find all functions called by a given function",
    "imports_of": "Find all imports of a given file or module",
    "importers_of": "Find all files that import a given file or module",
    "children_of": "Find all nodes contained in a file or class",
    "tests_for": "Find all tests for a given function or class",
    "inheritors_of": "Find all classes that inherit from a given class",
    "file_summary": "Get a summary of all nodes in a file",
}


def query_graph(
    pattern: str,
    target: str,
    repo_root: str | None = None,
    detail_level: str = "standard",
    limit: int = 20,
) -> dict[str, Any]:
    """Run a predefined graph query.

    Args:
        pattern: Query pattern. One of: callers_of, callees_of, imports_of,
                 importers_of, children_of, tests_for, inheritors_of, file_summary.
        target: The node name, qualified name, or file path to query about.
        repo_root: Repository root path. Auto-detected if omitted.
        detail_level: "standard" (full output) or "minimal" (name/kind/file only,
                 capped at five rows).
        limit: Maximum rows returned in "standard" mode. ``result_count`` always
                 reports the untruncated total and ``truncated`` says whether rows
                 were dropped.

    Returns:
        Matching nodes and edges for the query.
    """
    limit = max(1, int(limit))
    store, root = open_store(repo_root)
    try:
        if pattern not in _QUERY_PATTERNS:
            return {
                "status": "error",
                "error": (
                    f"Unknown pattern '{pattern}'. "
                    f"Available: {list(_QUERY_PATTERNS.keys())}"
                ),
            }

        results: list[dict] = []
        edges_out: list[dict] = []

        # For callers_of, skip common builtins early (bare names only)
        # "Who calls .map()?" returns hundreds of useless hits.
        # Qualified names (e.g. "utils.py::map") bypass this filter.
        if (
            pattern == "callers_of"
            and target in BUILTIN_CALL_NAMES
            and "::" not in target
        ):
            return {
                "status": "ok", "pattern": pattern, "target": target,
                "description": _QUERY_PATTERNS[pattern],
                "summary": (
                    f"'{target}' is a common builtin "
                    "— callers_of skipped to avoid noise."
                ),
                "results": [], "edges": [],
            }

        # Resolve target - try as-is, then as absolute path, then search.
        # file_summary targets are paths, so skip broad node search.
        node = None
        if pattern != "file_summary":
            node = store.get_node(target)
            if not node:
                abs_target = str(root / target)
                node = store.get_node(abs_target)
            if not node:
                # Search by name
                candidates = store.search_nodes(target, limit=5)
                if len(candidates) == 1:
                    node = candidates[0]
                    target = node.qualified_name
                elif len(candidates) > 1:
                    return {
                        "status": "ambiguous",
                        "summary": (
                            f"Multiple matches for '{target}'. "
                            "Please use a qualified name."
                        ),
                        "candidates": [node_to_dict(c) for c in candidates],
                    }

        if not node and pattern != "file_summary":
            return {
                "status": "not_found",
                "summary": f"No node found matching '{target}'.",
            }

        qn = node.qualified_name if node else target

        if pattern == "callers_of":
            seen_sources: set[str] = set()
            for e in store.get_edges_by_target(qn):
                if e.kind == "CALLS":
                    if e.source_qualified not in seen_sources:
                        seen_sources.add(e.source_qualified)
                        caller = store.get_node(e.source_qualified)
                        if caller:
                            results.append(node_to_dict(caller))
                        edges_out.append(edge_to_dict(e))
            # Fallback: CALLS edges store unqualified target names
            # (e.g. "generateTestCode") while qn is fully qualified
            # (e.g. "file.ts::generateTestCode"). Search by plain name too.
            if node:
                for e in store.search_edges_by_target_name(node.name):
                    if e.source_qualified not in seen_sources:
                        seen_sources.add(e.source_qualified)
                        caller = store.get_node(e.source_qualified)
                        if caller:
                            results.append(node_to_dict(caller))
                        edges_out.append(edge_to_dict(e))

        elif pattern == "callees_of":
            seen_targets: set[str] = set()
            for e in store.get_edges_by_source(qn):
                if e.kind == "CALLS":
                    if e.target_qualified not in seen_targets:
                        seen_targets.add(e.target_qualified)
                        callee = store.get_node(e.target_qualified)
                        if callee:
                            results.append(node_to_dict(callee))
                        elif "::" not in e.target_qualified:
                            results.append({
                                "kind": "Function",
                                "name": e.target_qualified,
                                "qualified_name": e.target_qualified,
                            })
                        edges_out.append(edge_to_dict(e))

        elif pattern == "imports_of":
            for e in store.get_edges_by_source(qn):
                if e.kind == "IMPORTS_FROM":
                    results.append({"import_target": e.target_qualified})
                    edges_out.append(edge_to_dict(e))

        elif pattern == "importers_of":
            # Find edges where target matches this file.
            # Use resolve() to canonicalize the path, matching how
            # _resolve_module_to_file stores edge targets.
            abs_target = (
                str((root / target).resolve()) if node is None
                else node.file_path
            )
            for e in store.get_edges_by_target(abs_target):
                if e.kind == "IMPORTS_FROM":
                    results.append({
                        "importer": e.source_qualified,
                        "file": e.file_path,
                    })
                    edges_out.append(edge_to_dict(e))

        elif pattern == "children_of":
            for e in store.get_edges_by_source(qn):
                if e.kind == "CONTAINS":
                    child = store.get_node(e.target_qualified)
                    if child:
                        results.append(node_to_dict(child))

        elif pattern == "tests_for":
            for e in store.get_edges_by_target(qn):
                if e.kind == "TESTED_BY":
                    test = store.get_node(e.source_qualified)
                    if test:
                        results.append(node_to_dict(test))
            # Also search by naming convention
            name = node.name if node else target
            test_nodes = store.search_nodes(f"test_{name}", limit=10)
            test_nodes += store.search_nodes(f"Test{name}", limit=10)
            seen = {r.get("qualified_name") for r in results}
            for t in test_nodes:
                if t.qualified_name not in seen and t.is_test:
                    results.append(node_to_dict(t))

        elif pattern == "inheritors_of":
            for e in store.get_edges_by_target(qn):
                if e.kind in ("INHERITS", "IMPLEMENTS"):
                    child = store.get_node(e.source_qualified)
                    if child:
                        results.append(node_to_dict(child))
                    edges_out.append(edge_to_dict(e))
            # Fallback: INHERITS/IMPLEMENTS edges store unqualified base names
            # (e.g. "Animal") while qn is fully qualified
            # (e.g. "sample.dart::Animal"). Search by plain name too. See: #87
            if not results and node:
                for kind in ("INHERITS", "IMPLEMENTS"):
                    for e in store.search_edges_by_target_name(node.name, kind=kind):
                        child = store.get_node(e.source_qualified)
                        if child:
                            results.append(node_to_dict(child))
                        edges_out.append(edge_to_dict(e))

        elif pattern == "file_summary":
            graph_paths = resolve_graph_file_paths(store, root, [target])
            for graph_path in graph_paths:
                for n in store.get_nodes_by_file(graph_path):
                    results.append(node_to_dict(n))

        summary = (
            f"Found {len(results)} result(s) "
            f"for {pattern}('{target}')"
        )

        total_results = len(results)

        if detail_level == "minimal":
            row_cap = min(limit, 5)
            minimal_results = [
                {
                    k: r[k]
                    for k in ("name", "kind", "file_path")
                    if k in r
                }
                for r in results[:row_cap]
            ]
            return {
                "status": "ok",
                "pattern": pattern,
                "target": target,
                "description": _QUERY_PATTERNS[pattern],
                "summary": summary,
                "result_count": total_results,
                "returned": len(minimal_results),
                "truncated": total_results > len(minimal_results),
                "results": minimal_results,
            }

        kept_results = results[:limit]
        truncated = total_results > len(kept_results)
        kept_edges = edges_out
        if truncated:
            # Drop edges that point at rows the caller will not receive, so the
            # payload never references a result that is not in ``results``.
            kept_names = {
                r["qualified_name"]
                for r in kept_results
                if "qualified_name" in r
            }
            kept_edges = [
                e
                for e in edges_out
                if e.get("source") in kept_names or e.get("target") in kept_names
            ]

        return {
            "status": "ok",
            "pattern": pattern,
            "target": target,
            "description": _QUERY_PATTERNS[pattern],
            "summary": summary,
            "result_count": total_results,
            "returned": len(kept_results),
            "truncated": truncated,
            "results": kept_results,
            "edges": kept_edges,
        }
    finally:
        store.close()


def list_graph_stats(repo_root: str | None = None) -> dict[str, Any]:
    """Get aggregate statistics about the knowledge graph.

    Args:
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        Total nodes, edges, breakdown by kind, languages, and last update time.
    """
    store, root = open_store(repo_root)
    try:
        stats = store.get_stats()

        summary_parts = [
            f"Graph statistics for {root.name}:",
            f"  Files: {stats.files_count}",
            f"  Total nodes: {stats.total_nodes}",
            f"  Total edges: {stats.total_edges}",
            f"  Languages: {', '.join(stats.languages) if stats.languages else 'none'}",
            f"  Last updated: {stats.last_updated or 'never'}",
            "",
            "Nodes by kind:",
        ]
        for kind, count in sorted(stats.nodes_by_kind.items()):
            summary_parts.append(f"  {kind}: {count}")
        summary_parts.append("")
        summary_parts.append("Edges by kind:")
        for kind, count in sorted(stats.edges_by_kind.items()):
            summary_parts.append(f"  {kind}: {count}")

        summary_parts.append("")
        summary_parts.append("Search: FTS5 (BM25)")

        return {
            "status": "ok",
            "summary": "\n".join(summary_parts),
            "total_nodes": stats.total_nodes,
            "total_edges": stats.total_edges,
            "nodes_by_kind": stats.nodes_by_kind,
            "edges_by_kind": stats.edges_by_kind,
            "languages": stats.languages,
            "files_count": stats.files_count,
            "last_updated": stats.last_updated,
        }
    finally:
        store.close()
