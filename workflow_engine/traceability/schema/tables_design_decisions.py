"""Design decision tables: design_decisions, decision_symbols, decision_commits, and FTS."""

import sqlite3


def create_design_decisions_table(conn: sqlite3.Connection, prefix: str = "") -> None:
    """Design decisions extracted from tickets and design documents.

    Each row captures a single design decision with its rationale,
    alternatives considered, and trade-offs.  Decisions are identified
    by a human-readable dd_id (e.g. "DD-0085-01") and linked to the
    originating ticket.

    Columns:
        dd_id:             Human-readable decision identifier (unique).
        ticket:            Originating ticket number.
        title:             Short decision title.
        rationale:         Why this decision was made.
        alternatives:      Other options that were considered.
        trade_offs:        Known trade-offs of the chosen approach.
        status:            Decision status ("active", "superseded", "deprecated").
        extraction_method: How the decision was extracted ("structured", "inferred").
        source_file:       File the decision was extracted from.
        source_line:       Line number in the source file.

    Indexes:
        idx_dd_ticket: Lookup by ticket number.
    """
    conn.executescript(f"""
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
    """)


def create_decision_symbols_table(conn: sqlite3.Connection, prefix: str = "") -> None:
    """Links design decisions to the code symbols they affect.

    Many-to-many join table connecting decisions to symbol names.
    Used by the ``why_symbol`` query to trace a symbol back to the
    design rationale that created or modified it.

    Columns:
        decision_id: FK to design_decisions.id.
        symbol_name: Qualified name of the affected symbol.

    Indexes:
        idx_ds_decision: Lookup by decision.
        idx_ds_symbol:   Reverse lookup by symbol name.
    """
    conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS {prefix}decision_symbols (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id INTEGER NOT NULL REFERENCES {prefix}design_decisions(id),
            symbol_name TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS {prefix}idx_ds_decision ON {prefix}decision_symbols(decision_id);
        CREATE INDEX IF NOT EXISTS {prefix}idx_ds_symbol   ON {prefix}decision_symbols(symbol_name);
    """)


def create_decision_commits_table(conn: sqlite3.Connection, prefix: str = "") -> None:
    """Links design decisions to the commits that implement them.

    Many-to-many join table connecting decisions to snapshots (commits).
    Used by ``get_decision`` and ``get_commit_context`` to show which
    commits relate to a given decision and vice versa.

    Columns:
        decision_id: FK to design_decisions.id.
        snapshot_id: FK to snapshots.id.

    Indexes:
        idx_dc_decision: Lookup by decision.
        idx_dc_snapshot: Reverse lookup by commit.
    """
    conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS {prefix}decision_commits (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id INTEGER NOT NULL REFERENCES {prefix}design_decisions(id),
            snapshot_id INTEGER NOT NULL REFERENCES {prefix}snapshots(id)
        );
        CREATE INDEX IF NOT EXISTS {prefix}idx_dc_decision ON {prefix}decision_commits(decision_id);
        CREATE INDEX IF NOT EXISTS {prefix}idx_dc_snapshot ON {prefix}decision_commits(snapshot_id);
    """)


def create_decision_fts(conn: sqlite3.Connection) -> None:
    """FTS5 virtual table for full-text search over design decisions.

    Content-synced with design_decisions via content= directive.
    Uses Porter stemming and Unicode tokenization for broad query matching.

    Does not support schema prefixes — only for standalone databases.
    """
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


def create_decision_tables(conn: sqlite3.Connection, prefix: str = "") -> None:
    """Create all decision tables: design_decisions, decision_symbols, and decision_commits."""
    create_design_decisions_table(conn, prefix)
    create_decision_symbols_table(conn, prefix)
    create_decision_commits_table(conn, prefix)
