"""SQL guardrails: the model's SQL runs only if it passes every check here.

The rules are enforced on the parsed SQL tree (sqlglot), not with string
matching, so comments, casing or odd spacing cannot slip anything through:

1. exactly one statement, and it is a query (SELECT / WITH / UNION)
2. no data-changing or admin node anywhere (INSERT, UPDATE, DELETE, MERGE,
   CREATE, DROP, ALTER, SET, GRANT, ...)
3. every table is one of the allowed gold tables (or a CTE defined in the query)
4. no file-reading or system functions (read_files, read_parquet, ...)
5. the outer query returns at most `max_rows` rows (a LIMIT is added or lowered)
"""
from __future__ import annotations

import sqlglot
from sqlglot import exp

DIALECT = "databricks"   # the dialect the model is asked to write

FORBIDDEN_NODES = tuple(
    getattr(exp, name)
    for name in (
        "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter",
        "Command", "Set", "Grant", "Revoke", "TruncateTable", "Use", "Copy",
        "LoadData", "Pragma", "Describe", "Cache", "Uncache", "Refresh", "Transaction",
    )
    if hasattr(exp, name)
)

FORBIDDEN_FUNCTIONS = {
    "read_files", "read_csv", "read_csv_auto", "read_parquet", "read_json", "read_json_auto",
    "read_text", "read_blob", "glob", "parquet_scan", "csv_scan", "json_scan",
    "current_user", "session_user", "reflect", "java_method", "sql_keywords", "query_table",
    "http_request", "secret", "ai_query",
}


class UnsafeSQL(ValueError):
    """Raised when the SQL breaks a guardrail; the message says which one."""


def validate_sql(sql: str, allowed_tables: frozenset[str], max_rows: int = 200) -> str:
    """Return a safe, normalised version of `sql`, or raise UnsafeSQL."""
    sql = (sql or "").strip().rstrip(";").strip()
    if not sql:
        raise UnsafeSQL("empty query")

    try:
        statements = [s for s in sqlglot.parse(sql, read=DIALECT) if s is not None]
    except sqlglot.errors.ParseError as exc:
        raise UnsafeSQL(f"could not parse SQL: {str(exc).splitlines()[0]}") from exc
    if len(statements) != 1:
        raise UnsafeSQL("only one statement is allowed")
    tree = statements[0]

    if not isinstance(tree, exp.Query):
        raise UnsafeSQL(f"only SELECT queries are allowed (got {tree.key.upper()})")

    for node in tree.walk():
        if isinstance(node, FORBIDDEN_NODES):
            raise UnsafeSQL(f"{node.key.upper()} is not allowed")

    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        name = table.name.lower()
        if not name:  # a table-valued function, e.g. FROM read_files(...)
            raise UnsafeSQL("table functions are not allowed")
        if name in cte_names and not table.db:
            continue
        if name not in allowed_tables:
            raise UnsafeSQL(f"table '{table.sql(dialect=DIALECT)}' is not allowed")
        if table.db and table.db.lower() != "gold":
            raise UnsafeSQL(f"only the gold schema is allowed (got '{table.db}')")
        if table.catalog and table.catalog.lower() != "ontgrid":
            raise UnsafeSQL(f"only the ontgrid catalog is allowed (got '{table.catalog}')")
        # normalise to gold.<table>; the backend decides the catalog
        table.set("catalog", None)
        table.set("db", exp.to_identifier("gold"))

    for func in tree.find_all(exp.Func):
        fname = (func.name if isinstance(func, exp.Anonymous) else func.sql_name()).lower()
        if fname in FORBIDDEN_FUNCTIONS:
            raise UnsafeSQL(f"function {fname}() is not allowed")

    limit = tree.args.get("limit")
    current = None
    if limit is not None:
        try:
            current = int(limit.expression.this)
        except (AttributeError, TypeError, ValueError):
            current = None
    if current is None or current > max_rows:
        tree = tree.limit(max_rows, copy=False)

    return tree.sql(dialect=DIALECT, pretty=True)
