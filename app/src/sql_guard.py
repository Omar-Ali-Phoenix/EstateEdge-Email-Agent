"""SQL guard rails for LLM-generated queries ("the model proposes, the code validates").

Every query that passes is rewritten into this shape:

    SELECT <columns> FROM <listings view> [WHERE ...] [GROUP BY ... [HAVING ...]]
    [ORDER BY ...] LIMIT <1..5> [OFFSET <0..500>]

Guarantees:
  * exactly ONE statement, a plain flat SELECT: no subqueries, JOIN, UNION, CTE or aliasing
  * no comments, user variables, file/OS/time-delay/random functions or system schemas
  * reads only the configured listings view/table
  * no `SELECT *`, and never the internal ML estimate or the duplicate coordinate columns
  * always sorted: a missing ORDER BY becomes cheapest-first, and `ID` is added as the last
    sort key so "show me more" (OFFSET) never repeats or skips rows
  * the row cap is applied (LIMIT 2 stays 2, LIMIT 500 becomes 5; OFFSET is kept)

This is a safety net. The real boundary is the database: the DB user can only SELECT from a
view that contains customer-safe columns (db/create_public_view.sql.tpl).
"""
import re

MAX_ROWS = 5
MAX_OFFSET = 500
MAX_SQL_CHARS = 2000
DEFAULT_SORT = "price IS NULL, price ASC"  # cheapest first, unpriced rows last
TIEBREAKER = "ID"  # unique column that makes OFFSET paging stable

class SQLGuardException(Exception):
    pass

_FORBIDDEN_WORDS = {
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE", "CREATE", "REPLACE",
    "RENAME", "GRANT", "REVOKE", "CALL", "EXEC", "EXECUTE", "PREPARE", "HANDLER",
    "LOAD", "OUTFILE", "DUMPFILE", "INTO", "LOCK", "UNLOCK", "SET", "SHOW", "DESCRIBE",
    "USE", "SLEEP", "BENCHMARK", "LOAD_FILE", "GET_LOCK", "RELEASE_LOCK",
    "INFORMATION_SCHEMA", "PERFORMANCE_SCHEMA", "MYSQL", "SYS",
    # joins and set operations
    "JOIN", "UNION", "INTERSECT", "EXCEPT", "LATERAL",
    # random order / server details
    "RAND", "RANDOM", "UUID", "VERSION", "DATABASE", "SCHEMA", "USER", "CURRENT_USER",
    "SESSION_USER", "SYSTEM_USER", "CONNECTION_ID",
}

# Columns the customer must never see or the model must never use.
_BLOCKED_COLUMNS_RE = re.compile(r"\b(?:prediction\w*|lat1|lon1)\b", re.IGNORECASE)

_STRING_RE = re.compile(r"'(?:[^'\\]|\\.|'')*'|\"(?:[^\"\\]|\\.|\"\")*\"", re.DOTALL)
_TRAILING_LIMIT_RE = re.compile(
    r"\s+LIMIT\s+(\d+)(?:\s*(,|OFFSET)\s*(\d+))?\s*$", re.IGNORECASE
)
_SHAPE_RE = re.compile(
    r"(?is)^SELECT\s+(?P<cols>.+?)\s+FROM\s+(?P<table>[\w.]+)(?P<rest>(?:\s+.*)?)$"
)
_AFTER_TABLE_RE = re.compile(r"(?is)^\s*(?:$|(?:WHERE|GROUP\s+BY|HAVING|ORDER\s+BY|LIMIT)\b)")
_AGGREGATE_RE = re.compile(r"\b(?:COUNT|SUM|AVG|MIN|MAX)\s*\(", re.IGNORECASE)

def _blank_strings(sql: str) -> str:
    """Replace string literals with '' so keyword scans ignore their content."""
    return _STRING_RE.sub("''", sql)

def sanitize_and_enforce_guardrails(raw_sql: str, allowed_table: str) -> str:
    if not raw_sql or not raw_sql.strip():
        raise SQLGuardException("Empty SQL query.")

    sql = raw_sql.strip()
    if sql.endswith(";"):
        sql = sql[:-1].rstrip()
    if len(sql) > MAX_SQL_CHARS:
        raise SQLGuardException("Query is too long. Write a shorter one.")

    scan = _blank_strings(sql).replace("`", "")

    if ";" in scan:
        raise SQLGuardException("Multiple statements are not allowed.")
    if "--" in scan or "/*" in scan or "*/" in scan or "#" in scan:
        raise SQLGuardException("SQL comments are not allowed.")
    if "@" in scan:
        raise SQLGuardException("User/system variables are not allowed.")
    if not re.match(r"(?is)^\s*SELECT\b", scan):
        raise SQLGuardException("Only SELECT queries are allowed.")

    words = set(re.findall(r"[A-Za-z_][A-Za-z_0-9]*", scan.upper()))
    bad = sorted(words & _FORBIDDEN_WORDS)
    if bad:
        raise SQLGuardException(f"Forbidden SQL keyword(s): {', '.join(bad)}")
    if re.search(r"\bFOR\s+SHARE\b", scan, re.IGNORECASE):
        raise SQLGuardException("Locking reads are not allowed.")

    # One flat SELECT ... FROM <table> only.
    if len(re.findall(r"\bSELECT\b", scan, re.IGNORECASE)) != 1:
        raise SQLGuardException("Subqueries are not allowed. Write one flat SELECT.")
    if len(re.findall(r"\bFROM\b", scan, re.IGNORECASE)) != 1:
        raise SQLGuardException("Use exactly one FROM clause on the listings table.")
    shape = _SHAPE_RE.match(scan)
    if not shape:
        raise SQLGuardException("Query must be: SELECT columns FROM the listings table ...")
    if shape.group("table").lower() != allowed_table.lower():  # also refuses other_db.<table>
        raise SQLGuardException(f"Table '{shape.group('table')}' is not allowed. Use only `{allowed_table}` with no database prefix.")
    if not _AFTER_TABLE_RE.match(shape.group("rest")):
        raise SQLGuardException("No aliases, joins or comma-separated tables. Put WHERE/ORDER BY right after the table name.")

    # Columns: no SELECT *, no hidden columns.
    cols = re.sub(r"(?i)\bCOUNT\s*\(\s*\*\s*\)", "COUNT(1)", shape.group("cols"))
    if re.search(r"(?:^|,|\.)\s*\*\s*(?:,|$)", cols):
        raise SQLGuardException("SELECT * is not allowed. List the columns you need.")
    if _BLOCKED_COLUMNS_RE.search(scan):
        raise SQLGuardException("That column is internal and not available. Remove it from the query.")

    # Row cap (keep OFFSET if the model supplied one; a smaller LIMIT is respected).
    offset, count = 0, MAX_ROWS
    m = _TRAILING_LIMIT_RE.search(sql)
    if m:
        if m.group(3) is not None:
            # `LIMIT offset, count`  or  `LIMIT count OFFSET offset`
            if m.group(2) == ",":
                offset, count = int(m.group(1)), int(m.group(3))
            else:
                count, offset = int(m.group(1)), int(m.group(3))
        else:
            count = int(m.group(1))
        sql = sql[: m.start()].rstrip()
    if re.search(r"\bLIMIT\b", _blank_strings(sql), re.IGNORECASE):
        raise SQLGuardException("LIMIT must be the last clause.")
    if offset > MAX_OFFSET:
        raise SQLGuardException(f"OFFSET cannot be larger than {MAX_OFFSET}.")
    count = min(max(count, 1), MAX_ROWS)

    # Deterministic sorting (skipped for aggregates, where the ID column is not valid).
    body = _blank_strings(sql).replace("`", "")
    aggregate = bool(
        _AGGREGATE_RE.search(body)
        or re.search(r"\bGROUP\s+BY\b", body, re.IGNORECASE)
        or re.search(r"\bSELECT\s+DISTINCT\b", body, re.IGNORECASE)
    )
    if not aggregate:
        order = re.search(r"\bORDER\s+BY\b(.*)$", body, re.IGNORECASE | re.DOTALL)
        if not order:
            sql += f" ORDER BY {DEFAULT_SORT}, {TIEBREAKER} ASC"
        elif not re.search(rf"\b{TIEBREAKER}\b", order.group(1), re.IGNORECASE):
            sql += f", {TIEBREAKER} ASC"

    sql += f" LIMIT {count}"
    if offset:
        sql += f" OFFSET {offset}"
    return sql
