"""Agent and evaluation tests with a scripted fake model (no network, no cost)."""
import json
from datetime import date

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("sqlglot")

from chatbot.agent import Agent, parse_json  # noqa: E402
from chatbot.db import DuckDBBackend  # noqa: E402
from chatbot.evaluate import results_match, run_eval, values_match  # noqa: E402
from chatbot.llm import Reply  # noqa: E402
from tests.gold_fixture import make_gold  # noqa: E402

import pandas as pd  # noqa: E402


class FakeLLM:
    model = "gpt-4o-mini"

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, messages, json_mode=False, max_tokens=800):
        self.calls.append(messages)
        text = self.replies.pop(0)
        return Reply(text if isinstance(text, str) else json.dumps(text), 100, 20, 0.01)


@pytest.fixture(scope="module")
def backend(tmp_path_factory):
    return DuckDBBackend(make_gold(tmp_path_factory.mktemp("gold")))


def agent(backend, replies):
    return Agent(FakeLLM(replies), backend, today=date(2026, 7, 31))


PEAK_SQL = ("SELECT delivery_date, peak_demand_mw FROM gold.mart_daily_summary "
            "WHERE is_complete_day ORDER BY peak_demand_mw DESC LIMIT 1")


def test_data_facts_go_into_the_prompt(backend):
    a = agent(backend, [{"sql": PEAK_SQL}, "The peak was 20,000 MW."])
    a.ask("peak?")
    system = a.llm.calls[0][0]["content"]
    assert "Today is 2026-07-31" in system and "2026-07-01 to 2026-07-31" in system
    assert "Prices exist only from 2026-07-10" in system


def test_happy_path(backend):
    a = agent(backend, [{"sql": PEAK_SQL, "assumption": ""}, "The peak was 20,000 MW on July 5."])
    ans = a.ask("What was the peak day?")
    assert ans.ok and len(ans.data) == 1 and ans.attempts == 1
    assert ans.text.startswith("The peak")
    assert ans.prompt_tokens == 200 and ans.cost_usd is not None


def test_blocked_query_gets_one_repair(backend):
    a = agent(backend, [{"sql": "SELECT * FROM silver.ieso_demand_hourly"}, {"sql": PEAK_SQL}, "ok"])
    ans = a.ask("peak?")
    assert ans.ok and ans.attempts == 2
    assert "guardrail" in a.llm.calls[1][-1]["content"]


def test_failing_query_gets_one_repair(backend):
    a = agent(backend, [{"sql": "SELECT no_such_column FROM gold.dim_date"}, {"sql": PEAK_SQL}, "ok"])
    ans = a.ask("peak?")
    assert ans.ok and ans.attempts == 2
    assert "failed" in a.llm.calls[1][-1]["content"]


def test_gives_up_after_two_attempts(backend):
    a = agent(backend, [{"sql": "DROP TABLE gold.dim_date"}, {"sql": "DELETE FROM gold.dim_date"}])
    ans = a.ask("delete it")
    assert not ans.ok and "guardrail" in ans.error and ans.data is None
    assert len(backend.run("SELECT * FROM gold.dim_date LIMIT 1")) == 1   # still there


def test_refusal(backend):
    ans = agent(backend, [{"refuse": "I only have Ontario data."}]).ask("Quebec prices?")
    assert ans.refused and not ans.ok and "Ontario" in ans.text


def test_invalid_json_then_valid(backend):
    ans = agent(backend, ["not json at all", {"sql": PEAK_SQL}, "ok"]).ask("peak?")
    assert ans.ok and ans.attempts == 2


def test_parse_json_tolerates_fences():
    assert parse_json('```json\n{"sql": "SELECT 1"}\n```') == {"sql": "SELECT 1"}
    assert parse_json('Here you go: {"refuse": "no"} thanks') == {"refuse": "no"}


def test_history_is_sent_for_follow_ups(backend):
    a = agent(backend, [{"sql": PEAK_SQL}, "ok"])
    a.ask("and in July?", history=[("peak in June?", "SELECT 1")])
    contents = [m["content"] for m in a.llm.calls[0]]
    assert "peak in June?" in contents


def test_values_match_rules():
    assert values_match(25646, 25646.0)
    assert values_match(0.2258, 22.58)          # share as percentage
    assert values_match(0.2258, 0.23)           # rounding within 2%
    assert not values_match(100.0, 110.0)
    assert values_match("July", "july")
    assert values_match(date(2026, 7, 14), pd.Timestamp("2026-07-14"))


def test_results_match_ignores_names_order_and_extra_columns():
    ref = pd.DataFrame({"d": [date(2026, 7, 14)], "peak": [25646.0]})
    got = pd.DataFrame({"peak_demand_mw": [25646], "max_temp_c": [34.3], "delivery_date": ["2026-07-14"]})
    assert results_match(ref, got)
    assert not results_match(ref, got.assign(peak_demand_mw=24000))
    assert not results_match(ref, None)


def test_results_match_equivalent_shapes():
    # q07: one row with two columns vs two rows
    ref = pd.DataFrame({"on_peak": [61.2], "off_peak": [41.67]})
    got = pd.DataFrame({"is_on_peak": [True, False], "avg_price": [61.2, 41.67]})
    assert results_match(ref, got)
    assert not results_match(ref, got.assign(avg_price=[61.2, 50.0]))
    # q10: month name vs yyyy-MM
    assert results_match(pd.DataFrame({"m": ["July"], "v": [18600.0]}),
                         pd.DataFrame({"year_month": ["2025-07"], "avg": [18600]}))
    assert not values_match("July", "2025-08")
    # q18: year as text vs number
    ref = pd.DataFrame({"year": [2025, 2026], "v": [16622.0, 16975.0]})
    got = pd.DataFrame({"period": ["2025", "2026"], "avg": [16622, 16975]})
    assert results_match(ref, got)
    assert not results_match(ref, got.assign(avg=[16975, 16622]))
    # whole-number measurements still allow rounding (95.0 g/kWh vs 95.4)
    assert values_match(95.0, 95.4) and values_match(13515207.0, 13515000)
    assert not values_match(2025, 2026) and not values_match(17, 18)


def test_run_eval_scores_correct_wrong_and_refusals(backend):
    questions = [
        {"id": "q1", "question": "peak?", "sql": PEAK_SQL},
        {"id": "q2", "question": "peak again?", "sql": PEAK_SQL},
        {"id": "r1", "question": "Quebec?", "expect": "refuse"},
    ]
    wrong = "SELECT delivery_date, peak_demand_mw FROM gold.mart_daily_summary ORDER BY peak_demand_mw ASC LIMIT 1"
    fake = [{"sql": PEAK_SQL}, "ok", {"sql": wrong}, "ok", {"refuse": "not Ontario"}]
    results = run_eval(agent(backend, fake), backend, questions)
    assert [(r.passed, r.outcome) for r in results] == [(True, "correct"), (False, "wrong"), (True, "refused")]


def test_transient_errors_are_retried_then_reported(backend):
    from chatbot.evaluate import is_transient
    assert is_transient("InternalServerError: Error code: 503 - high demand")
    assert not is_transient("The query failed: column not found")
    fake = FakeLLM([])
    calls = {"n": 0}

    def flaky(messages, json_mode=False, max_tokens=800):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Error code: 503 - This model is currently experiencing high demand")
        return Reply(json.dumps({"sql": PEAK_SQL}) if json_mode else "ok", 1, 1, 0.0)

    fake.chat = flaky
    a = Agent(fake, backend, today=date(2026, 7, 31))
    res = run_eval(a, backend, [{"id": "q1", "question": "peak?", "sql": PEAK_SQL}], retry_wait_s=0)
    assert res[0].passed and res[0].outcome == "correct"
