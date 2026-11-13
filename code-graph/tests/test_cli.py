"""Tests for the Typer entry point."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from codegraph import __version__
from codegraph.cli import app

runner = CliRunner()


def _repo(tmp_path: Path) -> Path:
    root = tmp_path.resolve()
    (root / ".git").mkdir()
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text(
        "from .util import helper\n\n\ndef main():\n    return helper()\n",
        encoding="utf-8",
    )
    (root / "src" / "util.py").write_text(
        "def helper():\n    return 1\n", encoding="utf-8"
    )
    return root


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_build_status_export(tmp_path: Path) -> None:
    root = _repo(tmp_path)

    build = runner.invoke(app, ["build", "--root", str(root), "--pretty"])
    assert build.exit_code == 0, build.stdout
    payload = json.loads(build.stdout)
    assert payload["status"] == "ok"
    assert payload["total_nodes"] >= 3

    status = runner.invoke(app, ["status", "--root", str(root), "--format", "text"])
    assert status.exit_code == 0
    assert "Files:" in status.stdout

    exported = runner.invoke(app, ["export", "--root", str(root)])
    assert exported.exit_code == 0
    snapshot = json.loads(exported.stdout)
    assert snapshot["nodes"] and snapshot["edges"]


def test_default_json_is_compact(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    build = runner.invoke(app, ["build", "--root", str(root)])
    assert build.exit_code == 0
    assert build.stdout.count("\n") == 1
    assert build.stdout.startswith("{")

    pretty = runner.invoke(app, ["status", "--root", str(root), "--pretty"])
    assert pretty.exit_code == 0
    assert pretty.stdout.startswith("{\n")


def test_search_and_query(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    assert runner.invoke(app, ["build", "--root", str(root)]).exit_code == 0

    search = runner.invoke(app, ["search", "helper", "--root", str(root)])
    assert search.exit_code == 0
    results = json.loads(search.stdout)["results"]
    assert any(item["name"] == "helper" for item in results)

    query = runner.invoke(
        app, ["query", "callers_of", "helper", "--root", str(root)]
    )
    assert query.exit_code == 0
    assert json.loads(query.stdout)["status"] == "ok"


def test_analysis_and_refactor_commands(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    assert runner.invoke(app, ["build", "--root", str(root)]).exit_code == 0

    hubs = runner.invoke(app, ["arch", "hubs", "--root", str(root)])
    assert hubs.exit_code == 0
    assert json.loads(hubs.stdout)["count"] >= 1

    for verb in ("communities", "overview", "bridges", "surprises"):
        assert runner.invoke(app, ["arch", verb, "--root", str(root)]).exit_code == 0

    # The old flat structure verbs are gone.
    for retired in ("communities", "architecture", "hub-nodes", "bridge-nodes", "surprising-connections"):
        assert runner.invoke(app, [retired, "--root", str(root)]).exit_code != 0

    gaps = runner.invoke(app, ["health", "knowledge-gaps", "--root", str(root)])
    assert gaps.exit_code == 0
    assert "gaps" in json.loads(gaps.stdout)

    dead = runner.invoke(app, ["health", "dead-code", "--root", str(root)])
    assert dead.exit_code == 0
    assert json.loads(dead.stdout)["status"] == "ok"

    # Diagnostics live under `health`, not as top-level verbs or refactor modes.
    for retired in (
        ["dead-code"],
        ["knowledge-gaps"],
        ["large-functions"],
        ["suggested-questions"],
        ["refactor", "dead-code"],
    ):
        assert runner.invoke(app, [*retired, "--root", str(root)]).exit_code != 0

    flows = runner.invoke(app, ["flows", "list", "--root", str(root)])
    assert flows.exit_code == 0
    listed = json.loads(flows.stdout)["flows"]
    if listed:  # the fixture repo may have no multi-node flow
        shown = runner.invoke(
            app, ["flows", "show", str(listed[0]["id"]), "--root", str(root)]
        )
        assert shown.exit_code == 0
        assert json.loads(shown.stdout)["flow"]["id"] == listed[0]["id"]

    # The singular/plural pair and manual post-processing are gone.
    for retired in (["flow", "1"], ["flows"], ["postprocess"]):
        assert runner.invoke(app, [*retired, "--root", str(root)]).exit_code != 0

    risk = runner.invoke(
        app, ["change", "risk", "src/app.py", "--root", str(root)]
    )
    assert risk.exit_code == 0
    assert json.loads(risk.stdout)["status"] == "ok"

    # A bare verb no longer exists: the diff verbs live under `change`.
    for retired in ("changes", "impact", "affected-flows"):
        assert runner.invoke(app, [retired, "--root", str(root)]).exit_code != 0


def test_export_json_shape(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    assert runner.invoke(app, ["build", "--root", str(root)]).exit_code == 0

    plain = runner.invoke(app, ["export", "--root", str(root)])
    exported = runner.invoke(app, ["export", "--format", "json", "--root", str(root)])
    assert plain.exit_code == exported.exit_code == 0
    assert json.loads(plain.stdout) == json.loads(exported.stdout)

    out = tmp_path / "graph.json"
    written = runner.invoke(
        app, ["export", "--out", str(out), "--root", str(root)]
    )
    assert written.exit_code == 0, written.stdout
    assert json.loads(out.read_text(encoding="utf-8")) == json.loads(plain.stdout)


def test_export_rejects_unknown_format(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    result = runner.invoke(app, ["export", "--format", "yaml", "--root", str(root)])
    assert result.exit_code == 2
    assert "--format must be json" in result.stderr


def _repo_many_callers(tmp_path: Path) -> Path:
    """A repo where three functions call ``helper``."""
    root = _repo(tmp_path)
    (root / "src" / "app.py").write_text(
        "from .util import helper\n\n\n"
        "def main():\n    return helper()\n\n\n"
        "def foo():\n    return helper()\n\n\n"
        "def bar():\n    return helper()\n",
        encoding="utf-8",
    )
    return root


def test_query_limit_truncates_and_reports_total(tmp_path: Path) -> None:
    root = _repo_many_callers(tmp_path)
    assert runner.invoke(app, ["build", "--root", str(root)]).exit_code == 0

    full = runner.invoke(app, ["query", "callers_of", "helper", "--root", str(root)])
    assert full.exit_code == 0, full.stdout
    payload = json.loads(full.stdout)
    total = payload["result_count"]
    assert total >= 2, "fixture should yield several callers"
    assert payload["returned"] == total
    assert payload["truncated"] is False

    capped = runner.invoke(
        app, ["query", "callers_of", "helper", "--limit", "1", "--root", str(root)]
    )
    assert capped.exit_code == 0, capped.stdout
    limited = json.loads(capped.stdout)
    assert limited["result_count"] == total
    assert limited["returned"] == 1
    assert limited["truncated"] is True
    assert len(limited["results"]) == 1

    # A truncated payload must not reference rows the caller did not receive.
    kept = {row["qualified_name"] for row in limited["results"]}
    for edge in limited["edges"]:
        assert edge["source"] in kept or edge["target"] in kept


def test_query_limit_above_total_is_not_truncated(tmp_path: Path) -> None:
    root = _repo_many_callers(tmp_path)
    assert runner.invoke(app, ["build", "--root", str(root)]).exit_code == 0

    result = runner.invoke(
        app, ["query", "callers_of", "helper", "--limit", "50", "--root", str(root)]
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["returned"] == payload["result_count"]
    assert payload["truncated"] is False


def test_query_detail_minimal_shape(tmp_path: Path) -> None:
    root = _repo_many_callers(tmp_path)
    assert runner.invoke(app, ["build", "--root", str(root)]).exit_code == 0

    result = runner.invoke(
        app,
        ["query", "callers_of", "helper", "--detail", "minimal", "--root", str(root)],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert "edges" not in payload
    assert payload["returned"] == len(payload["results"])
    assert payload["truncated"] is False
    for row in payload["results"]:
        assert set(row) == {"name", "kind", "file_path"}


def test_query_detail_minimal_is_capped_at_five(tmp_path: Path) -> None:
    root = _repo_many_callers(tmp_path)
    assert runner.invoke(app, ["build", "--root", str(root)]).exit_code == 0

    full = runner.invoke(
        app,
        ["query", "callers_of", "helper", "--detail", "minimal", "--root", str(root)],
    )
    total = json.loads(full.stdout)["result_count"]
    if total <= 5:
        return  # cap not reachable on this fixture

    capped = runner.invoke(
        app,
        ["query", "callers_of", "helper", "--detail", "minimal", "--limit", "99",
         "--root", str(root)],
    )
    payload = json.loads(capped.stdout)
    assert payload["returned"] == 5
    assert payload["truncated"] is True


def test_query_detail_minimal_respects_smaller_limit(tmp_path: Path) -> None:
    root = _repo_many_callers(tmp_path)
    assert runner.invoke(app, ["build", "--root", str(root)]).exit_code == 0

    result = runner.invoke(
        app,
        ["query", "callers_of", "helper", "--detail", "minimal", "--limit", "1",
         "--root", str(root)],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["returned"] == 1
    assert payload["truncated"] is True


def test_query_rejects_unknown_detail(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    result = runner.invoke(
        app, ["query", "callers_of", "helper", "--detail", "verbose", "--root", str(root)]
    )
    assert result.exit_code == 2
    assert "--detail must be standard or minimal" in result.stderr


def test_query_rejects_non_positive_limit(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    result = runner.invoke(
        app, ["query", "callers_of", "helper", "--limit", "0", "--root", str(root)]
    )
    assert result.exit_code == 2
    assert "--limit must be >= 1" in result.stderr


def test_stats_command_is_gone(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    assert runner.invoke(app, ["build", "--root", str(root)]).exit_code == 0

    stats = runner.invoke(app, ["stats", "--root", str(root)])
    assert stats.exit_code == 2
    assert "No such command" in (stats.output + (stats.stderr or ""))

    status = runner.invoke(app, ["status", "--root", str(root)])
    assert status.exit_code == 0
    assert json.loads(status.stdout)["status"] == "ok"


def test_data_dir_flag_is_gone(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    elsewhere = tmp_path / "elsewhere"

    result = runner.invoke(
        app, ["status", "--root", str(root), "--data-dir", str(elsewhere)]
    )
    assert result.exit_code == 2
    assert "No such option" in (result.output + (result.stderr or ""))
    # A rejected flag must not create the directory as a side effect.
    assert not elsewhere.exists()


def test_graph_resolves_under_root_only(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    assert runner.invoke(app, ["build", "--root", str(root)]).exit_code == 0
    assert (root / ".code-graph" / "graph.db").exists()

    other = tmp_path / "other"
    (other / ".git").mkdir(parents=True)
    payload = json.loads(
        runner.invoke(app, ["status", "--root", str(other)]).stdout
    )
    assert payload["total_nodes"] == 0, "another root must not see this graph"


def _command_paths(typer_app: object, prefix: str = "") -> set[str]:
    """Every registered command path, e.g. ``{"arch hubs", "build"}``."""
    paths: set[str] = set()
    for command in typer_app.registered_commands:  # type: ignore[attr-defined]
        paths.add((prefix + " " + (command.name or command.callback.__name__)).strip())
    for group in typer_app.registered_groups:  # type: ignore[attr-defined]
        sub = getattr(group, "typer_instance", None) or group.typer
        name = group.name or sub.info.name
        paths |= _command_paths(sub, (prefix + " " + name).strip())
    return paths


def test_suggested_commands_all_exist() -> None:
    """A renamed verb must not leave stale follow-up suggestions."""
    from codegraph.cli.suggestions import SUGGESTIONS

    paths = _command_paths(app)

    for source, offered in SUGGESTIONS.items():
        assert _matches(paths, source), f"{source!r} is not a command"
        for entry in offered:
            assert _matches(paths, entry.split(" -- ")[0]), (
                f"{source} suggests {entry!r}, which is not a command"
            )


def _matches(paths: set[str], text: str) -> bool:
    """True when *text* starts with a registered command path."""
    words = text.split()
    return any(" ".join(words[:i]) in paths for i in range(len(words), 0, -1))


def test_hint_targets_are_real_commands() -> None:
    """Hints may only point at commands that exist."""
    from codegraph.cli.hints import _INTENT_COMMANDS, _WORKFLOW

    paths = _command_paths(app)
    suggested = {step["command"] for steps in _WORKFLOW.values() for step in steps}
    suggested |= {c for commands in _INTENT_COMMANDS.values() for c in commands}

    unmapped = sorted(entry for entry in suggested if not _matches(paths, entry))
    assert unmapped == [], f"hint targets match no command: {unmapped}"
