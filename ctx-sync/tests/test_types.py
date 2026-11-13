"""Tests for the community (architecture) and flows context types."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from ctxsync.cli import app
from ctxsync.flows_doc import (
    END_MARKER as END,
    START_MARKER as START,
    parse_blocks,
)

runner = CliRunner()


def _sync(target: Path, payload_file: Path, *extra: str):
    # --root keeps the fixture's /repo paths rendering as repo-relative.
    return runner.invoke(
        app,
        ["--target", str(target), "--input", str(payload_file), "--root", "/repo", *extra],
    )


def test_community_document_shape(tmp_path: Path, community_file: Path) -> None:
    target = tmp_path / "ARCHITECTURE.md"

    result = _sync(target, community_file, "--type", "community")
    assert result.exit_code == 0, result.stderr
    text = target.read_text(encoding="utf-8")

    assert text.startswith("<!-- CTX-SYNC:COMMUNITY-START -->")
    assert text.count("<!-- CTX-SYNC:COMMUNITY-START -->") == 2
    assert "# Context" not in text
    assert "**ID:** `community::core" in text
    assert "**ID:** `community::util" in text
    assert "**Kind:**" not in text
    assert "**Members:** 3" in text
    assert "**Cohesion:** 0.420" in text
    assert "**Description:** Core module" in text
    assert "**Files:** `src/app.py`" in text
    # Language comes from the entity, not from metadata: exactly one line each.
    assert text.count("**Language:**") == 2
    # Inter-community edges render on both ends.
    assert "**Connected to:** util — 4 edges (CALLS/IMPORTS_FROM)" in text
    assert "**Connected to:** core — 4 edges (CALLS/IMPORTS_FROM)" in text
    # A community spans files, so per-file facts and call edges are dropped.
    assert "**File:**" not in text
    assert "**Calls:**" not in text
    assert "**Called by:**" not in text


def test_community_aliases_canonicalize(tmp_path: Path, community_file: Path) -> None:
    target = tmp_path / "ARCHITECTURE.md"

    result = _sync(target, community_file, "--type", "architecture")
    assert result.exit_code == 0, result.stderr
    assert target.read_text(encoding="utf-8").startswith(
        "<!-- CTX-SYNC:COMMUNITY-START -->"
    )


def test_community_document_is_idempotent(tmp_path: Path, community_file: Path) -> None:
    target = tmp_path / "ARCHITECTURE.md"

    assert _sync(target, community_file, "--type", "community").exit_code == 0
    first = target.read_text(encoding="utf-8")

    assert _sync(target, community_file, "--type", "community").exit_code == 0
    assert target.read_text(encoding="utf-8") == first

    check = _sync(target, community_file, "--type", "community", "--check")
    assert check.exit_code == 0, check.stderr


def test_community_keeps_human_notes_and_ignores_removed(
    tmp_path: Path, community_payload: dict
) -> None:
    target = tmp_path / "ARCHITECTURE.md"
    first = tmp_path / "first.json"
    first.write_text(json.dumps(community_payload), encoding="utf-8")
    assert _sync(target, first, "--type", "community").exit_code == 0

    noted = "# Architecture\n\nutil must never import core.\n\n" + target.read_text(
        encoding="utf-8"
    )
    target.write_text(noted, encoding="utf-8")

    shrunk = dict(community_payload)
    shrunk["communities"] = [community_payload["communities"][1]]
    shrunk["inter_community_edges"] = []
    second = tmp_path / "second.json"
    second.write_text(json.dumps(shrunk), encoding="utf-8")

    result = _sync(target, second, "--type", "community")
    assert result.exit_code == 0, result.stderr
    text = target.read_text(encoding="utf-8")
    assert "util must never import core." in text
    # The community that left the export keeps its block and its notes.
    assert "`community::core#" in text or "**ID:** `community::core`" in text
    assert "**Source Status:** missing_from_source" not in text
    assert "## Archived" not in text


def test_community_names_collide_but_ids_do_not(tmp_path: Path) -> None:
    """Two communities may share a name; membership separates them."""

    def community(community_id: int, size: int, files: list[str]) -> dict:
        return {
            "id": community_id,
            "name": "codegraph-find",
            "level": 1,
            "cohesion": 0.2,
            "size": size,
            "dominant_language": "python",
            "description": "",
            "member_count": size,
            "files": files,
        }

    def payload(first_id: int, second_id: int) -> dict:
        return {
            "status": "ok",
            "root": "/repo",
            "type": "community",
            "communities": [
                community(first_id, 5, ["/repo/src/a.py"]),
                community(second_id, 3, ["/repo/src/b.py"]),
            ],
            "inter_community_edges": [],
            "warnings": [],
        }

    first = tmp_path / "a.json"
    first.write_text(json.dumps(payload(235, 340)), encoding="utf-8")
    target = tmp_path / "ARCHITECTURE.md"
    assert _sync(target, first, "--type", "community").exit_code == 0
    text = target.read_text(encoding="utf-8")

    ids = [line for line in text.splitlines() if line.startswith("**ID:**")]
    assert len(ids) == 2, ids
    assert len(set(ids)) == 2
    assert all(i.startswith("**ID:** `community::codegraph-find#") for i in ids)
    # Duplicate names stay readable; the id carries the distinction.
    assert text.count("**ID:** `community::codegraph-find#") == 2

    # Same communities, renumbered by a full rebuild: nothing changes.
    second = tmp_path / "b.json"
    second.write_text(json.dumps(payload(328, 340)), encoding="utf-8")
    assert _sync(target, second, "--type", "community").exit_code == 0
    assert target.read_text(encoding="utf-8") == text


def test_all_three_types_share_the_marker_pair(
    tmp_path: Path, dump_file: Path, community_file: Path, flows_file: Path
) -> None:
    """One marker shape, one tag per type: START above, END below."""
    expected = {
        "call-graph": "CALL-GRAPH",
        "community": "COMMUNITY",
        "flows": "FLOW",
    }
    for context_type, tag in expected.items():
        source = {
            "call-graph": dump_file,
            "community": community_file,
            "flows": flows_file,
        }[context_type]
        target = tmp_path / f"{tag}.md"
        result = _sync(target, source, "--type", context_type)
        assert result.exit_code == 0, result.stderr
        text = target.read_text(encoding="utf-8")
        assert f"<!-- CTX-SYNC:{tag}-START -->" in text
        assert f"<!-- CTX-SYNC:{tag}-END -->" in text
        assert text.count(f"<!-- CTX-SYNC:{tag}-START -->") == text.count(
            f"<!-- CTX-SYNC:{tag}-END -->"
        )
        for other in set(expected.values()) - {tag}:
            assert f"CTX-SYNC:{other}-" not in text


def test_community_key_from_export_is_used(
    tmp_path: Path, community_payload: dict
) -> None:
    keyed = json.loads(json.dumps(community_payload))
    keyed["communities"][0]["key"] = "core#deadbeefcafe"
    source = tmp_path / "keyed.json"
    source.write_text(json.dumps(keyed), encoding="utf-8")
    target = tmp_path / "ARCHITECTURE.md"
    assert _sync(target, source, "--type", "community").exit_code == 0
    assert "`community::core#deadbeefcafe`" in target.read_text(encoding="utf-8")


def test_flows_document_is_a_call_tree(
    tmp_path: Path, flows_file: Path
) -> None:
    target = tmp_path / "FLOWS.md"

    result = _sync(target, flows_file, "--type", "flows")
    assert result.exit_code == 0, result.stderr
    text = target.read_text(encoding="utf-8")

    assert START in text and END in text
    # No document scaffolding and no JSON: a flow reads as a nested arrow list.
    assert "CTX_SYNC_CONTEXT_TYPE" not in text
    assert "CALL-GRAPH" not in text
    assert "###" not in text
    assert "**" not in text
    assert "{" not in text

    body = text.split(START)[1].split(END)[0]
    expected = (
        "\n* flow=src/app.py::main#deadbeefcafe\n"
        "    * entry_point=src/app.py::main\n"
        "    * criticality=0.8123\n"
        "    * nodes=3/3\n"
        "* src/app.py::main\n"
        "    * → src/util.py::helper\n"
        "        * → src/util.py::load\n"
        "        * → src/app.py::main\n"
    )
    assert body == expected

    meta = parse_blocks(text)
    assert meta == [
        {
            "key": "src/app.py::main#deadbeefcafe",
            "entry_point": "src/app.py::main",
            "criticality": "0.8123",
            "nodes": "3/3",
        }
    ]


def test_flows_alias_and_idempotence(tmp_path: Path, flows_file: Path) -> None:
    target = tmp_path / "FLOWS.md"

    assert _sync(target, flows_file, "--type", "flow").exit_code == 0
    first = target.read_text(encoding="utf-8")

    assert _sync(target, flows_file, "--type", "flows").exit_code == 0
    assert target.read_text(encoding="utf-8") == first

    check = _sync(target, flows_file, "--type", "flows", "--check")
    assert check.exit_code == 0, check.stderr


def test_flows_preserves_surrounding_prose(
    tmp_path: Path, flows_payload: dict
) -> None:
    target = tmp_path / "FLOWS.md"
    source = tmp_path / "flows.json"
    source.write_text(json.dumps(flows_payload), encoding="utf-8")

    assert _sync(target, source, "--type", "flows").exit_code == 0

    prose = (
        "# Execution flows\n\n"
        "Hand-written intro that must survive.\n\n"
        f"{START}\n* flow=src/old.py::gone#abcd\n* src/old.py::gone\n{END}\n\n"
        "Trailing notes, also verbatim.\n"
    )
    target.write_text(prose, encoding="utf-8")

    assert _sync(target, source, "--type", "flows").exit_code == 0
    text = target.read_text(encoding="utf-8")

    assert text.startswith("# Execution flows\n\nHand-written intro that must survive.\n")
    assert "Trailing notes, also verbatim.\n" in text
    # The vanished flow keeps its block, byte for byte, and is not flagged.
    assert "    * status=missing_from_source\n" not in text
    assert f"{START}\n* flow=src/old.py::gone#abcd\n* src/old.py::gone\n{END}" in text
    # A flow the document did not have is appended after the trailing prose.
    assert "src/app.py::main#deadbeefcafe" in text
    assert text.index("Trailing notes") < text.index("src/app.py::main#deadbeefcafe")
    # Re-syncing is stable.
    assert _sync(target, source, "--type", "flows").exit_code == 0
    assert target.read_text(encoding="utf-8") == text


def test_flows_second_flow_is_appended(tmp_path: Path, flows_payload: dict) -> None:
    target = tmp_path / "FLOWS.md"
    first = tmp_path / "a.json"
    first.write_text(json.dumps(flows_payload), encoding="utf-8")
    assert _sync(target, first, "--type", "flows").exit_code == 0

    extra = json.loads(json.dumps(flows_payload))
    extra["flows"].append(
        {
            "key": "/repo/src/other.py::run#111122223333",
            "name": "run",
            "entry_point": "/repo/src/other.py::run",
            "criticality": 0.11,
            "unique_nodes": 1,
            "tree_nodes": 1,
            "root": {"qn": "/repo/src/other.py::run", "calls": []},
        }
    )
    second = tmp_path / "b.json"
    second.write_text(json.dumps(extra), encoding="utf-8")

    assert _sync(target, second, "--type", "flows").exit_code == 0
    text = target.read_text(encoding="utf-8")
    assert text.count(START) == 2
    assert "src/other.py::run#111122223333" in text
    assert "* src/other.py::run\n" in text


def test_flows_rejects_legacy_payload(tmp_path: Path) -> None:
    legacy = {
        "status": "ok",
        "root": "/repo",
        "type": "flows",
        "flows": [{"id": 1, "name": "main", "steps": []}],
    }
    source = tmp_path / "legacy.json"
    source.write_text(json.dumps(legacy), encoding="utf-8")
    target = tmp_path / "FLOWS.md"

    result = _sync(target, source, "--type", "flows")
    assert result.exit_code == 1
    assert "re-export" in result.stderr
    assert not target.exists()


def test_call_graph_rendering_unchanged(
    tmp_path: Path, dump_file: Path, graph_payload: dict
) -> None:
    target = tmp_path / "CONTEXT.md"
    assert _sync(target, dump_file).exit_code == 0
    text = target.read_text(encoding="utf-8")

    assert "**File:** `src/app.py`" in text
    assert "**Lines:** 3-8" in text
    assert "**Kind:** Function" in text
    assert "**Calls:** `src/util.py::helper`, `print`" in text
    assert "**Called by:** `src/app.py::main`" in text
