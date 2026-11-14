"""Helpers shared by the test suite."""

from __future__ import annotations

from pathlib import Path

from codegraph.parsing.parser import CodeParser, EdgeInfo, NodeInfo


def parse_path(
    parser: CodeParser, path: Path | str
) -> tuple[list[NodeInfo], list[EdgeInfo]]:
    """Read *path* and parse its bytes, the path the build itself uses.

    ``CodeParser.parse_file`` was removed so tests exercise ``parse_bytes``
    like ``full_build`` and ``incremental_update`` do: they read the bytes
    once for hashing and hand the same bytes to the parser.
    """
    resolved = Path(path)
    return parser.parse_bytes(resolved, resolved.read_bytes())
