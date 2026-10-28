"""Builds the table-schema section of the system prompt.

At cold start we introspect the REAL columns of the listings table in Aurora and
sample the most common values of the categorical columns, so the model writes
SQL against what is actually in the database (e.g. city = 'Springfield - ...').
Falls back to the columns known from the QuickSight dataset if Aurora is not
reachable.

The hints and fallback column list below ship the real-estate example dataset.
Replace them with your own if you point the agent at a different table.
"""
import logging
import time

from src.config import Config

logger = logging.getLogger(__name__)

# Human hints keyed by lower-case column name (or prefix, see _hint()).
_HINTS = {
    "id": "unique listing id - always show it to the customer as a reference",
    "url": "link to the listing - share it with the customer",
    "price": "total asking price in the configured currency (integer)",
    "area": "built area in square metres",
    "bedrooms": "number of bedrooms",
    "bathrooms": "number of bathrooms",
    "total_rooms": "total number of rooms",
    "governorate": "region / province / state text",
    "city": "city/compound-level text; ALWAYS match with LIKE '%...%'",
    "district": "neighbourhood text; match with LIKE",
    "finish": "finishing level, e.g. 'Semi Finished'",
    "purpose": "'sale' or 'rent'",
    "unit": "property type, e.g. 'Apartment', 'Villa'",
    "view": "view type, e.g. 'Garden', 'Main Street'",
    "year_built": "delivery/construction year (future years = off-plan / under construction)",
    "date": "date the listing was published (newer = fresher)",
    "details": "free text, mostly NULL",
    "address": "mostly NULL",
    "header1": "listing title text",
    "header2": "listing title text",
    "lat": "latitude (valid)",
    "lon": "longitude (valid)",
    "lat1": "scaled duplicate of latitude - do NOT use",
    "lon1": "scaled duplicate of longitude - do NOT use",
}

_FALLBACK_COLUMNS = [
    "url", "bathrooms", "bedrooms", "area", "governorate", "city", "district", "finish",
    "details", "purpose", "header1", "header2", "unit", "date", "address", "view",
    "year_built", "ID", "total_rooms", "price", "lat", "lon",
]

_CATEGORICAL = ["governorate", "city", "district", "finish", "purpose", "unit", "view"]

_cached: str | None = None
_cached_until: float | None = None  # None = keep for the container's life

def _hint(col: str) -> str:
    low = col.lower()
    if low.startswith("price_per"):
        return "price per square metre"
    if low.startswith("prediction"):
        return "internal ML price estimate - NEVER show to customers or use for filtering"
    return _HINTS.get(low, "")

def _load_live(table: str):
    from src.db import run_query

    columns = run_query(
        "SELECT COLUMN_NAME AS name, DATA_TYPE AS type FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = :schema AND TABLE_NAME = :table ORDER BY ORDINAL_POSITION",
        {"schema": Config.DB_NAME, "table": table},
    )
    if not columns:
        raise RuntimeError(f"Table `{table}` not found in database `{Config.DB_NAME}`")
    names = {c["name"].lower(): c["name"] for c in columns}

    samples = {}
    for key in _CATEGORICAL:
        col = names.get(key)
        if not col:
            continue
        limit = 40 if key in ("city", "district") else 20
        rows = run_query(
            f"SELECT `{col}` AS v, COUNT(*) AS n FROM `{table}` "
            f"WHERE `{col}` IS NOT NULL GROUP BY `{col}` ORDER BY n DESC LIMIT {limit}"
        )
        samples[col] = [str(r["v"]) for r in rows]
    return columns, samples

_FALLBACK_RETRY_SECONDS = 60  # a failed live read is retried after this; a good one is kept for the container's life

def get_schema_context() -> str:
    global _cached, _cached_until
    if _cached and (_cached_until is None or time.monotonic() < _cached_until):
        return _cached

    table = Config.table()
    live = True
    try:
        columns, samples = _load_live(table)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Live schema introspection failed (%s); using static fallback", exc)
        columns = [{"name": c, "type": "?"} for c in _FALLBACK_COLUMNS]
        samples, live = {}, False

    lines = [f"Table: `{table}` (Amazon Aurora MySQL). Columns:"]
    for c in columns:
        hint = _hint(c["name"])
        lines.append(f"- `{c['name']}` ({c['type']})" + (f": {hint}" if hint else ""))
    if samples:
        lines.append("\nMost common values (use these exact spellings in filters):")
        for col, values in samples.items():
            lines.append(f"- `{col}`: " + " | ".join(values))

    _cached = "\n".join(lines)
    _cached_until = None if live else time.monotonic() + _FALLBACK_RETRY_SECONDS
    return _cached
