"""Streamlit chat app.   Run:  streamlit run chatbot/app.py"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatbot.agent import Agent  # noqa: E402
from chatbot.config import Settings  # noqa: E402
from chatbot.db import make_backend  # noqa: E402
from chatbot.llm import LLM  # noqa: E402

EXAMPLE_QUESTIONS = [
    "What was the highest demand day in 2026, and how hot was it?",
    "Compare on-peak and off-peak prices by month",
    "What share of generation came from gas on the 10 highest-demand days?",
    "Which hour of the day has the highest average price?",
    "How did demand on weekends compare with business days in July 2026?",
    "Did any pipeline runs fail?",
]

st.set_page_config(page_title="Ask the grid", page_icon="⚡", layout="wide")


@st.cache_resource
def load_agent() -> tuple[Agent, Settings]:
    settings = Settings.from_env()
    backend = make_backend(settings)
    llm = LLM(settings.llm_provider, settings.model)
    return Agent(llm, backend, settings.max_rows), settings


def chart(df: pd.DataFrame) -> None:
    """Draw a simple chart when the result is one label column + numeric columns."""
    if df is None or len(df) < 2 or len(df) > 100 or df.shape[1] < 2:
        return
    label, values = df.columns[0], [c for c in df.columns[1:] if pd.api.types.is_numeric_dtype(df[c])]
    if not values or pd.api.types.is_numeric_dtype(df[label]) and label != "hour_ending":
        return
    data = df.set_index(label)[values[:3]]
    if pd.api.types.is_datetime64_any_dtype(df[label]) or "date" in str(label):
        st.line_chart(data)
    else:
        st.bar_chart(data)


def show(ans, settings) -> None:
    if ans.refused:
        st.info(f"I can only answer questions about this Ontario grid data. {ans.text}")
        return
    if ans.error:
        st.error(f"I couldn't answer that: {ans.error}")
    else:
        st.markdown(ans.text)
        chart(ans.data)
    if ans.sql:
        with st.expander("SQL and result"):
            if ans.assumption:
                st.caption(f"Assumption: {ans.assumption}")
            st.code(ans.sql, language="sql")
            if ans.data is not None:
                st.dataframe(ans.data, hide_index=True)
    cost = f" · ~${ans.cost_usd:.5f}" if ans.cost_usd is not None else ""
    st.caption(f"{ans.seconds:.1f} s · {ans.prompt_tokens + ans.completion_tokens:,} tokens{cost} · "
               f"{ans.attempts} attempt(s)")


try:
    agent, settings = load_agent()
except Exception as exc:  # missing .env values or data files
    st.error(f"Setup problem: {exc}")
    st.stop()

with st.sidebar:
    st.title("⚡ Ask the grid")
    st.write("Questions about Ontario's electricity demand, supply mix, prices and weather, "
             "answered from the lakehouse's gold tables.")
    st.caption(f"Data: {settings.data_backend} · {agent.facts['min_date']} to {agent.facts['max_date']}")
    st.caption(f"Model: {agent.llm.model} ({settings.llm_provider})")
    st.caption("Read-only: queries are checked by guardrails before they run.")
    st.subheader("Try")
    for q in EXAMPLE_QUESTIONS:
        if st.button(q, use_container_width=True):
            st.session_state.pending = q
    if st.button("Clear chat", type="secondary"):
        st.session_state.messages = []
        st.session_state.history = []

st.session_state.setdefault("messages", [])
st.session_state.setdefault("history", [])

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        if msg["role"] == "user":
            st.markdown(msg["content"])
        else:
            show(msg["content"], settings)

question = st.chat_input("Ask about demand, generation, prices or weather")
question = question or st.session_state.pop("pending", None)
if question:
    st.session_state.messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)
    with st.chat_message("assistant"):
        with st.spinner("Writing and checking the query..."):
            ans = agent.ask(question, st.session_state.history)
        show(ans, settings)
    st.session_state.messages.append({"role": "assistant", "content": ans})
    if ans.ok:
        st.session_state.history.append((question, ans.sql))
