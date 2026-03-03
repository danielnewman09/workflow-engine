"""Core tables: schema_info, snapshots, and file_changes."""

import sqlite3


def create_schema_info_table(conn: sqlite3.Connection, prefix: str = "") -> None:
    """Key-value store for schema metadata (e.g. version tracking).

    Columns:
        key:   Metadata key (primary key).
        value: Metadata value.
    """
    conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS {prefix}schema_info (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """)


def create_snapshots_table(conn: sqlite3.Connection, prefix: str = "") -> None:
    """One row per git commit ingested into the traceability database.

    Each snapshot captures the commit metadata and optional ticket/phase
    associations derived from the commit message prefix.

    Columns:
        sha:           Full commit hash (unique).
        date:          ISO-8601 commit date.
        author:        Commit author.
        message:       Full commit message.
        prefix:        Parsed message prefix (e.g. "feat", "fix").
        ticket_number: Associated ticket number, if any.
        phase:         Associated workflow phase, if any.

    Indexes:
        idx_snapshots_ticket: Lookup by ticket_number.
        idx_snapshots_date:   Chronological ordering.
    """
    conn.executescript(f"""
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
    """)


def create_file_changes_table(conn: sqlite3.Connection, prefix: str = "") -> None:
    """Per-commit file diff summary.

    Records what files changed in each commit and the magnitude of the
    change (insertions/deletions).  Linked to snapshots via snapshot_id.

    Columns:
        snapshot_id: FK to snapshots.id.
        file_path:   Path of the changed file.
        change_type: One of "added", "modified", "deleted", "renamed".
        insertions:  Lines added.
        deletions:   Lines removed.
        old_path:    Previous path if renamed.

    Indexes:
        idx_file_changes_snapshot: Join with snapshots.
        idx_file_changes_path:    Lookup by file path.
    """
    conn.executescript(f"""
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
    """)


def create_core_tables(conn: sqlite3.Connection, prefix: str = "") -> None:
    """Create all core tables: schema_info, snapshots, and file_changes."""
    create_schema_info_table(conn, prefix)
    create_snapshots_table(conn, prefix)
    create_file_changes_table(conn, prefix)
