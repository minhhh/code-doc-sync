"""Flow documents: one readable call tree per flow, nothing else generated.

A flow document is ordinary Markdown holding one marked block per flow, using
the same marker pair as the entity context types:

    <!-- CTX-SYNC:FLOW-START -->
    * flow=src/hints.py::generate_hints#4d562c3a071b
        * entry_point=src/hints.py::generate_hints
        * criticality=0.4533
        * nodes=9/9
    * src/hints.py::generate_hints
        * → src/hints.py::_track_result
            * → src/hints.py::SessionState.record_files
    <!-- CTX-SYNC:FLOW-END -->

Two root bullets: the first holds the flow details (identity plus one field per
child bullet), the second starts the traced call tree. Four spaces per level of
depth and one `→` per call.
A target that was already expanded is listed once more without children, which
records the call without walking the subtree a second time. Sync replaces the
block body and nothing else: no headings, no generated prose. Text outside a
block is opaque and preserved byte-for-byte, including blocks whose flow is no
longer in the export. New flows are appended at the end of the file, and blocks
are matched by the `flow=` field of their first bullet.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .markdown import append_blocks, block_markers

START_MARKER, END_MARKER = block_markers("flows")
CALL_ARROW = "→"

_BLOCK_RE = re.compile(
    re.escape(START_MARKER) + r"(.*?)" + re.escape(END_MARKER),
    re.DOTALL,
)
_FIELD_RE = re.compile(r"^\s*\*\s+([a-z_]+)=(.*)$", re.MULTILINE)


@dataclass
class FlowSyncResult:
    """What changed while splicing flow blocks."""

    text: str
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    ignored: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated)


def render_block(flow: dict[str, Any]) -> str:
    """Render one flow as a root bullet plus the call tree root bullet."""
    fields: list[str] = []
    if flow.get("entry_point"):
        fields.append(f"entry_point={flow['entry_point']}")
    if flow.get("criticality") is not None:
        fields.append(f"criticality={flow['criticality']}")
    unique = flow.get("unique_nodes")
    if unique is not None:
        fields.append(f"nodes={unique}/{flow.get('tree_nodes', unique)}")
    if flow.get("status"):
        fields.append(f"status={flow['status']}")

    root = flow.get("root")
    root_qn = str(root.get("qn") or "") if isinstance(root, dict) else ""
    branches = root.get("calls") if isinstance(root, dict) else None
    lines = [f"* flow={flow['key']}"]
    lines.extend(f"    * {field}" for field in fields)
    lines.append(f"* {root_qn}")
    lines.extend(_tree_lines(branches if isinstance(branches, list) else [], depth=1))
    body = "\n".join(lines)
    return f"{START_MARKER}\n{body}\n{END_MARKER}"


def parse_blocks(text: str) -> list[dict[str, str]]:
    """Return the metadata line of every block in *text*, skipping unkeyed ones."""
    blocks: list[dict[str, str]] = []
    for match in _BLOCK_RE.finditer(text):
        meta = _parse_meta(match.group(1))
        if meta.get("key"):
            blocks.append(meta)
    return blocks


def render_document(text: str, flows: list[dict[str, Any]]) -> FlowSyncResult:
    """Replace each block matched by ``key``, then append new flows at the end."""
    incoming: dict[str, dict[str, Any]] = {
        str(flow["key"]): flow for flow in flows if flow.get("key")
    }
    result = FlowSyncResult(text="")
    parts: list[str] = []
    cursor = 0

    for match in _BLOCK_RE.finditer(text):
        parts.append(text[cursor : match.start()])
        cursor = match.end()

        key = _parse_meta(match.group(1)).get("key", "")
        flow = incoming.pop(key, None) if key else None
        if flow is None:
            # No readable key, or the flow left the export: keep the bytes.
            parts.append(match.group(0))
            result.ignored += 1
            continue
        rendered = render_block(flow)
        parts.append(rendered)
        if rendered == match.group(0):
            result.unchanged += 1
        else:
            result.updated += 1

    parts.append(text[cursor:])
    new_blocks = [render_block(incoming[key]) for key in sorted(incoming)]
    result.added = len(new_blocks)
    result.text = append_blocks("".join(parts), new_blocks)
    return result


def _tree_lines(calls: list[Any], *, depth: int) -> list[str]:
    pad = "    " * depth
    lines: list[str] = []
    for child in calls:
        if not isinstance(child, dict):
            continue
        if "ref" in child:
            lines.append(f"{pad}* {CALL_ARROW} {child['ref']}")
            continue
        lines.append(f"{pad}* {CALL_ARROW} {child.get('qn', '')}")
        nested = child.get("calls")
        if isinstance(nested, list):
            lines.extend(_tree_lines(nested, depth=depth + 1))
    return lines


def _parse_meta(body: str) -> dict[str, str]:
    """Read the ``name=value`` bullets of the details root bullet."""
    meta: dict[str, str] = {}
    for name, value in _FIELD_RE.findall(body):
        meta[name.strip()] = value.strip()
    key = meta.pop("flow", "")
    if not key:
        return {}
    meta["key"] = key
    return meta
