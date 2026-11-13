"""Find and refresh the marked blocks of a document, leaving the rest alone.

Sync never rewrites a document it does not understand. It locates the start and
end markers of each block, replaces the bytes between them for every block whose
entity appears in the export, and appends a block for every entity the document
does not have yet. Everything outside a marker pair is copied through unchanged:
headings, prose, notes, and blocks whose entity left the export.

All three context types use the same marker pair and differ only in their tag::

    <!-- CTX-SYNC:CALL-GRAPH-START -->
    ...
    <!-- CTX-SYNC:CALL-GRAPH-END -->

    <!-- CTX-SYNC:COMMUNITY-START -->  ...  <!-- CTX-SYNC:COMMUNITY-END -->
    <!-- CTX-SYNC:FLOW-START -->       ...  <!-- CTX-SYNC:FLOW-END -->
"""

from __future__ import annotations

import re

from .model import ContextDocument, ContextEntity, Entity

# The block tags. A flow document is written by ``flows_doc``, which uses the
# same marker pair through :func:`block_markers`.
BLOCK_TAGS = {
    "call-graph": "CALL-GRAPH",
    "community": "COMMUNITY",
    "flows": "FLOW",
}

_ID_RE = re.compile(r"^\*\*ID:\*\*[ \t]*`?([^`\n]+)`?[ \t]*$", re.MULTILINE)


def block_markers(context_type: str) -> tuple[str, str]:
    """Return the start and end marker comments for a context type."""
    tag = BLOCK_TAGS.get(context_type, context_type.upper())
    return f"<!-- CTX-SYNC:{tag}-START -->", f"<!-- CTX-SYNC:{tag}-END -->"


def block_pattern(context_type: str) -> re.Pattern[str]:
    """Match whole blocks of one context type, markers included."""
    start, end = block_markers(context_type)
    return re.compile(re.escape(start) + r"(.*?)" + re.escape(end), re.DOTALL)


def parse_document(text: str) -> ContextDocument:
    """Locate the managed blocks in an existing document.

    Only marker pairs delimit a region. Anything else in the file is opaque and
    stays untouched, including text that looks like generated content.
    """
    return ContextDocument(text=text, entities=_marker_regions(text))


def render_block(entity: Entity, context_type: str = "call-graph") -> str:
    """Render one complete block: a marker pair around the graph facts.

    Nothing else is written. The block holds only fields taken from the export:
    no heading, no caption, no generated prose.
    """
    start, end = block_markers(context_type)
    return "\n".join([start, *_fact_lines(entity), end]) + "\n"


def render_new_document(context_type: str, entities: list[Entity]) -> str:
    """Build a document from scratch, for a target that does not exist yet.

    Only the blocks are written: no title, no section heading, no preamble. The
    file is a list of marker pairs holding graph facts, exactly like a flow
    document, so the author decides what surrounds them.
    """
    blocks = [
        render_block(entity, context_type)
        for entity in sorted(entities, key=lambda item: item.entity_id)
    ]
    return "\n".join(blocks)


def append_blocks(text: str, blocks: list[str]) -> str:
    """Append rendered blocks at the end of *text*, keeping one blank line."""
    if not blocks:
        return text
    body = "\n\n".join(block.strip("\n") for block in blocks)
    if not text.strip():
        return body + "\n"
    if text.endswith("\n\n"):
        return f"{text}{body}\n"
    if text.endswith("\n"):
        return f"{text}\n{body}\n"
    return f"{text}\n\n{body}\n"


def _marker_regions(text: str) -> list[ContextEntity]:
    """Collect the blocks of every entity context type, in document order."""
    regions: list[ContextEntity] = []
    for context_type in ("call-graph", "community"):
        for match in block_pattern(context_type).finditer(text):
            entity_id = _ID_RE.search(text, match.start(), match.end())
            if entity_id is None:
                continue
            regions.append(
                ContextEntity(
                    entity_id=entity_id.group(1).strip(),
                    start=match.start(),
                    end=match.end(),
                )
            )
    return sorted(regions, key=lambda item: item.start)


def _fact_lines(entity: Entity) -> list[str]:
    """Render the machine-managed facts that sync replaces in place."""
    lines = [
        "**Source Status:** active",
        f"**ID:** `{entity.entity_id}`",
    ]
    # A single-kind projection (flows, communities) leaves kind empty.
    if entity.kind:
        lines.append(f"**Kind:** {entity.kind}")
    # Communities and flows span many files, so location lines would lie.
    if entity.file:
        lines.extend(
            [
                f"**File:** `{entity.file}`",
                f"**Lines:** {_line_range(entity)}",
                f"**Language:** {entity.language or 'unknown'}",
            ]
        )
    elif entity.language:
        lines.append(f"**Language:** {entity.language}")
    if entity.signature:
        lines.append(f"**Signature:** {_one_line(entity.signature)}")
    if entity.docstring:
        lines.append(f"**Docstring:** {_one_line(entity.docstring)}")
    for label, value in entity.metadata:
        lines.append(f"**{label}:** {value}")
    if entity.show_call_edges:
        calls = ", ".join(f"`{value}`" for value in entity.calls) or "_(none)_"
        called_by = ", ".join(f"`{value}`" for value in entity.called_by) or "_(none)_"
        lines.extend([f"**Calls:** {calls}", f"**Called by:** {called_by}"])
    return lines


def _one_line(value: str) -> str:
    return " ".join(value.split())


def _line_range(entity: Entity) -> str:
    if entity.line_start is None:
        return "unknown"
    if entity.line_end is None or entity.line_end == entity.line_start:
        return str(entity.line_start)
    return f"{entity.line_start}-{entity.line_end}"
