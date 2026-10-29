"""Settings read from environment variables (or a local .env file, never committed)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv is optional
    pass

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    data_backend: str = "duckdb"          # duckdb | databricks
    llm_provider: str = "gemini"          # gemini | azure | ollama | openai
    model: str = ""                       # blank = provider default
    gold_dir: Path = REPO_ROOT / "data" / "gold"
    max_rows: int = 200

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            data_backend=os.getenv("DATA_BACKEND", "duckdb").lower(),
            llm_provider=os.getenv("LLM_PROVIDER", "gemini").lower(),
            model=os.getenv("LLM_MODEL", ""),
            gold_dir=Path(os.getenv("GOLD_DIR", str(REPO_ROOT / "data" / "gold"))),
            max_rows=int(os.getenv("MAX_ROWS", "200")),
        )
