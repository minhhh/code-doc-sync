"""Shared post-build processing pipeline.

After the core Tree-sitter parse (full_build or incremental_update), four
post-processing steps must run to populate derived tables:

1. Compute node signatures
2. Rebuild FTS5 search index
3. Trace execution flows
4. Detect code communities

This module extracts that pipeline so every entry point — build and update —
produces identical results.
"""

from __future__ import annotations

import json as _json
import logging
import sqlite3
import time
from typing import Any

from ..graph import GraphStore

logger = logging.getLogger(__name__)


def run_post_processing(
    store: GraphStore,
    *,
    level: str = "full",
    changed_files: list[str] | None = None,
) -> dict[str, Any]:
    """Run the post-build steps on a populated graph.

    Each step is non-fatal: failures are logged and collected as warnings
    so the primary build result is never lost.

    Args:
        store: An open GraphStore with nodes and edges already populated.
        level: ``"full"`` runs every step; ``"minimal"`` stops after
            signatures and FTS (fast, keeps search working); ``"none"``
            skips the pipeline entirely.
        changed_files: When given, flows and communities are recomputed
            incrementally for the affected files instead of from scratch.

    Returns:
        Dict with keys for each step's result count, the ``postprocess_level``
        applied, and a ``warnings`` list (only present when a step failed).
    """
    result: dict[str, Any] = {"postprocess_level": level}
    if level == "none":
        return result

    warnings: list[str] = []

    _compute_signatures(store, result, warnings)
    _rebuild_fts_index(store, result, warnings)

    if level == "full":
        _trace_flows(store, result, warnings, changed_files)
        _detect_communities(store, result, warnings, changed_files)
        _compute_summaries(store, result, warnings)
        store.set_metadata(
            "last_postprocessed_at", time.strftime("%Y-%m-%dT%H:%M:%S")
        )
        store.set_metadata("postprocess_level", level)

    if warnings:
        result["warnings"] = warnings
    return result


# -- Individual steps (private) ------------------------------------------


def _compute_signatures(
    store: GraphStore,
    result: dict[str, Any],
    warnings: list[str],
) -> None:
    """Compute human-readable signatures for nodes that lack one."""
    try:
        rows = store.get_nodes_without_signature()
        for row in rows:
            node_id, name, kind, params, ret = (
                row[0],
                row[1],
                row[2],
                row[3],
                row[4],
            )
            if kind in ("Function", "Test"):
                sig = f"def {name}({params or ''})"
                if ret:
                    sig += f" -> {ret}"
            elif kind == "Class":
                sig = f"class {name}"
            else:
                sig = name
            store.update_node_signature(node_id, sig[:512])
        store.commit()
        result["signatures_computed"] = len(rows)
    except (sqlite3.OperationalError, TypeError, KeyError) as e:
        logger.warning("Signature computation failed: %s", e)
        warnings.append(f"Signature computation failed: {type(e).__name__}: {e}")


def _rebuild_fts_index(
    store: GraphStore,
    result: dict[str, Any],
    warnings: list[str],
) -> None:
    """Rebuild the FTS5 full-text search index."""
    try:
        from ..query.search import rebuild_fts_index

        fts_count = rebuild_fts_index(store)
        result["fts_indexed"] = fts_count
    except (sqlite3.OperationalError, ImportError) as e:
        logger.warning("FTS index rebuild failed: %s", e)
        warnings.append(f"FTS index rebuild failed: {type(e).__name__}: {e}")


def _trace_flows(
    store: GraphStore,
    result: dict[str, Any],
    warnings: list[str],
    changed_files: list[str] | None = None,
) -> None:
    """Trace execution flows, incrementally when changed files are known."""
    try:
        if changed_files:
            from ..query.flows import incremental_trace_flows

            count = incremental_trace_flows(store, changed_files)
        else:
            from ..query.flows import store_flows, trace_flows

            count = store_flows(store, trace_flows(store))
        result["flows_detected"] = count
    except (sqlite3.OperationalError, ImportError) as e:
        logger.warning("Flow detection failed: %s", e)
        warnings.append(f"Flow detection failed: {type(e).__name__}: {e}")


def _detect_communities(
    store: GraphStore,
    result: dict[str, Any],
    warnings: list[str],
    changed_files: list[str] | None = None,
) -> None:
    """Detect code communities, incrementally when changed files are known."""
    try:
        if changed_files:
            from ..query.communities import incremental_detect_communities

            count = incremental_detect_communities(store, changed_files)
        else:
            from ..query.communities import detect_communities, store_communities

            count = store_communities(store, detect_communities(store))
        result["communities_detected"] = count
    except (sqlite3.OperationalError, ImportError) as e:
        logger.warning("Community detection failed: %s", e)
        warnings.append(f"Community detection failed: {type(e).__name__}: {e}")


def _compute_summaries(
    store: GraphStore,
    result: dict[str, Any],
    warnings: list[str],
) -> None:
    """Populate community_summaries, flow_snapshots, and risk_index tables.

    Uses batched aggregate queries and in-memory grouping instead of
    per-community/per-node loops. On graphs with ~100k edges this
    reduces the work from ``O(nodes + communities)`` SQLite round trips
    each doing their own B-tree scan to a handful of ``GROUP BY``
    queries, turning what used to be an effective hang into a few
    seconds.

    Each summary block (community_summaries, flow_snapshots, risk_index)
    is wrapped in an explicit transaction so the DELETE + INSERT sequence
    is atomic.  If a table doesn't exist yet the block is silently skipped.
    """
    from collections import defaultdict
    from os.path import commonprefix

    conn = store._conn

    # -- community_summaries --
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM community_summaries")

        # Pre-compute per-qualified_name edge counts once. Previously
        # this section ran a per-community triple-JOIN aggregate query
        # (nodes LEFT JOIN edges LEFT JOIN edges), which on graphs with
        # thousands of communities was the second-biggest hang.
        edge_counts: dict[str, int] = defaultdict(int)
        for row in conn.execute(
            "SELECT source_qualified, COUNT(*) FROM edges GROUP BY source_qualified"
        ):
            edge_counts[row[0]] += row[1]
        for row in conn.execute(
            "SELECT target_qualified, COUNT(*) FROM edges GROUP BY target_qualified"
        ):
            edge_counts[row[0]] += row[1]

        # Group non-File nodes per community for top-symbol selection.
        nodes_by_comm: dict[int, list[tuple[str, int]]] = defaultdict(list)
        for row in conn.execute(
            "SELECT community_id, name, qualified_name FROM nodes "
            "WHERE community_id IS NOT NULL AND kind != 'File'"
        ):
            cid, name, qn = row[0], row[1], row[2]
            nodes_by_comm[cid].append((name, edge_counts.get(qn, 0)))

        # Group distinct file paths per community (preserving first-seen
        # order for stable output, same as DISTINCT in the old query).
        files_by_comm: dict[int, list[str]] = defaultdict(list)
        seen_files: dict[int, set[str]] = defaultdict(set)
        for row in conn.execute(
            "SELECT community_id, file_path FROM nodes WHERE community_id IS NOT NULL"
        ):
            cid, fp = row[0], row[1]
            if fp not in seen_files[cid]:
                seen_files[cid].add(fp)
                files_by_comm[cid].append(fp)

        community_rows = conn.execute(
            "SELECT id, name, size, dominant_language FROM communities"
        ).fetchall()
        for r in community_rows:
            cid, cname, csize, clang = r[0], r[1], r[2], r[3]

            # Top 5 symbols by total edge count (in + out). Python's
            # sorted() is stable so ties break by original row order.
            members = sorted(
                nodes_by_comm.get(cid, []),
                key=lambda nc: nc[1],
                reverse=True,
            )
            key_syms = _json.dumps([m[0] for m in members[:5]])

            # Auto-generate purpose from common file path prefix.
            paths = files_by_comm.get(cid, [])[:20]
            purpose = ""
            if paths:
                prefix = commonprefix(paths)
                if "/" in prefix:
                    purpose = prefix.rsplit("/", 1)[0].split("/")[-1] if "/" in prefix else ""

            conn.execute(
                "INSERT OR REPLACE INTO community_summaries "
                "(community_id, name, purpose, key_symbols, size, dominant_language) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (cid, cname, purpose, key_syms, csize, clang or ""),
            )
        conn.commit()
    except sqlite3.OperationalError:
        conn.rollback()  # Table may not exist yet

    # -- flow_snapshots --
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM flow_snapshots")
        flow_rows = conn.execute(
            "SELECT id, name, entry_point_id, criticality, node_count, "
            "file_count, path_json FROM flows"
        ).fetchall()

        # Collect every node id referenced by any flow, then fetch
        # their qualified_names in one batched query instead of per-flow
        # per-node lookups.
        needed_ids: set[int] = set()
        parsed_paths: list[list[int]] = []
        for r in flow_rows:
            needed_ids.add(r[2])  # entry_point_id
            path_ids = _json.loads(r[6]) if r[6] else []
            parsed_paths.append(path_ids)
            # Match the old semantics: entry + up to 3 intermediates + last
            for nid in path_ids[1:4]:
                needed_ids.add(nid)
            if path_ids:
                needed_ids.add(path_ids[-1])

        id_to_name: dict[int, str] = {}
        if needed_ids:
            # Batch the IN clause in chunks of 450 to stay under SQLite's
            # default SQLITE_MAX_VARIABLE_NUMBER (999), same strategy as
            # GraphStore.get_edges_among.
            id_list = list(needed_ids)
            for i in range(0, len(id_list), 450):
                batch = id_list[i : i + 450]
                placeholders = ",".join("?" for _ in batch)
                node_rows = conn.execute(
                    f"SELECT id, qualified_name FROM nodes WHERE id IN ({placeholders})",  # nosec B608
                    batch,
                ).fetchall()
                for nr in node_rows:
                    id_to_name[nr[0]] = nr[1]

        for r, path_ids in zip(flow_rows, parsed_paths):
            fid, fname, ep_id = r[0], r[1], r[2]
            crit, ncount, fcount = r[3], r[4], r[5]
            ep_name = id_to_name.get(ep_id, str(ep_id))
            critical_path: list[str] = []
            if path_ids:
                critical_path.append(ep_name)
                if len(path_ids) > 2:
                    for nid in path_ids[1:4]:
                        nm = id_to_name.get(nid)
                        if nm:
                            critical_path.append(nm)
                if len(path_ids) > 1:
                    last = id_to_name.get(path_ids[-1])
                    if last and last not in critical_path:
                        critical_path.append(last)
            conn.execute(
                "INSERT OR REPLACE INTO flow_snapshots "
                "(flow_id, name, entry_point, critical_path, criticality, "
                "node_count, file_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (fid, fname, ep_name, _json.dumps(critical_path), crit, ncount, fcount),
            )
        conn.commit()
        result["summaries_computed"] = True
    except sqlite3.OperationalError as exc:
        conn.rollback()
        warnings.append(f"Summary computation failed: {type(exc).__name__}: {exc}")

    # -- risk_index --
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM risk_index")

        # Pre-compute caller and test-coverage counts in two aggregate
        # queries. Previously this section ran two COUNT(*) queries per
        # candidate node; on a ~100k-edge graph with tens of thousands
        # of Function/Class/Test nodes that was the primary hang
        # observed during Godot builds.
        caller_counts: dict[str, int] = {}
        for row in conn.execute(
            "SELECT target_qualified, COUNT(*) FROM edges "
            "WHERE kind = 'CALLS' GROUP BY target_qualified"
        ):
            caller_counts[row[0]] = row[1]

        tested_counts: dict[str, int] = {}
        for row in conn.execute(
            "SELECT source_qualified, COUNT(*) FROM edges "
            "WHERE kind = 'TESTED_BY' GROUP BY source_qualified"
        ):
            tested_counts[row[0]] = row[1]

        risk_nodes = conn.execute(
            "SELECT id, qualified_name, name FROM nodes WHERE kind IN ('Function', 'Class', 'Test')"
        ).fetchall()
        security_kw = {
            "auth",
            "login",
            "password",
            "token",
            "session",
            "crypt",
            "secret",
            "credential",
            "permission",
            "sql",
            "execute",
        }
        for n in risk_nodes:
            nid, qn, name = n[0], n[1], n[2]
            caller_count = caller_counts.get(qn, 0)
            tested = tested_counts.get(qn, 0)
            coverage = "tested" if tested > 0 else "untested"
            name_lower = name.lower()
            sec_relevant = 1 if any(kw in name_lower for kw in security_kw) else 0
            risk = 0.0
            if caller_count > 10:
                risk += 0.3
            elif caller_count > 3:
                risk += 0.15
            if coverage == "untested":
                risk += 0.3
            if sec_relevant:
                risk += 0.4
            risk = min(risk, 1.0)
            conn.execute(
                "INSERT OR REPLACE INTO risk_index "
                "(node_id, qualified_name, risk_score, caller_count, "
                "test_coverage, security_relevant, last_computed) "
                "VALUES (?, ?, ?, ?, ?, ?, datetime('now'))",
                (nid, qn, risk, caller_count, coverage, sec_relevant),
            )
        conn.commit()
        result["summaries_computed"] = True
    except sqlite3.OperationalError as exc:
        conn.rollback()
        warnings.append(f"Summary computation failed: {type(exc).__name__}: {exc}")
