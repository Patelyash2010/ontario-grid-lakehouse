# Ask the grid: local setup

A guarded text-to-SQL assistant over the gold tables.

```
question ─► LLM writes Databricks SQL ─► guardrails (sqlglot) ─► DuckDB copy of gold
                 ▲          (1 repair attempt if blocked/failed)       or Databricks warehouse
                 └──────────── LLM explains the result ◄──────────────────────┘
```

## 1. Python environment (3.10+)

```powershell
py --list                         # pick 3.12 or 3.13
py -3.13 -m venv .venv-chat
.venv-chat\Scripts\Activate.ps1
python -m pip install -r chatbot\requirements.txt
python -m pytest                  # 90 tests, no network needed
```

## 2. Secrets in `.env` (never committed)

Copy `.env.example` to `.env` and fill in:

- `GEMINI_API_KEY`: aistudio.google.com → **Get API key** → Create API key (free tier).
  (GitHub Models, the first choice, was retired on 2026-07-30.)
- `DATABRICKS_HOST`, `DATABRICKS_HTTP_PATH`: SQL Warehouses → `wh_powerbi` → Connection details.
- `DATABRICKS_TOKEN`: Databricks → your profile → Settings → Developer → Access tokens →
  Generate (90 days).

## 3. Copy gold to your laptop (once, then whenever you want fresh data)

```powershell
python scripts\export_gold.py     # writes data\gold\*.parquet, then stop wh_powerbi
```

## 4. Run

```powershell
streamlit run chatbot\app.py                 # opens http://localhost:8501
python scripts\run_eval.py --pause 8         # writes eval\results.md
```

Switch to the live warehouse or another model by editing `.env`:
`DATA_BACKEND=databricks`, `LLM_PROVIDER=azure`, `ollama` or `openai`.

## Guardrails

Enforced on the parsed SQL tree, not by string matching:

1. one statement, and it must be a query
2. no INSERT / UPDATE / DELETE / MERGE / CREATE / DROP / ALTER / SET / GRANT anywhere
3. only the 8 gold tables (`ontgrid.gold`), or CTEs defined in the query
4. no file-reading or system functions (`read_files`, `read_parquet`, `current_user`, ...)
5. at most 200 rows returned (LIMIT added or lowered)

The DuckDB backend also loads the tables into memory and then disables file
system access, so even a bypassed check could not read files.
