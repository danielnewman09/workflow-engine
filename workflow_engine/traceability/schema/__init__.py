"""
Traceability Database Schema

Shared module that creates and manages the traceability database schema.
This database accumulates data over time (unlike codebase.db which is rebuilt).

Table definitions live in schema.tables; this module re-exports the public
API so that ``from workflow_engine.traceability.schema import create_schema``
continues to work unchanged.
"""

import sqlite3
from pathlib import Path

from workflow_engine.traceability.schema.tables_core import create_core_tables
from workflow_engine.traceability.schema.tables_coverage import create_coverage_tables
from workflow_engine.traceability.schema.tables_design_decisions import (
    create_decision_fts,
    create_decision_tables,
)
from workflow_engine.traceability.schema.tables_records import (
    create_record_fts,
    create_record_tables,
)
from workflow_engine.traceability.schema.tables_symbols import create_symbol_tables
from workflow_engine.traceability.schema.tables_tickets import (
    create_ticket_fts,
    create_ticket_tables,
)

SCHEMA_VERSION = 3


def create_schema(db_path: str | Path) -> sqlite3.Connection:
    """Create the traceability database schema.

    Creates tables if they don't exist (safe to call repeatedly).

    Args:
        db_path: Path to the SQLite database file.

    Returns:
        Open connection with row_factory set to sqlite3.Row.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    _create_tables(conn)

    # Set schema version
    conn.execute(
        "INSERT OR REPLACE INTO schema_info (key, value) VALUES ('version', ?)",
        (str(SCHEMA_VERSION),)
    )
    conn.commit()

    return conn


def ensure_schema(conn: sqlite3.Connection, schema_prefix: str = "") -> None:
    """Ensure traceability tables exist on an already-open connection.

    Used when the traceability DB is ATTACHed to another database
    (e.g., workflow.db ATTACHes traceability.db as 'trace').

    Args:
        conn: Open SQLite connection.
        schema_prefix: Schema prefix for ATTACHed databases, e.g. 'trace.'
    """
    _create_tables(conn, schema_prefix)
    conn.execute(
        f"INSERT OR REPLACE INTO {schema_prefix}schema_info (key, value) VALUES ('version', ?)",
        (str(SCHEMA_VERSION),)
    )
    conn.commit()


def _create_tables(conn: sqlite3.Connection, prefix: str = "") -> None:
    """Create all traceability tables (idempotent)."""
    create_core_tables(conn, prefix)
    create_symbol_tables(conn, prefix)
    create_decision_tables(conn, prefix)
    create_record_tables(conn, prefix)
    create_ticket_tables(conn, prefix)
    create_coverage_tables(conn, prefix)

    if not prefix:
        create_decision_fts(conn)
        create_record_fts(conn)
        create_ticket_fts(conn)


def rebuild_fts(conn: sqlite3.Connection) -> None:
    """Rebuild all FTS5 indexes."""
    conn.execute("INSERT INTO design_decisions_fts(design_decisions_fts) VALUES('rebuild')")
    try:
        conn.execute("INSERT INTO tickets_fts(tickets_fts) VALUES('rebuild')")
    except sqlite3.OperationalError:
        pass  # tickets_fts may not exist yet (schema v2 databases)
    conn.commit()


def get_schema_version(conn: sqlite3.Connection) -> int:
    """Get the current schema version."""
    try:
        row = conn.execute(
            "SELECT value FROM schema_info WHERE key = 'version'"
        ).fetchone()
        return int(row["value"]) if row else 0
    except sqlite3.OperationalError:
        return 0
