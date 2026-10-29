"""Check the model connection configured in .env.

    python scripts/check_llm.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatbot.config import Settings  # noqa: E402  (loads .env)
from chatbot.llm import LLM  # noqa: E402

settings = Settings.from_env()
llm = LLM(settings.llm_provider, settings.model)
print(f"provider={llm.provider}  model={llm.model}")
reply = llm.chat([{"role": "user", "content": 'Reply with JSON only: {"status": "ok"}'}], json_mode=True)
print(f"reply: {reply.text.strip()}")
print(f"{reply.seconds:.1f} s, {reply.prompt_tokens} + {reply.completion_tokens} tokens")
