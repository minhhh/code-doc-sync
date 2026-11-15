"""Resolve ``obj.method()`` calls whose receiver has a declared class.

The parser records ``extra.receiver`` on every member call and
``extra.receiver_type`` when the file declares what the receiver is
(``store: GraphStore``, ``s = Store()``). This pass turns such a call into a
qualified target, but only when exactly one class in the graph declares that
method — a guess is worse than a bare name here, because a wrong ``CALLS``
edge hides an entry point and marks dead code as live.

Edges whose receiver type is unknown are left alone: that covers external
code (``conn.commit()`` on a sqlite connection), container methods
(``seen.add()``) and anything reached through an attribute or a call result.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..graph import GraphStore

logger = logging.getLogger(__name__)


def resolve_typed_receivers(store: GraphStore) -> dict[str, int]:
    """Rewrite bare CALLS targets that carry a declared receiver type.

    Safe to call repeatedly: resolved targets contain ``::`` and are skipped
    on later runs.

    Returns counters for telemetry.
    """
    conn = store._conn

    file_language: dict[str, str] = {}
    for row in conn.execute(
        "SELECT file_path, language FROM nodes WHERE kind = 'File'"
    ).fetchall():
        file_language[row["file_path"]] = row["language"]

    # (class name, method name) -> qualified method names
    methods: dict[tuple[str, str], list[str]] = {}
    for row in conn.execute(
        "SELECT name, qualified_name, parent_name FROM nodes "
        "WHERE kind IN ('Function', 'Test') AND parent_name IS NOT NULL"
    ).fetchall():
        methods.setdefault((row["parent_name"], row["name"]), []).append(
            row["qualified_name"]
        )

    stats = {"candidates": 0, "resolved": 0, "ambiguous": 0, "no_method": 0}
    if not methods:
        return stats

    rows = conn.execute(
        "SELECT id, source_qualified, target_qualified, file_path, extra "
        "FROM edges WHERE kind = 'CALLS' "
        "AND target_qualified NOT LIKE '%::%' "
        "AND extra LIKE '%receiver_type%'"
    ).fetchall()
    stats["candidates"] = len(rows)

    for row in rows:
        try:
            extra: dict[str, Any] = json.loads(row["extra"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        declared = extra.get("receiver_type")
        if not declared:
            continue

        class_name = str(declared).rsplit("::", 1)[-1]
        candidates = methods.get((class_name, row["target_qualified"]), [])
        if not candidates:
            stats["no_method"] += 1
            continue

        if len(candidates) > 1:
            # Prefer a method defined in the caller's own file, then one in a
            # file of the same language. Anything else stays unresolved.
            source = row["source_qualified"]
            caller_file = source.split("::", 1)[0] if "::" in source else row["file_path"]
            language = file_language.get(caller_file)
            local = [c for c in candidates if c.startswith(f"{caller_file}::")]
            same_lang = [
                c for c in candidates
                if file_language.get(c.split("::", 1)[0]) == language
            ]
            candidates = local if len(local) == 1 else same_lang
            if len(candidates) != 1:
                stats["ambiguous"] += 1
                continue

        conn.execute(
            "UPDATE edges SET target_qualified = ? WHERE id = ?",
            (candidates[0], row["id"]),
        )
        stats["resolved"] += 1

    if stats["resolved"]:
        conn.commit()
        logger.info(
            "Resolved %d typed-receiver CALLS edges (%d ambiguous, %d no method)",
            stats["resolved"], stats["ambiguous"], stats["no_method"],
        )
    return stats
