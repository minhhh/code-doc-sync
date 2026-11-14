"""Tests for CLI hint generation."""

from codegraph.cli.hints import _WORKFLOW, generate_hints


class TestNextSteps:
    def test_workflow_entry_becomes_next_steps(self):
        hints = generate_hints("search", {"status": "ok"})

        assert hints["next_steps"]
        assert all(step in _WORKFLOW["search"] for step in hints["next_steps"])

    def test_command_without_workflow_entry_gets_no_suggestions(self):
        hints = generate_hints("build", {"status": "ok"})

        assert hints["next_steps"] == []

    def test_next_steps_are_capped(self):
        hints = generate_hints("flows list", {})

        assert len(hints["next_steps"]) <= 3

    def test_no_entry_suggests_itself(self):
        for command, steps in _WORKFLOW.items():
            assert all(step["command"] != command for step in steps), command


class TestWarnings:
    def test_high_risk_score_warns(self):
        hints = generate_hints("change risk", {"risk_score": 0.81})

        assert any("High risk score" in w for w in hints["warnings"])

    def test_low_risk_score_stays_quiet(self):
        hints = generate_hints("change risk", {"risk_score": 0.2})

        assert hints["warnings"] == []

    def test_test_gaps_are_named(self):
        hints = generate_hints(
            "health knowledge-gaps", {"test_gaps": [{"name": "do_work"}]},
        )

        assert hints["warnings"] == ["Test coverage gaps: do_work"]

    def test_architecture_warnings_pass_through(self):
        hints = generate_hints(
            "arch overview", {"warnings": ["coupling between a and b"]},
        )

        assert hints["warnings"] == ["coupling between a and b"]


class TestRelated:
    def test_impacted_files_are_listed(self):
        hints = generate_hints(
            "change impact", {"impacted_files": ["a.py", "b.py"]},
        )

        assert hints["related"] == ["a.py", "b.py"]

    def test_related_is_deduplicated_and_capped(self):
        hints = generate_hints(
            "change impact",
            {"impacted_files": ["a.py", "a.py", "b.py", "c.py", "d.py"]},
        )

        assert hints["related"] == ["a.py", "b.py", "c.py"]
