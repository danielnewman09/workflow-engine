"""
Traceability Database Schema

Shared module that creates and manages the traceability database schema.
This database accumulates data over time (unlike codebase.db which is rebuilt).

Tables:
    snapshots          - One row per git commit
    file_changes       - Per-commit file diff summary
    symbol_snapshots   - Symbol locations at a commit
    symbol_changes     - Computed diffs between consecutive snapshots
    design_decisions   - Extracted design decisions from tickets/docs
    decision_symbols   - Links decisions to affected symbols
    decision_commits   - Links decisions to implementing commits
    design_decisions_fts - FTS5 index over decision text
"""

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 2


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
    conn.executescript(f"""
        -- Schema version tracking
        CREATE TABLE IF NOT EXISTS {prefix}schema_info (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        -- Git commit snapshots
        CREATE TABLE IF NOT EXISTS {prefix}snapshots (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            sha            TEXT    NOT NULL UNIQUE,
            date           TEXT    NOT NULL,
            author         TEXT    NOT NULL,
            message        TEXT    NOT NULL,
            prefix         TEXT,
            ticket_number  TEXT,
            phase          TEXT
        );
        CREATE INDEX IF NOT EXISTS {prefix}idx_snapshots_ticket ON {prefix}snapshots(ticket_number);
        CREATE INDEX IF NOT EXISTS {prefix}idx_snapshots_date   ON {prefix}snapshots(date);

        -- Per-commit file changes
        CREATE TABLE IF NOT EXISTS {prefix}file_changes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id INTEGER NOT NULL REFERENCES {prefix}snapshots(id),
            file_path   TEXT    NOT NULL,
            change_type TEXT    NOT NULL,
            insertions  INTEGER NOT NULL DEFAULT 0,
            deletions   INTEGER NOT NULL DEFAULT 0,
            old_path    TEXT
        );
        CREATE INDEX IF NOT EXISTS {prefix}idx_file_changes_snapshot ON {prefix}file_changes(snapshot_id);
        CREATE INDEX IF NOT EXISTS {prefix}idx_file_changes_path     ON {prefix}file_changes(file_path);

        -- Symbol locations at a given commit
        CREATE TABLE IF NOT EXISTS {prefix}symbol_snapshots (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id    INTEGER NOT NULL REFERENCES {prefix}snapshots(id),
            qualified_name TEXT    NOT NULL,
            kind           TEXT    NOT NULL,
            file_path      TEXT    NOT NULL,
            line_number    INTEGER NOT NULL,
            signature      TEXT,
            class_scope    TEXT
        );
        CREATE INDEX IF NOT EXISTS {prefix}idx_symbol_snap_snapshot ON {prefix}symbol_snapshots(snapshot_id);
        CREATE INDEX IF NOT EXISTS {prefix}idx_symbol_snap_name     ON {prefix}symbol_snapshots(qualified_name);

        -- Computed diffs between consecutive snapshots
        CREATE TABLE IF NOT EXISTS {prefix}symbol_changes (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id    INTEGER NOT NULL REFERENCES {prefix}snapshots(id),
            qualified_name TEXT    NOT NULL,
            change_type    TEXT    NOT NULL,
            old_file_path  TEXT,
            new_file_path  TEXT,
            old_line       INTEGER,
            new_line       INTEGER,
            old_signature  TEXT,
            new_signature  TEXT
        );
        CREATE INDEX IF NOT EXISTS {prefix}idx_symbol_changes_snapshot ON {prefix}symbol_changes(snapshot_id);
        CREATE INDEX IF NOT EXISTS {prefix}idx_symbol_changes_name     ON {prefix}symbol_changes(qualified_name);

        -- Design decisions extracted from tickets and design docs
        CREATE TABLE IF NOT EXISTS {prefix}design_decisions (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            dd_id             TEXT    NOT NULL UNIQUE,
            ticket            TEXT    NOT NULL,
            title             TEXT    NOT NULL,
            rationale         TEXT,
            alternatives      TEXT,
            trade_offs        TEXT,
            status            TEXT    NOT NULL DEFAULT 'active',
            extraction_method TEXT    NOT NULL DEFAULT 'structured',
            source_file       TEXT    NOT NULL,
            source_line       INTEGER
        );
        CREATE INDEX IF NOT EXISTS {prefix}idx_dd_ticket ON {prefix}design_decisions(ticket);

        -- Links decisions to affected symbols
        CREATE TABLE IF NOT EXISTS {prefix}decision_symbols (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id INTEGER NOT NULL REFERENCES {prefix}design_decisions(id),
            symbol_name TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS {prefix}idx_ds_decision ON {prefix}decision_symbols(decision_id);
        CREATE INDEX IF NOT EXISTS {prefix}idx_ds_symbol   ON {prefix}decision_symbols(symbol_name);

        -- Links decisions to implementing commits
        CREATE TABLE IF NOT EXISTS {prefix}decision_commits (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id INTEGER NOT NULL REFERENCES {prefix}design_decisions(id),
            snapshot_id INTEGER NOT NULL REFERENCES {prefix}snapshots(id)
        );
        CREATE INDEX IF NOT EXISTS {prefix}idx_dc_decision ON {prefix}decision_commits(decision_id);
        CREATE INDEX IF NOT EXISTS {prefix}idx_dc_snapshot ON {prefix}decision_commits(snapshot_id);

        -- Record layer field mappings
        CREATE TABLE IF NOT EXISTS {prefix}record_layer_fields (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            record_name    TEXT NOT NULL,
            layer          TEXT NOT NULL,
            field_name     TEXT NOT NULL,
            field_type     TEXT,
            source_field   TEXT,
            notes          TEXT
        );
        CREATE INDEX IF NOT EXISTS {prefix}idx_rlf_record ON {prefix}record_layer_fields(record_name);
        CREATE INDEX IF NOT EXISTS {prefix}idx_rlf_layer ON {prefix}record_layer_fields(layer);

        -- Record layer mapping (cross-layer associations)
        CREATE TABLE IF NOT EXISTS {prefix}record_layer_mapping (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            record_name     TEXT NOT NULL UNIQUE,
            pydantic_model  TEXT,
            pybind_class    TEXT,
            sql_table       TEXT
        );
        CREATE INDEX IF NOT EXISTS {prefix}idx_rlm_pydantic ON {prefix}record_layer_mapping(pydantic_model);
    """)

    # FTS5 virtual tables (no prefix support — only for standalone DB)
    if not prefix:
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS design_decisions_fts USING fts5(
                dd_id,
                title,
                rationale,
                alternatives,
                trade_offs,
                content=design_decisions,
                content_rowid=id,
                tokenize='porter unicode61'
            )
        """)

        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS record_layer_fields_fts USING fts5(
                field_name,
                field_type,
                notes,
                content=record_layer_fields,
                content_rowid=id,
                tokenize='porter unicode61'
            )
        """)


def rebuild_fts(conn: sqlite3.Connection) -> None:
    """Rebuild the FTS5 index from the design_decisions table."""
    conn.execute("INSERT INTO design_decisions_fts(design_decisions_fts) VALUES('rebuild')")
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
