"""Ticket tables: tickets, acceptance criteria, workflow log, artifacts, files, references, and FTS."""

import sqlite3


def create_tickets_table(conn: sqlite3.Connection, prefix: str = "") -> None:
    """Core ticket content parsed from markdown ticket files.

    Each row represents a single ticket with its metadata, status,
    and categorization fields.

    Columns:
        ticket_number:     Unique ticket identifier (e.g. "0085").
        title:             Ticket title.
        canonical_phase:   Current workflow phase.
        raw_status:        Status string from the ticket file.
        priority:          Priority level.
        complexity:        Estimated complexity.
        created_date:      When the ticket was created.
        author:            Ticket author.
        summary:           Brief description of the ticket.
        ticket_type:       Type classification (default "feature").
        parent_ticket:     Parent ticket number for sub-tickets.
        target_components: Components affected by the ticket.
        languages:         Implementation languages (default "C++").
        requires_math:     Whether mathematical implementation is needed.
        generate_tutorial: Whether a tutorial should be generated.
        source_file:       Path to the markdown ticket file.
        indexed_at:        ISO-8601 timestamp of last indexing.

    Indexes:
        idx_tickets_phase:    Filter by workflow phase.
        idx_tickets_parent:   Lookup sub-tickets by parent.
        idx_tickets_priority: Filter by priority.
    """
    conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS {prefix}tickets (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_number   TEXT NOT NULL UNIQUE,
            title           TEXT NOT NULL,
            canonical_phase TEXT NOT NULL,
            raw_status      TEXT,
            priority        TEXT,
            complexity      TEXT,
            created_date    TEXT,
            author          TEXT,
            summary         TEXT,
            ticket_type     TEXT DEFAULT 'feature',
            parent_ticket   TEXT,
            target_components TEXT,
            languages       TEXT DEFAULT 'C++',
            requires_math   INTEGER DEFAULT 0,
            generate_tutorial INTEGER DEFAULT 0,
            source_file     TEXT NOT NULL,
            indexed_at      TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS {prefix}idx_tickets_phase ON {prefix}tickets(canonical_phase);
        CREATE INDEX IF NOT EXISTS {prefix}idx_tickets_parent ON {prefix}tickets(parent_ticket);
        CREATE INDEX IF NOT EXISTS {prefix}idx_tickets_priority ON {prefix}tickets(priority);
    """)


def create_ticket_acceptance_criteria_table(conn: sqlite3.Connection, prefix: str = "") -> None:
    """Acceptance criteria extracted from ticket files.

    Each criterion describes a testable condition that must be met
    for the ticket to be considered complete.

    Columns:
        ticket_number: FK to tickets.ticket_number.
        criterion_id:  Identifier within the ticket (e.g. "AC-1").
        description:   Full text of the criterion.
        is_met:        Whether the criterion has been satisfied (0/1).
        category:      Optional grouping category.

    Indexes:
        idx_tac_ticket: Lookup by ticket.
    """
    conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS {prefix}ticket_acceptance_criteria (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_number   TEXT NOT NULL REFERENCES {prefix}tickets(ticket_number),
            criterion_id    TEXT,
            description     TEXT NOT NULL,
            is_met          INTEGER NOT NULL DEFAULT 0,
            category        TEXT
        );
        CREATE INDEX IF NOT EXISTS {prefix}idx_tac_ticket ON {prefix}ticket_acceptance_criteria(ticket_number);
    """)


def create_ticket_workflow_log_table(conn: sqlite3.Connection, prefix: str = "") -> None:
    """Workflow phase completion records for a ticket.

    Tracks when each workflow phase started, completed, and its outcome.

    Columns:
        ticket_number: FK to tickets.ticket_number.
        phase_name:    Name of the workflow phase.
        started_at:    ISO-8601 start timestamp.
        completed_at:  ISO-8601 completion timestamp.
        branch:        Git branch used for this phase.
        pr_url:        Pull request URL, if applicable.
        status:        Phase outcome status.
        notes:         Free-text notes.

    Indexes:
        idx_twl_ticket: Lookup by ticket.
    """
    conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS {prefix}ticket_workflow_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_number   TEXT NOT NULL REFERENCES {prefix}tickets(ticket_number),
            phase_name      TEXT NOT NULL,
            started_at      TEXT,
            completed_at    TEXT,
            branch          TEXT,
            pr_url          TEXT,
            status          TEXT,
            notes           TEXT
        );
        CREATE INDEX IF NOT EXISTS {prefix}idx_twl_ticket ON {prefix}ticket_workflow_log(ticket_number);
    """)


def create_ticket_artifacts_table(conn: sqlite3.Connection, prefix: str = "") -> None:
    """Artifacts produced during workflow phases.

    Each artifact is a file generated or modified as part of completing
    a workflow phase (e.g. design doc, test report).

    Columns:
        workflow_log_id: FK to ticket_workflow_log.id.
        artifact_path:   Path to the artifact file.
    """
    conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS {prefix}ticket_artifacts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            workflow_log_id INTEGER NOT NULL REFERENCES {prefix}ticket_workflow_log(id),
            artifact_path   TEXT NOT NULL
        );
    """)


def create_ticket_files_table(conn: sqlite3.Connection, prefix: str = "") -> None:
    """Files declared in ticket New/Modified/Removed sections.

    Records the expected file changes described in the ticket, used
    to verify that implementation matches the plan.

    Columns:
        ticket_number: FK to tickets.ticket_number.
        file_path:     Path of the declared file.
        change_type:   One of "new", "modified", "removed".
        description:   What the change entails.

    Indexes:
        idx_tf_ticket: Lookup by ticket.
        idx_tf_path:   Lookup by file path.
    """
    conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS {prefix}ticket_files (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_number   TEXT NOT NULL REFERENCES {prefix}tickets(ticket_number),
            file_path       TEXT NOT NULL,
            change_type     TEXT NOT NULL,
            description     TEXT
        );
        CREATE INDEX IF NOT EXISTS {prefix}idx_tf_ticket ON {prefix}ticket_files(ticket_number);
        CREATE INDEX IF NOT EXISTS {prefix}idx_tf_path ON {prefix}ticket_files(file_path);
    """)


def create_ticket_references_table(conn: sqlite3.Connection, prefix: str = "") -> None:
    """Cross-references between tickets, code, and documentation.

    Captures relationships like "depends on ticket X", "references
    file Y", or "see design doc Z".

    Columns:
        ticket_number: FK to tickets.ticket_number.
        ref_type:      Reference type (e.g. "depends_on", "references").
        ref_target:    Target of the reference (ticket number, file path, URL).

    Indexes:
        idx_tr_ticket: Lookup by ticket.
    """
    conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS {prefix}ticket_references (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_number   TEXT NOT NULL REFERENCES {prefix}tickets(ticket_number),
            ref_type        TEXT NOT NULL,
            ref_target      TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS {prefix}idx_tr_ticket ON {prefix}ticket_references(ticket_number);
    """)


def create_ticket_fts(conn: sqlite3.Connection) -> None:
    """FTS5 virtual table for full-text search over tickets.

    Content-synced with tickets via content= directive.
    Uses Porter stemming and Unicode tokenization.

    Does not support schema prefixes — only for standalone databases.
    """
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS tickets_fts USING fts5(
            ticket_number,
            title,
            summary,
            content=tickets,
            content_rowid=id,
            tokenize='porter unicode61'
        )
    """)


def create_ticket_tables(conn: sqlite3.Connection, prefix: str = "") -> None:
    """Create all ticket tables and child tables."""
    create_tickets_table(conn, prefix)
    create_ticket_acceptance_criteria_table(conn, prefix)
    create_ticket_workflow_log_table(conn, prefix)
    create_ticket_artifacts_table(conn, prefix)
    create_ticket_files_table(conn, prefix)
    create_ticket_references_table(conn, prefix)
