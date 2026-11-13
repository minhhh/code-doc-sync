"""Merge a graph export into a document by updating its marked blocks."""

from __future__ import annotations

from .markdown import (
    append_blocks,
    parse_document,
    render_block,
    render_new_document,
)
from .model import ContextDocument, Entity, SyncResult


def sync_document(
    document: ContextDocument,
    entities: list[Entity],
    *,
    context_type: str = "call-graph",
) -> SyncResult:
    """Refresh matching blocks, ignore unknown ones, append the new entities.

    The document keeps its own structure: headings, prose, order, and every byte
    outside a ``CTX-SYNC`` marker pair stay as they are. Only the bytes between
    the markers of a block whose entity is in the export get replaced.
    """
    if document.empty:
        ordered = sorted(entities, key=lambda item: item.entity_id)
        return SyncResult(text=render_new_document(context_type, ordered), added=len(ordered))

    incoming = {entity.entity_id: entity for entity in entities}
    matched: set[str] = set()
    result = SyncResult(text="")
    parts: list[str] = []
    cursor = 0

    for region in sorted(document.entities, key=lambda item: item.start):
        parts.append(document.text[cursor : region.start])
        cursor = region.end
        entity = incoming.get(region.entity_id)
        original = document.text[region.start : region.end]
        if entity is None:
            parts.append(original)
            result.ignored += 1
            continue
        rendered = render_block(entity, context_type).rstrip("\n")
        parts.append(rendered)
        matched.add(region.entity_id)
        if rendered == original:
            result.unchanged += 1
        else:
            result.updated += 1

    parts.append(document.text[cursor:])
    missing = [incoming[key] for key in sorted(incoming) if key not in matched]
    result.added = len(missing)
    result.text = append_blocks(
        "".join(parts),
        [render_block(entity, context_type) for entity in missing],
    )
    return result


def render_empty(entities: list[Entity], *, context_type: str = "call-graph") -> str:
    """Build a fresh document from a graph export."""
    return sync_document(parse_document(""), entities, context_type=context_type).text
