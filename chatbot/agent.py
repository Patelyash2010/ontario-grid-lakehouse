"""The agent: question -> SQL -> guardrails -> run -> plain-English answer.

    1. the model writes one Databricks SQL query (or refuses an off-topic question)
    2. guardrails check it; a blocked or failing query gets ONE repair attempt
    3. the query runs on the backend (DuckDB locally, or Databricks)
    4. the model explains the result, using only numbers that are in it

Every answer carries the SQL that produced it, so it can be checked.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from .guardrails import UnsafeSQL, validate_sql
from .llm import estimate_cost_usd
from .schema import ALLOWED_TABLES, EXAMPLES, schema_context

SQL_RULES = """\
You are "Ask the grid", a data analyst for an Ontario electricity lakehouse
(IESO demand, generation by fuel, day-ahead prices, and weather for Toronto,
Ottawa and London). Turn the user's question into ONE Databricks SQL query.

Today is {today}. Hourly data covers {min_date} to {max_date}.
Prices exist only from {price_start} (IESO keeps ~90 days of price files).

Rules:
- Use only the tables below, always written as gold.<table>. Never SELECT *.
- Daily peaks, daily totals or "which day" questions: use gold.mart_daily_summary
  and filter is_complete_day (today is a partial day).
- Hours are IESO hour-ending 1-24 in Eastern Standard Time.
- For weekdays, weekends, holidays, seasons and month names, join
  gold.dim_date (fact.delivery_date = dim_date.date) and use its columns
  instead of date functions.
- Relative dates ("last week", "this year") are relative to today. Use DATE'yyyy-mm-dd' literals.
- Price questions: filter has_price (or price columns IS NOT NULL).
- Averages over a period (a month, a year, on-peak vs off-peak) are averages of
  the HOURLY values in gold.fact_grid_hourly, never averages of daily averages.
  Use mart_daily_summary only for per-day values (peaks, daily max temperature).
- Shares and intensities over a period are ratios of sums, never averages of
  the hourly ratio columns: gas share = SUM(gas_mwh) / SUM(total_generation_mwh);
  carbon intensity in g/kWh = SUM(emissions_t) / SUM(total_generation_mwh) * 1000.
- "Which month/day/hour had the highest X": group over the whole period asked
  (e.g. all 12 months of the year), order by X and return the top row.
- Comparing periods ("2025 vs 2026", "on-peak vs off-peak", "weekdays vs
  weekends"): return exactly one row (or one column) per period, nothing finer.
- Return few columns with clear aliases that include units (avg_demand_mw,
  avg_price_cad_per_mwh, gas_share). Round: MW and MWh to 0 decimals, prices to 2,
  shares to 4 (as 0-1), temperatures to 1. Aggregate so results stay small.
- If the question cannot be answered from these tables (other provinces,
  future forecasts, advice, anything not about this data), refuse.

Reply with JSON only, one of:
  {{"sql": "<query>", "assumption": "<one short line, or empty>"}}
  {{"refuse": "<one short reason>"}}

Tables:
{schema}"""

ANSWER_RULES = """\
You explain query results about Ontario's electricity grid.
Answer the user's question in 1-3 short sentences, using ONLY numbers that
appear in the result table. Include units (MW, MWh, CAD/MWh, %, C). Shares in
the data are 0-1: write them as percentages. Dates like "July 14, 2026".
Write numbers with thousands separators and no trailing ".0" (18,581 MW,
13,515,207 t). A count of 0 is a real answer: say "none" or "0", not "no data".
If the result is empty, say no matching data was found and give the likely
reason (for example, prices start on {price_start}). Do not mention SQL."""


@dataclass
class Answer:
    question: str
    sql: str = ""
    data: pd.DataFrame | None = None
    text: str = ""
    assumption: str = ""
    refused: bool = False
    error: str = ""
    attempts: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0
    cost_usd: float | None = None
    log: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.refused and not self.error and self.data is not None


def parse_json(text: str) -> dict:
    """Read the model's JSON, tolerating code fences or extra prose."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if match:
            return json.loads(match.group(0))
        raise


class Agent:
    def __init__(self, llm, backend, max_rows: int = 200, today: date | None = None):
        self.llm = llm
        self.backend = backend
        self.max_rows = max_rows
        self.today = today or date.today()
        self.facts = self._data_facts()

    def _data_facts(self) -> dict:
        row = self.backend.run(
            "SELECT MIN(delivery_date) AS min_date, MAX(delivery_date) AS max_date, "
            "MIN(CASE WHEN has_price THEN delivery_date END) AS price_start "
            "FROM gold.fact_grid_hourly"
        ).iloc[0]
        return {k: str(row[k])[:10] for k in ("min_date", "max_date", "price_start")}

    def _sql_messages(self, question: str, history: list[tuple[str, str]]) -> list[dict]:
        today = self.today.isoformat()
        system = SQL_RULES.format(today=today, schema=schema_context(), **self.facts)
        messages = [{"role": "system", "content": system}]
        for q, sql in EXAMPLES:
            messages.append({"role": "user", "content": q})
            messages.append({"role": "assistant", "content": json.dumps({"sql": sql.format(today=today), "assumption": ""})})
        for q, sql in history[-2:]:          # short memory for follow-up questions
            messages.append({"role": "user", "content": q})
            messages.append({"role": "assistant", "content": json.dumps({"sql": sql, "assumption": ""})})
        messages.append({"role": "user", "content": question})
        return messages

    def _call(self, ans: Answer, messages: list[dict], json_mode: bool):
        reply = self.llm.chat(messages, json_mode=json_mode)
        ans.prompt_tokens += reply.prompt_tokens
        ans.completion_tokens += reply.completion_tokens
        return reply.text

    def ask(self, question: str, history: list[tuple[str, str]] | None = None) -> Answer:
        start = time.perf_counter()
        ans = Answer(question=question)
        try:
            self._answer(ans, history or [])
        except Exception as exc:  # the app shows the error instead of crashing
            ans.error = ans.error or f"{type(exc).__name__}: {exc}"
        ans.seconds = time.perf_counter() - start
        ans.cost_usd = estimate_cost_usd(getattr(self.llm, "model", ""), ans.prompt_tokens, ans.completion_tokens,
                                         getattr(self.llm, "provider", ""))
        return ans

    def _answer(self, ans: Answer, history: list[tuple[str, str]]) -> None:
        messages = self._sql_messages(ans.question, history)
        for attempt in (1, 2):
            ans.attempts = attempt
            raw = self._call(ans, messages, json_mode=True)
            try:
                reply = parse_json(raw)
            except (json.JSONDecodeError, ValueError):
                problem = "Your reply was not valid JSON."
                ans.log.append(f"attempt {attempt}: invalid JSON")
            else:
                if reply.get("refuse"):
                    ans.refused = True
                    ans.text = str(reply["refuse"])
                    ans.log.append("refused")
                    return
                ans.assumption = str(reply.get("assumption") or "")
                try:
                    ans.sql = validate_sql(str(reply.get("sql", "")), ALLOWED_TABLES, self.max_rows)
                    ans.data = self.backend.run(ans.sql)
                    ans.log.append(f"attempt {attempt}: ran, {len(ans.data)} rows")
                    break
                except UnsafeSQL as exc:
                    problem = f"The query was blocked by a guardrail: {exc}."
                    ans.log.append(f"attempt {attempt}: blocked ({exc})")
                except Exception as exc:
                    problem = f"The query failed: {str(exc).splitlines()[0][:300]}"
                    ans.log.append(f"attempt {attempt}: error ({problem})")
            if attempt == 2:
                ans.error = problem
                return
            messages += [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": f"{problem} Fix it and reply with JSON only."},
            ]

        table = ans.data.head(30).to_csv(index=False)
        more = f"\n({len(ans.data)} rows in total, first 30 shown)" if len(ans.data) > 30 else ""
        ans.text = self._call(
            ans,
            [
                {"role": "system", "content": ANSWER_RULES.format(**self.facts)},
                {"role": "user", "content": f"Question: {ans.question}\n\nResult:\n{table}{more}"},
            ],
            json_mode=False,
        ).strip()
