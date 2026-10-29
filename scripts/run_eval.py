"""Run the evaluation set and write eval/results.md.

    python scripts/run_eval.py                 # uses .env settings
    python scripts/run_eval.py --pause 4       # slower, for free-tier rate limits
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from chatbot.agent import Agent  # noqa: E402
from chatbot.config import Settings  # noqa: E402
from chatbot.db import make_backend  # noqa: E402
from chatbot.evaluate import run_eval  # noqa: E402
from chatbot.llm import LLM  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pause", type=float, default=0.0, help="seconds between questions")
    parser.add_argument("--only", nargs="*", help="question ids to run, e.g. q01 q05")
    args = parser.parse_args()

    settings = Settings.from_env()
    backend = make_backend(settings)
    llm = LLM(settings.llm_provider, settings.model)
    agent = Agent(llm, backend, settings.max_rows)

    questions = json.loads((REPO / "eval" / "questions.json").read_text(encoding="utf-8"))
    if args.only:
        questions = [q for q in questions if q["id"] in args.only]

    results = run_eval(agent, backend, questions, pause_s=args.pause)

    unavailable = [r for r in results if r.outcome == "unavailable"]
    passed = sum(r.passed for r in results)
    answerable = [r for r in results if r.id.startswith("q")]
    refusals = [r for r in results if r.id.startswith("r")]
    avg_s = sum(r.seconds for r in results) / max(len(results), 1)
    avg_tokens = sum(r.tokens for r in results) / max(len(results), 1)

    lines = [
        "# Evaluation results",
        "",
        f"Model `{llm.model}` ({settings.llm_provider}), data `{settings.data_backend}`, run on {date.today()}.",
        "",
        f"- **Overall: {passed}/{len(results)} passed**",
        f"- Answerable questions correct: {sum(r.passed for r in answerable)}/{len(answerable)}",
        f"- Off-topic / unsafe requests refused or blocked: {sum(r.passed for r in refusals)}/{len(refusals)}",
        f"- Average {avg_s:.1f} s and {avg_tokens:,.0f} tokens per question",
        *([f"- Provider unavailable after retries (not a model error, re-run these): "
           f"{', '.join(r.id for r in unavailable)}"] if unavailable else []),
        "",
        "| id | pass | outcome | question | answer | expected |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        answer = r.answer.replace("|", "/").replace("\n", " ")
        lines.append(f"| {r.id} | {'✅' if r.passed else '❌'} | {r.outcome} | {r.question} | {answer} | {r.expected} |")
    failed = [r for r in results if not r.passed]
    if failed:
        lines += ["", "## SQL of failed questions", ""]
        for r in failed:
            lines += [f"**{r.id}** {r.question}", "", "```sql", r.sql or "-- no SQL", "```", ""]
    out = REPO / "eval" / "results.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")

    for r in results:
        print(f"{r.id}  {'PASS' if r.passed else 'FAIL'}  {r.outcome:<8} {r.seconds:>5}s  {r.question}")
    print(f"\n{passed}/{len(results)} passed. Details: {out}")


if __name__ == "__main__":
    main()
