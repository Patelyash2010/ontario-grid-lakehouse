"""One small wrapper over OpenAI-compatible model providers.

    gemini  Google Gemini API, free tier (key from aistudio.google.com)
    azure   Azure OpenAI / Azure AI Foundry deployment
    ollama  a model running locally in Ollama (no internet, no cost)
    openai  any other OpenAI-compatible endpoint (OPENAI_BASE_URL + OPENAI_API_KEY)

The agent only calls `LLM.chat(...)`, so switching provider is one setting.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

DEFAULT_MODELS = {
    "gemini": "gemini-2.5-flash",
    "azure": "gpt-4.1-mini",          # the deployment name in Azure
    "ollama": "qwen2.5-coder:3b",
    "openai": "gpt-4.1-mini",
}

# USD per 1M tokens (input, output), for the cost estimate shown in the app
PRICES = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
}
FREE_PROVIDERS = {"gemini", "ollama"}


@dataclass
class Reply:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0


class LLM:
    def __init__(self, provider: str, model: str = ""):
        import openai

        self.provider = provider
        self.model = model or DEFAULT_MODELS.get(provider, "")
        self._json_supported = True
        common = {"max_retries": 5, "timeout": 90}
        if provider == "gemini":
            self.client = openai.OpenAI(
                base_url=os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/"),
                api_key=os.environ["GEMINI_API_KEY"].strip(),
                **common,
            )
        elif provider == "azure":
            self.client = openai.AzureOpenAI(
                azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"].strip(),
                api_key=os.environ["AZURE_OPENAI_API_KEY"].strip(),
                api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"),
                **common,
            )
        elif provider == "ollama":
            self.client = openai.OpenAI(
                base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
                api_key="ollama",
                **common,
            )
        elif provider == "openai":
            self.client = openai.OpenAI(
                base_url=os.getenv("OPENAI_BASE_URL") or None,
                api_key=os.environ["OPENAI_API_KEY"].strip(),
                **common,
            )
        else:
            raise ValueError(f"unknown LLM_PROVIDER: {provider} (use gemini, azure, ollama or openai)")

    def chat(self, messages: list[dict], json_mode: bool = False, max_tokens: int = 4000) -> Reply:
        # generous max_tokens: "thinking" models (Gemini 2.5+) spend part of it on reasoning
        import openai

        start = time.perf_counter()
        kwargs = {"response_format": {"type": "json_object"}} if json_mode and self._json_supported else {}
        try:
            resp = self.client.chat.completions.create(
                model=self.model, messages=messages, temperature=0, max_tokens=max_tokens, **kwargs
            )
        except openai.BadRequestError:
            if not kwargs:
                raise
            # some endpoints reject JSON mode; the agent's parser handles plain text too
            self._json_supported = False
            resp = self.client.chat.completions.create(
                model=self.model, messages=messages, temperature=0, max_tokens=max_tokens
            )
        if isinstance(resp, str):
            # the endpoint answered with text instead of JSON: show what it said
            raise RuntimeError(f"{self.provider} returned a non-JSON reply: {resp.strip()[:300]}")
        usage = resp.usage
        return Reply(
            text=resp.choices[0].message.content or "",
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            seconds=time.perf_counter() - start,
        )


def estimate_cost_usd(model: str, prompt_tokens: int, completion_tokens: int,
                      provider: str = "") -> float | None:
    if provider in FREE_PROVIDERS:
        return 0.0
    key = model.split("/")[-1]
    if key not in PRICES:
        return None
    pin, pout = PRICES[key]
    return (prompt_tokens * pin + completion_tokens * pout) / 1_000_000
