import os

import pytest

from lmplab import agent
from lmplab.config import settings

os.environ.setdefault("LMPLAB_NO_LLM", "1")
SETTINGS = settings()
HAS_REPORTS = (SETTINGS.paths.reports / "analytics.json").exists() and (SETTINGS.paths.reports / "pdup.json").exists()
HAS_WAREHOUSE = SETTINGS.paths.warehouse.exists()


@pytest.mark.parametrize("sql", [
    "DROP TABLE area",
    "SELECT 1; SELECT 2",
    "COPY v_hour TO 'out.csv'",
    "SELECT * FROM read_csv('C:/secrets.csv')",
    "ATTACH 'other.db'",
    "PRAGMA database_list",
    "UPDATE area SET name = 'x'",
])
def test_sql_guard_rejects_anything_but_one_read_only_select(sql):
    result = agent.Toolbox(SETTINGS).query_warehouse(sql)
    assert "error" in result


@pytest.mark.skipif(not HAS_WAREHOUSE, reason="needs the DuckDB warehouse (uv run lmplab fetch ingest)")
def test_sql_tool_answers_and_caps_rows():
    result = agent.Toolbox(SETTINGS).query_warehouse("SELECT area_id, name FROM area ORDER BY area_id")
    assert result["columns"] == ["area_id", "name"]
    assert result["row_count"] == 42


def test_area_names_and_numbers_resolve():
    assert agent.find_area("Medicine Hat") == 4
    assert agent.find_area("why is area 18 so expensive") == 18
    assert agent.find_area("Lac La Biche") == 27
    assert agent.find_area("the moon") is None


def test_planner_routes_questions_to_tools():
    tools = [name for name, _ in agent.plan(agent.DEMO_QUESTIONS[1])]
    assert tools == ["run_what_if", "run_what_if"]   # the change alone, then with the uprate
    _, with_uprate = agent.plan(agent.DEMO_QUESTIONS[1])
    assert with_uprate[1] == {"add_se_wind_mw": 400.0, "uprate_corridor": "SW-SE", "uprate_mw": 400.0}
    assert [n for n, _ in agent.plan(agent.DEMO_QUESTIONS[2])] == ["pdup_review"]
    assert [n for n, _ in agent.plan(agent.DEMO_QUESTIONS[0])] == ["area_profile"]


def test_grounding_traces_rounded_numbers_and_flags_invented_ones():
    steps = [{"tool": "x", "arguments": {}, "result": {"mae": 10.554, "share": 0.4318, "name": "4 Medicine Hat"}}]
    good = agent.grounding("Error $10.55/MWh, separated 43% of hours in area 4.", steps)
    assert good == {"numbers": 3, "traced": 3, "untraced": []}
    bad = agent.grounding("Error $12.10/MWh.", steps)
    assert bad["untraced"] == ["$12.10"]


@pytest.mark.skipif(not HAS_REPORTS, reason="needs artifacts/reports")
def test_offline_answer_comes_from_the_tools():
    result = agent.ask(SETTINGS, agent.DEMO_QUESTIONS[2])
    assert result["mode"] == "deterministic planner"
    assert "Resubmission required" in result["answer"]
    assert result["grounding"]["untraced"] == []


class ScriptedLLM:
    """Stands in for the model server: asks for one tool, then answers with the given text."""

    def __init__(self, final_text: str):
        self.final_text = final_text
        self.calls = 0

    def model(self) -> str:
        return "scripted"

    def complete(self, messages, tools=None, max_tokens=0, temperature=0.0):
        self.calls += 1
        if self.calls == 1:
            return {"content": "", "tool_calls": [{"id": "c1", "function": {
                "name": "pdup_review", "arguments": '{"project": "Prairie Ridge"}', "parsed": {"project": "Prairie Ridge"}}}]}
        assert messages[-1]["role"] == "tool" and "Resubmission required" in messages[-1]["content"]
        return {"content": self.final_text}


@pytest.mark.skipif(not HAS_REPORTS, reason="needs artifacts/reports")
def test_llm_loop_runs_the_requested_tool_and_keeps_the_model_answer():
    result = agent.ask(SETTINGS, "Is Prairie Ridge ready?", llm=ScriptedLLM("Not yet: 10 deficiencies."))
    assert [s["tool"] for s in result["steps"]] == ["pdup_review"]
    assert result["answer"] == "Not yet: 10 deficiencies." and result["answer_source"] == "model"
    assert result["mode"] == "local LLM (scripted)" and result["grounding"]["untraced"] == []


@pytest.mark.skipif(not HAS_REPORTS, reason="needs artifacts/reports")
def test_empty_model_answer_falls_back_to_tool_facts():
    result = agent.ask(SETTINGS, "Is Prairie Ridge ready?", llm=ScriptedLLM(""))
    assert result["answer_source"].startswith("tool facts")
    assert "Resubmission required" in result["answer"]


@pytest.mark.skipif(not HAS_REPORTS, reason="needs artifacts/reports")
def test_tool_results_carry_verified_facts():
    profile = agent.Toolbox(SETTINGS).call("area_profile", {"area": "Medicine Hat"})
    assert profile["area"] == "4 Medicine Hat"
    assert "Medicine Hat averaged" in profile["facts"]
    assert agent.Toolbox(SETTINGS).call("no_such_tool", {}) == {"error": "Unknown tool no_such_tool"}
