"""Score the agent against questions with known answers.

Each question has a reference SQL query written by hand. Both queries run on
the same data, and the agent passes when its result contains every value of
the reference result. Column names, column order and extra columns don't
matter; numbers match within 2% (so rounding differences pass), a share may be
given as 0-1 or as a percentage, a number may come back as text ('2025'), a
month as a name or as yyyy-MM ('July' = '2025-07'), and a one-row answer may
be laid out as several rows (on-peak and off-peak as two rows instead of two
columns).

Questions marked "expect": "refuse" pass when the agent refuses or the
guardrails block the query, i.e. when no data comes back.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass

import pandas as pd

from .guardrails import validate_sql
from .schema import ALLOWED_TABLES

REL_TOL = 0.02
MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], start=1)}


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and not (isinstance(v, float) and math.isnan(v))


def _norm(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    if hasattr(v, "item"):          # numpy scalar
        v = v.item()
    if isinstance(v, bool):
        return v
    if _is_number(v):
        return float(v)
    text = str(v).strip().lower()[:19]
    try:
        return float(text.replace(",", ""))       # '2025', '16,622'
    except ValueError:
        return text


def _same_month(name: str, other: str) -> bool:
    """'july' matches '2025-07' or '2025-07-01'."""
    if name not in MONTHS or len(other) < 7 or other[4] != "-":
        return False
    return other[5:7] == f"{MONTHS[name]:02d}"


def _is_int(v) -> bool:
    if hasattr(v, "item"):
        v = v.item()
    return isinstance(v, int) and not isinstance(v, bool)


def values_match(ref, got) -> bool:
    exact = _is_int(ref)            # integer-typed reference: a year, hour or count
    ref, got = _norm(ref), _norm(got)
    if ref is None:
        return True                 # a NULL in the reference is not checked
    if isinstance(ref, bool) or isinstance(got, bool):
        return ref == got
    if isinstance(ref, float) and isinstance(got, float):
        if exact:
            return ref == got       # years, hours, counts: exact (2025 is not 2026)
        for candidate in (got, got / 100, got * 100):   # 0.23 vs 23%
            if math.isclose(ref, candidate, rel_tol=REL_TOL, abs_tol=0.01):
                return True
        return False
    if isinstance(ref, str) and isinstance(got, str):
        if ref == got or (len(ref) >= 10 and ref[:10] == got[:10]):
            return True
        return _same_month(ref, got) or _same_month(got, ref)
    return str(ref) == str(got)


def results_match(ref: pd.DataFrame, got: pd.DataFrame | None) -> bool:
    """True when every reference row is found (as a set of values) in a distinct agent row."""
    if got is None:
        return False
    if ref.empty:
        return got.empty
    if len(got) > max(2 * len(ref), 5):
        return False                # dumping many rows is not an answer
    got_rows = [list(r) for r in got.itertuples(index=False)]
    used: set[int] = set()
    for ref_row in ref.itertuples(index=False):
        found = False
        for i, row in enumerate(got_rows):
            if i in used:
                continue
            remaining = list(row)
            ok = True
            for value in ref_row:
                hit = next((j for j, g in enumerate(remaining) if values_match(value, g)), None)
                if hit is None:
                    ok = False
                    break
                remaining.pop(hit)
            if ok:
                used.add(i)
                found = True
                break
        if not found:
            # a one-row reference may come back spread over a few rows
            return len(ref) == 1 and _values_in_cells(list(ref.iloc[0]), got)
    return True


def _values_in_cells(ref_values: list, got: pd.DataFrame) -> bool:
    cells = [v for row in got.itertuples(index=False) for v in row]
    for value in ref_values:
        hit = next((j for j, g in enumerate(cells) if values_match(value, g)), None)
        if hit is None:
            return False
        cells.pop(hit)
    return True


@dataclass
class EvalResult:
    id: str
    question: str
    passed: bool
    outcome: str        # correct | wrong | refused | blocked | error | unavailable
    seconds: float
    tokens: int
    sql: str
    answer: str
    expected: str = ""


TRANSIENT = ("503", "429", "overloaded", "high demand", "RateLimit", "InternalServerError",
             "APIConnectionError", "APITimeoutError", "Timeout")


def is_transient(error: str) -> bool:
    """Provider outages and rate limits: not the model's fault, so retried, not scored."""
    return any(t.lower() in (error or "").lower() for t in TRANSIENT)


def run_eval(agent, backend, questions: list[dict], pause_s: float = 0.0,
             retries: int = 3, retry_wait_s: float = 30.0) -> list[EvalResult]:
    results = []
    for q in questions:
        ans = agent.ask(q["question"])
        for _ in range(retries):
            if not is_transient(ans.error):
                break
            time.sleep(retry_wait_s)
            ans = agent.ask(q["question"])
        tokens = ans.prompt_tokens + ans.completion_tokens
        expected = ""
        if q.get("expect") == "refuse":
            passed = not ans.ok
            outcome = "refused" if ans.refused else "blocked" if ans.error else "answered"
        else:
            ref = backend.run(validate_sql(q["sql"], ALLOWED_TABLES))
            expected = "; ".join(", ".join(str(v) for v in row) for row in ref.head(5).itertuples(index=False))
            passed = ans.ok and results_match(ref, ans.data)
            outcome = ("correct" if passed else "wrong") if ans.ok else (
                "refused" if ans.refused else "unavailable" if is_transient(ans.error) else "error")
        results.append(EvalResult(q["id"], q["question"], passed, outcome, round(ans.seconds, 1),
                                  tokens, ans.sql, ans.text or ans.error, expected))
        if pause_s:
            time.sleep(pause_s)      # stay under free-tier rate limits
    return results
