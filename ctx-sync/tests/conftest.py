from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def graph_payload() -> dict:
    return {
        "status": "ok",
        "root": "/repo",
        "nodes": [
            {
                "kind": "File",
                "name": "/repo/src/app.py",
                "qualified_name": "/repo/src/app.py",
                "file_path": "/repo/src/app.py",
                "line_start": 1,
                "line_end": 20,
                "language": "python",
                "is_test": False,
            },
            {
                "kind": "Function",
                "name": "main",
                "qualified_name": "/repo/src/app.py::main",
                "file_path": "/repo/src/app.py",
                "line_start": 3,
                "line_end": 8,
                "language": "python",
                "is_test": False,
            },
            {
                "kind": "Function",
                "name": "helper",
                "qualified_name": "/repo/src/util.py::helper",
                "file_path": "/repo/src/util.py",
                "line_start": 1,
                "line_end": 4,
                "language": "python",
                "is_test": False,
            },
        ],
        "edges": [
            {
                "kind": "CALLS",
                "source": "/repo/src/app.py::main",
                "target": "helper",
            },
            {
                "kind": "CALLS",
                "source": "/repo/src/app.py::main",
                "target": "print",
            },
        ],
    }


@pytest.fixture
def dump_file(tmp_path: Path, graph_payload: dict) -> Path:
    path = tmp_path / "dump.json"
    path.write_text(json.dumps(graph_payload), encoding="utf-8")
    return path


@pytest.fixture
def community_payload() -> dict:
    return {
        "status": "ok",
        "root": "/repo",
        "type": "community",
        "summary": "Architecture: 2 communities, 1 inter-community pairs",
        "communities": [
            {
                "id": 1,
                "name": "core",
                "level": 1,
                "cohesion": 0.42,
                "size": 3,
                "dominant_language": "python",
                "description": "Core module",
                "member_count": 3,
                "files": ["/repo/src/app.py"],
            },
            {
                "id": 2,
                "name": "util",
                "level": 1,
                "cohesion": 0.1,
                "size": 2,
                "dominant_language": "python",
                "description": "",
                "member_count": 2,
                "files": ["/repo/src/util.py"],
            },
        ],
        "inter_community_edges": [
            {
                "source_community": 1,
                "target_community": 2,
                "edge_count": 4,
                "edge_kinds": ["CALLS", "IMPORTS_FROM"],
            }
        ],
        "warnings": [],
    }


@pytest.fixture
def flows_payload() -> dict:
    return {
        "status": "ok",
        "root": "/repo",
        "type": "flows",
        "summary": "Flows: 1 execution paths",
        "flows": [
            {
                "key": "/repo/src/app.py::main#deadbeefcafe",
                "name": "main",
                "entry_point": "/repo/src/app.py::main",
                "criticality": 0.8123,
                "unique_nodes": 3,
                "tree_nodes": 3,
                "root": {
                    "qn": "/repo/src/app.py::main",
                    "calls": [
                        {
                            "qn": "/repo/src/util.py::helper",
                            "calls": [
                                {"qn": "/repo/src/util.py::load", "calls": []},
                                {"ref": "/repo/src/app.py::main"},
                            ],
                        }
                    ],
                },
            }
        ],
    }


@pytest.fixture
def community_file(tmp_path: Path, community_payload: dict) -> Path:
    path = tmp_path / "community.json"
    path.write_text(json.dumps(community_payload), encoding="utf-8")
    return path


@pytest.fixture
def flows_file(tmp_path: Path, flows_payload: dict) -> Path:
    path = tmp_path / "flows.json"
    path.write_text(json.dumps(flows_payload), encoding="utf-8")
    return path
