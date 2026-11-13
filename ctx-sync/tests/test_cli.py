from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from ctxsync.cli import app

runner = CliRunner()


def test_sync_from_stdin_and_idempotent(tmp_path: Path, graph_payload: dict) -> None:
    target = tmp_path / "CONTEXT.md"
    payload = json.dumps(graph_payload)

    first = runner.invoke(app, ["--target", str(target)], input=payload)
    assert first.exit_code == 0, first.stdout
    assert target.exists()
    text = target.read_text(encoding="utf-8")
    assert "src/app.py::main" in text

    second = runner.invoke(app, ["--target", str(target)], input=payload)
    assert second.exit_code == 0
    assert target.read_text(encoding="utf-8") == text


def test_dry_run_does_not_write(tmp_path: Path, graph_payload: dict) -> None:
    target = tmp_path / "CONTEXT.md"
    result = runner.invoke(
        app, ["--target", str(target), "--dry-run"], input=json.dumps(graph_payload)
    )
    assert result.exit_code == 0
    assert "<!-- CTX-SYNC:CALL-GRAPH-START -->" in result.stdout
    assert not target.exists()


def test_check_exit_codes(tmp_path: Path, dump_file: Path) -> None:
    target = tmp_path / "CONTEXT.md"
    stale = runner.invoke(
        app, ["--target", str(target), "--input", str(dump_file), "--check"]
    )
    assert stale.exit_code == 1
    assert "out of date" in stale.stderr

    written = runner.invoke(app, ["--target", str(target), "--input", str(dump_file)])
    assert written.exit_code == 0
    clean = runner.invoke(
        app, ["--target", str(target), "--input", str(dump_file), "--check"]
    )
    assert clean.exit_code == 0
    assert "up to date" in clean.stdout


def test_invalid_input_fails(tmp_path: Path) -> None:
    target = tmp_path / "CONTEXT.md"
    result = runner.invoke(app, ["--target", str(target)], input="not json")
    assert result.exit_code == 1
    assert "invalid JSON" in result.stderr


def test_update_without_code_graph_fails(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("ctxsync.cli.shutil.which", lambda _name: None)
    target = tmp_path / "CONTEXT.md"
    result = runner.invoke(app, ["--target", str(target), "--update"])
    assert result.exit_code == 1
    assert "code-graph on PATH" in result.stderr


def test_creates_document_with_block_markers(tmp_path: Path, dump_file: Path) -> None:
    target = tmp_path / "CONTEXT.md"
    result = runner.invoke(
        app, ["--target", str(target), "--input", str(dump_file), "--type", "call-graph"]
    )
    assert result.exit_code == 0
    text = target.read_text(encoding="utf-8")
    assert "<!-- CTX-SYNC:CALL-GRAPH-START -->" in text
    assert "<!-- CTX-SYNC:CALL-GRAPH-END -->" in text
    assert "# Call Graph" not in text


def test_blocks_of_another_type_are_left_alone(tmp_path: Path, dump_file: Path) -> None:
    target = tmp_path / "MIXED.md"
    target.write_text(
        "# Notes\n\n"
        "<!-- CTX-SYNC:COMMUNITY-START -->\n"
        "**ID:** `community::core#abc123`\n"
        "<!-- CTX-SYNC:COMMUNITY-END -->\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["--target", str(target), "--input", str(dump_file)])
    assert result.exit_code == 0, result.stderr
    text = target.read_text(encoding="utf-8")
    # The other type's block is opaque, not an error and not a rewrite target.
    assert "`community::core#abc123`" in text
    assert "<!-- CTX-SYNC:CALL-GRAPH-START -->" in text


def test_unknown_context_type_fails(tmp_path: Path, dump_file: Path) -> None:
    target = tmp_path / "CONTEXT.md"
    result = runner.invoke(
        app, ["--target", str(target), "--input", str(dump_file), "--type", "not-a-type"]
    )
    assert result.exit_code == 1
    assert "unsupported context type" in result.stderr
    assert not target.exists()


def test_wrong_payload_for_type_fails(tmp_path: Path, dump_file: Path) -> None:
    target = tmp_path / "CONTEXT.md"
    result = runner.invoke(
        app, ["--target", str(target), "--input", str(dump_file), "--type", "flows"]
    )
    assert result.exit_code == 1
    assert "no 'flows' array" in result.stderr
    assert not target.exists()
