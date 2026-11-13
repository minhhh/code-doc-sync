"""Tests for typed export projections."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codegraph.cli import app
from codegraph.export import GraphTypeError, canonical_graph_type

runner = CliRunner()


def _repo(tmp_path: Path) -> Path:
    root = tmp_path.resolve()
    (root / ".git").mkdir()
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text(
        "from .util import helper, load\n\n\n"
        "def main():\n    return helper()\n\n\n"
        "def start():\n    return load()\n",
        encoding="utf-8",
    )
    (root / "src" / "util.py").write_text(
        "def helper():\n    return 1\n\n\ndef load():\n    return 2\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture
def built_repo(tmp_path: Path) -> Path:
    root = _repo(tmp_path)
    build = runner.invoke(app, ["build", "--root", str(root)])
    assert build.exit_code == 0, build.stdout
    return root


def _export(root: Path, *args: str) -> dict:
    result = runner.invoke(app, ["export", "--root", str(root), *args])
    assert result.exit_code == 0, result.stderr
    return json.loads(result.stdout)


def test_canonical_graph_type_aliases() -> None:
    assert canonical_graph_type("call-graph") == "call-graph"
    assert canonical_graph_type("ctx") == "call-graph"
    assert canonical_graph_type("architecture") == "community"
    assert canonical_graph_type("communities") == "community"
    assert canonical_graph_type("flow") == "flows"
    assert canonical_graph_type("FLOWS") == "flows"
    with pytest.raises(GraphTypeError):
        canonical_graph_type("nope")


def test_unknown_type_exits_two(built_repo: Path) -> None:
    result = runner.invoke(app, ["export", "--root", str(built_repo), "--type", "nope"])
    assert result.exit_code == 2
    assert "unknown --type 'nope'" in result.stderr
    assert "call-graph, community, flows" in result.stderr


def test_default_export_is_call_graph(built_repo: Path) -> None:
    payload = _export(built_repo)
    assert payload["type"] == "call-graph"
    assert payload["nodes"] and payload["edges"]


def test_community_export_shape(built_repo: Path) -> None:
    payload = _export(built_repo, "--type", "community")

    assert payload["type"] == "community"
    assert payload["communities"]
    assert isinstance(payload["inter_community_edges"], list)
    assert "warnings" in payload
    for community in payload["communities"]:
        assert {"id", "name", "size", "cohesion", "member_count", "files"} <= set(
            community
        )


def test_community_alias_matches_canonical(built_repo: Path) -> None:
    assert _export(built_repo, "--type", "architecture") == _export(
        built_repo, "--type", "community"
    )


def _flows_repo(tmp_path: Path) -> Path:
    """A repo with one non-trivial, self-recursive call tree."""
    root = (tmp_path / "flows").resolve()
    (root / ".git").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text(
        "def main():\n"
        "    return branch() + leaf()\n\n\n"
        "def branch():\n"
        "    return main()\n\n\n"
        "def leaf():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    (root / "src" / "util.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    build = runner.invoke(app, ["build", "--root", str(root)])
    assert build.exit_code == 0, build.stdout
    return root


def test_flows_export_shape(tmp_path: Path) -> None:
    payload = _export(_flows_repo(tmp_path), "--type", "flows")

    assert payload["type"] == "flows"
    flows = payload["flows"]
    assert flows, "expected at least one traced flow"
    for flow in flows:
        assert {
            "key",
            "name",
            "entry_point",
            "criticality",
            "unique_nodes",
            "tree_nodes",
            "root",
        } <= set(flow)
        assert flow["root"]["qn"] == flow["entry_point"]
        assert flow["tree_nodes"] >= flow["unique_nodes"] >= 1
        assert flow["key"].startswith(f"{flow['entry_point']}#")


def test_flows_tree_keeps_edges_and_dedupes_targets(tmp_path: Path) -> None:
    payload = _export(_flows_repo(tmp_path), "--type", "flows")

    def walk(node: dict) -> list[dict]:
        children = node.get("calls", [])
        return [child for child in children] + [
            nested for child in children for nested in walk(child)
        ]

    for flow in payload["flows"]:
        nodes = walk(flow["root"])
        targets = [node.get("qn") or node.get("ref") for node in flow["root"].get("calls", [])]
        assert len(targets) == len(set(targets)), "duplicate CALLS edges must collapse"
        assert len(nodes) + 1 == flow["tree_nodes"]
        # Every node is either expanded once or referenced as a revisit.
        expanded = [node["qn"] for node in nodes if "qn" in node]
        assert len(expanded) == len(set(expanded)), "a node must expand at most once"


def test_flows_alias_and_limit(built_repo: Path) -> None:
    payload = _export(built_repo, "--type", "flow", "--limit", "1")
    assert payload["type"] == "flows"
    assert len(payload["flows"]) <= 1


def test_export_out_file_and_summary(built_repo: Path, tmp_path: Path) -> None:
    out = tmp_path / "graph.json"
    result = runner.invoke(
        app,
        [
            "export",
            "--root",
            str(built_repo),
            "--type",
            "community",
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["type"] == "community"
    assert json.loads(out.read_text(encoding="utf-8"))["type"] == "community"
