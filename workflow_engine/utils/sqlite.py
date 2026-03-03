import sqlite3
from typing import Any


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    """Convert a list of sqlite3.Row objects to plain dicts."""
    return [dict(row) for row in rows]
