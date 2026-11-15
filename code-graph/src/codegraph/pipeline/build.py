"""Building and updating the graph, then post-processing it.

Parsing lives in ``incremental`` (full and incremental paths); everything
after the parse lives in ``postprocessing``. This module only sequences them
and shapes the summary the CLI prints.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..graph import GraphStore
from .incremental import full_build, incremental_update
from .postprocessing import run_post_processing

logger = logging.getLogger(__name__)


def build_or_update(
    store: GraphStore,
    root: Path,
    *,
    full_rebuild: bool = False,
    base: str = "HEAD~1",
    postprocess: str = "full",
    recurse_submodules: bool | None = None,
) -> dict[str, Any]:
    """Build or incrementally update the graph.

    Args:
        store: Open graph store.
        root: Repository root.
        full_rebuild: Re-parse every file instead of only changed ones.
        base: Git ref for the incremental diff (default ``HEAD~1``).
        postprocess: ``"full"``, ``"minimal"``, or ``"none"`` — see
            :func:`postprocessing.run_post_processing`.
        recurse_submodules: Include files from git submodules. Falls back to
            the ``CODE_GRAPH_RECURSE_SUBMODULES`` environment variable.

    Returns:
        Summary with file/node/edge counts, post-process counts, and errors.
    """
    if full_rebuild:
        result = full_build(root, store, recurse_submodules)
        payload: dict[str, Any] = {
            "status": "ok",
            "build_type": "full",
            "summary": (
                f"Full build complete: parsed {result['files_parsed']} files, "
                f"created {result['total_nodes']} nodes and "
                f"{result['total_edges']} edges."
            ),
            **result,
        }
        changed_files = None
    else:
        result = incremental_update(root, store, base=base)
        if result["files_updated"] == 0:
            return {
                "status": "ok",
                "build_type": "incremental",
                "summary": "No changes detected. Graph is up to date.",
                "postprocess_level": postprocess,
                **result,
            }
        payload = {
            "status": "ok",
            "build_type": "incremental",
            "summary": (
                f"Incremental update: {result['files_updated']} files re-parsed, "
                f"{result['total_nodes']} nodes and "
                f"{result['total_edges']} edges updated. "
                f"Changed: {result['changed_files']}. "
                f"Dependents also updated: {result['dependent_files']}."
            ),
            **result,
        }
        changed_files = result.get("changed_files")

    payload.update(
        run_post_processing(store, level=postprocess, changed_files=changed_files)
    )
    return payload
