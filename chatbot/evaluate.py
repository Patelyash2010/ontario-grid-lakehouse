"""Score the agent against questions with known answers.

Each question has a reference SQL query written by hand. Both queries run on
the same data, and the agent passes when its result contains every value of
the reference result. Column names, column order and extra columns don't
matter; numbers match within 2% (so rounding differences pass), and a share
may be given as 0-1 or as a percentage.

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
    return str(v).strip().lower()[:19]


def values_match(ref, got) -> bool:
    ref, got = _norm(ref), _norm(got)
    if ref is None:
        return True                 # a NULL in the reference is not checked
    if isinstance(ref, bool) or isinstance(got, bool):
        return ref == got
    if isinstance(ref, float) and isinstance(got, float):
        for candidate in (got, got / 100, got * 100):   # 0.23 vs 23%
            if math.isclose(ref, candidate, rel_tol=REL_TOL, abs_tol=0.01):
                return True
        return False
    if isinstance(ref, str) and isinstance(got, str):
        return ref == got or ref[:10] == got[:10] and len(ref) >= 10
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
            return False
    return True


@dataclass
class EvalResult:
    id: str
    question: str
    passed: bool
    outcome: str        # correct | wrong | refused | blocked | error
    seconds: float
    tokens: int
    sql: str
    answer: str


def run_eval(agent, backend, questions: list[dict], pause_s: float = 0.0) -> list[EvalResult]:
    results = []
    for q in questions:
        ans = agent.ask(q["question"])
        tokens = ans.prompt_tokens + ans.completion_tokens
        if q.get("expect") == "refuse":
            passed = not ans.ok
            outcome = "refused" if ans.refused else "blocked" if ans.error else "answered"
        else:
            ref = backend.run(validate_sql(q["sql"], ALLOWED_TABLES))
            passed = ans.ok and results_match(ref, ans.data)
            outcome = ("correct" if passed else "wrong") if ans.ok else ("refused" if ans.refused else "error")
        results.append(EvalResult(q["id"], q["question"], passed, outcome, round(ans.seconds, 1),
                                  tokens, ans.sql, ans.text or ans.error))
        if pause_s:
            time.sleep(pause_s)      # stay under free-tier rate limits
    return results
