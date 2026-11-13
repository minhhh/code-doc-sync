from __future__ import annotations

from pathlib import Path

from ctxsync.graph import entities_from_graph
from ctxsync.markdown import block_markers, parse_document
from ctxsync.sync import render_empty, sync_document

START, END = block_markers("call-graph")


def test_normalizes_nodes_to_relative_entities(graph_payload: dict) -> None:
    entities = entities_from_graph(graph_payload, root=Path("/repo"))
    assert [e.entity_id for e in entities] == [
        "src/app.py::main",
        "src/util.py::helper",
    ]
    main = entities[0]
    assert main.name == "main"
    assert main.kind == "Function"
    assert main.file == "src/app.py"
    assert main.line_start == 3
    assert main.calls == ("src/util.py::helper", "print")


def test_accepts_prebuilt_entities() -> None:
    payload = {
        "entities": [
            {
                "id": "src/a.py::f",
                "file": "src/a.py",
                "symbol": "f",
                "kind": "function",
                "signature": "f()",
                "loc": {"start_line": 1, "end_line": 2},
                "calls": ["g"],
                "called_by": ["main"],
                "docstring": "Does f.",
            }
        ]
    }
    entities = entities_from_graph(payload)
    assert entities[0].entity_id == "src/a.py::f"
    assert entities[0].line_end == 2
    assert entities[0].docstring == "Does f."


def test_new_document_is_marker_delimited(graph_payload: dict) -> None:
    text = render_empty(entities_from_graph(graph_payload, root=Path("/repo")))
    # Blocks only: no title, no section heading, no preamble.
    assert text.startswith(START)
    assert "#" not in text.replace("### ", "")
    assert text.count(START) == 2
    assert text.count(END) == 2
    assert "**Source Status:** active" in text
    assert "**ID:** `src/app.py::main`" in text
    assert "**Calls:** `src/util.py::helper`, `print`" in text
    # No inner MACHINE/HUMAN markers and no document-level type marker.
    assert "MACHINE" not in text
    assert "HUMAN" not in text
    assert "CTX_SYNC_CONTEXT_TYPE" not in text


def test_refresh_preserves_prose_byte_for_byte(graph_payload: dict) -> None:
    entities = entities_from_graph(graph_payload, root=Path("/repo"))
    original = render_empty(entities)
    prose = (
        "#### Human Notes & Discoveries\n"
        "    legacy indent\n"
        "### Not an entity heading\n"
        "<!-- CTX-SYNC:CALL-GRAPH-START -->\n"
        "hand-written text that looks managed\n"
        "<!-- CTX-SYNC:CALL-GRAPH-END -->\n"
    )
    original = original.rstrip("\n") + "\n\n" + prose
    document = parse_document(original)
    assert len(document.entities) == 2

    updated = sync_document(document, entities)
    assert prose in updated.text
    # A second pass is a no-op.
    second = sync_document(parse_document(updated.text), entities)
    assert second.text == updated.text


def test_block_missing_from_export_is_left_untouched(graph_payload: dict) -> None:
    entities = entities_from_graph(graph_payload, root=Path("/repo"))
    original = render_empty(entities)
    document = parse_document(original)

    remaining = [e for e in entities if e.entity_id == "src/app.py::main"]
    updated = sync_document(document, remaining)

    # The block survives where it was, byte for byte, with its old facts.
    assert updated.text.count("**ID:** `src/util.py::helper`") == 1
    helper_tail = original[original.index("**ID:** `src/util.py::helper`") :]
    assert updated.text.endswith(helper_tail)
    assert updated.text.index("`src/app.py::main`") < updated.text.index(
        "`src/util.py::helper`"
    )
    assert "**Source Status:** missing_from_source" not in updated.text
    assert updated.ignored == 1
    assert updated.added == 0


def test_returning_entity_is_refreshed_in_place(graph_payload: dict) -> None:
    entities = entities_from_graph(graph_payload, root=Path("/repo"))
    document = parse_document(render_empty(entities))
    without_helper = [e for e in entities if e.entity_id != "src/util.py::helper"]
    shrunk = sync_document(document, without_helper)
    assert "src/util.py::helper" in shrunk.text

    # Simulate a block that stayed in the document while the code moved on.
    stale = shrunk.text.replace(
        "**ID:** `src/util.py::helper`\n",
        "**ID:** `src/util.py::helper`\n**Signature:** old()\n",
        1,
    )
    restored = sync_document(parse_document(stale), entities)
    assert restored.updated == 1
    assert restored.added == 0
    assert "**Signature:** old()" not in restored.text
    assert restored.text.count(START) == 2


def test_structure_prose_and_order_are_preserved(graph_payload: dict) -> None:
    entities = entities_from_graph(graph_payload, root=Path("/repo"))
    document_text = (
        "# My Notes\n\n"
        "Intro paragraph.\n\n"
        f"{START}\n"
        "### a heading I chose\n"
        "**Source Status:** active\n"
        "**ID:** `src/app.py::main`\n"
        "**Calls:** stale\n"
        f"{END}\n\n"
        "Some prose between blocks.\n\n"
        f"{START}\n"
        "### a deleted thing\n"
        "**Source Status:** missing_from_source\n"
        "**ID:** `src/gone.py::gone`\n"
        f"{END}\n\n"
        "Trailing notes.\n"
    )

    result = sync_document(parse_document(document_text), entities)
    text = result.text

    # Facts inside the matched block are refreshed; the block is regenerated.
    assert "**Calls:** stale" not in text
    assert "**Calls:** `src/util.py::helper`, `print`" in text
    # Unknown block kept verbatim, including its stale status.
    assert "**Source Status:** missing_from_source" in text
    assert "src/gone.py::gone" in text
    # Surrounding structure and order survive.
    assert text.startswith("# My Notes\n\nIntro paragraph.\n")
    assert text.index("Intro paragraph.") < text.index("Some prose between blocks.")
    assert text.index("Some prose between blocks.") < text.index("Trailing notes.")
    assert "CTX_SYNC_CONTEXT_TYPE" not in text
    assert (result.updated, result.added, result.ignored) == (1, 1, 1)

    # The new entity is appended after everything, including the trailing prose.
    assert text.rstrip().endswith(END)
    assert text.index("Trailing notes.") < text.index("**ID:** `src/util.py::helper`")

    second = sync_document(parse_document(text), entities)
    assert second.text == text
    assert (second.updated, second.added, second.unchanged) == (0, 0, 2)


def test_rejects_error_payload() -> None:
    import pytest

    from ctxsync.graph import GraphInputError

    with pytest.raises(GraphInputError):
        entities_from_graph({"status": "error", "error": "boom"})
