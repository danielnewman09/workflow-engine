#!/usr/bin/env python3
# Ticket: 0083_database_agent_orchestration
# Design: docs/designs/0083_database_agent_orchestration/design.md
"""
Workflow Engine Database Schema

SQLite schema for the workflow coordination database. Includes:
- tickets: mirrors ticket metadata from consuming repo
- phases: individual workflow phases as claimable work items
- human_gates: human review gate records
- dependencies: inter-ticket dependency tracking
- agents: agent registry for liveness tracking
- file_locks: file-level conflict detection
- audit_log: immutable audit trail for all state transitions

Schema version is stored in PRAGMA user_version. The migrate() function
applies schema changes incrementally and is idempotent.

Key prototype findings (P1: Atomic Claim Transaction Semantics):
- All write transactions MUST use BEGIN IMMEDIATE
- PRAGMA busy_timeout=5000 MUST be set on connection open
- WAL mode enables concurrent reads during write transactions
"""

import sqlite3
from pathlib import Path

# Current schema version — increment when adding tables or columns
SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------


def open_db(db_path: str | Path) -> sqlite3.Connection:
    """
    Open (or create) the workflow database with required PRAGMAs.

    Sets:
    - journal_mode=WAL: concurrent reads while single writer holds lock
    - busy_timeout=5000: retry on locked DB for up to 5 seconds (P1 finding)
    - foreign_keys=ON: enforce referential integrity

    All write transactions must use BEGIN IMMEDIATE to prevent OperationalError
    under concurrent agent access (validated in P1 prototype).
    """
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # P1: busy_timeout is required for graceful retry under contention
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ---------------------------------------------------------------------------
# DDL — ordered by dependency (no FK violations on fresh create)
# ---------------------------------------------------------------------------

_CREATE_TICKETS = """
CREATE TABLE IF NOT EXISTS tickets (
    id              TEXT PRIMARY KEY,           -- e.g. "0083"
    name            TEXT NOT NULL,              -- e.g. "database_agent_orchestration"
    full_name       TEXT NOT NULL,              -- e.g. "0083_database_agent_orchestration"
    priority        TEXT CHECK(priority IN ('Low', 'Medium', 'High', 'Critical')),
    complexity      TEXT CHECK(complexity IN ('Small', 'Medium', 'Large', 'XL')),
    components      TEXT,                       -- comma-separated
    languages       TEXT NOT NULL DEFAULT 'C++', -- comma-separated
    github_issue    INTEGER,
    current_status  TEXT NOT NULL,
    markdown_path   TEXT NOT NULL,              -- path to tickets/*.md
    custom_metadata TEXT,                       -- JSON blob for project-specific fields
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

_CREATE_PHASES = """
CREATE TABLE IF NOT EXISTS phases (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id       TEXT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    phase_name      TEXT NOT NULL,              -- e.g. "Design", "Implementation"
    phase_order     INTEGER NOT NULL,           -- ordering within ticket (0-based)
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN (
                            'pending', 'blocked', 'available', 'claimed',
                            'running', 'completed', 'failed', 'skipped'
                        )),
    agent_type      TEXT,                       -- which agent type can claim this (null = human gate)
    claimed_by      TEXT,                       -- agent instance ID
    claimed_at      TEXT,
    heartbeat_at    TEXT,
    started_at      TEXT,
    completed_at    TEXT,
    result_summary  TEXT,                       -- brief outcome text
    error_details   TEXT,                       -- on failure
    artifacts       TEXT,                       -- JSON array of file paths produced
    parallel_group  TEXT,                       -- non-null for phases that run in parallel
    UNIQUE(ticket_id, phase_name)
)
"""

_CREATE_HUMAN_GATES = """
CREATE TABLE IF NOT EXISTS human_gates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    phase_id        INTEGER NOT NULL REFERENCES phases(id) ON DELETE CASCADE,
    ticket_id       TEXT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    gate_type       TEXT NOT NULL,              -- e.g. "design_review", "prototype_review"
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending', 'approved', 'rejected', 'changes_requested')),
    requested_at    TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at      TEXT,
    decided_by      TEXT,                       -- human reviewer identifier
    decision_notes  TEXT,
    context         TEXT,                       -- JSON blob with context for reviewer
    UNIQUE(phase_id)
)
"""

_CREATE_DEPENDENCIES = """
CREATE TABLE IF NOT EXISTS dependencies (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    blocked_ticket_id   TEXT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    blocking_ticket_id  TEXT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    dependency_type     TEXT NOT NULL DEFAULT 'completion'
                            CHECK(dependency_type IN ('completion', 'design', 'implementation')),
    resolved            INTEGER NOT NULL DEFAULT 0 CHECK(resolved IN (0, 1)),
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at         TEXT,
    UNIQUE(blocked_ticket_id, blocking_ticket_id)
)
"""

_CREATE_AGENTS = """
CREATE TABLE IF NOT EXISTS agents (
    id              TEXT PRIMARY KEY,           -- unique agent instance ID (UUID)
    agent_type      TEXT NOT NULL,              -- e.g. "cpp-architect", "cpp-implementer"
    status          TEXT NOT NULL DEFAULT 'idle'
                        CHECK(status IN ('idle', 'working', 'stale', 'terminated')),
    current_phase_id INTEGER REFERENCES phases(id),
    registered_at   TEXT NOT NULL DEFAULT (datetime('now')),
    last_heartbeat  TEXT NOT NULL DEFAULT (datetime('now')),
    metadata        TEXT                        -- JSON blob (model, worktree, etc.)
)
"""

_CREATE_FILE_LOCKS = """
CREATE TABLE IF NOT EXISTS file_locks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path   TEXT NOT NULL,
    phase_id    INTEGER NOT NULL REFERENCES phases(id) ON DELETE CASCADE,
    agent_id    TEXT NOT NULL REFERENCES agents(id),
    acquired_at TEXT NOT NULL DEFAULT (datetime('now')),
    released_at TEXT,
    UNIQUE(file_path, phase_id)
)
"""

_CREATE_AUDIT_LOG = """
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL DEFAULT (datetime('now')),
    actor       TEXT NOT NULL,                  -- agent ID, "human:{name}", or "scheduler"
    action      TEXT NOT NULL,                  -- e.g. "claim_phase", "complete_phase"
    entity_type TEXT NOT NULL,                  -- "ticket", "phase", "gate", "agent"
    entity_id   TEXT NOT NULL,
    old_state   TEXT,
    new_state   TEXT,
    details     TEXT                            -- JSON blob with additional context
)
"""

# ---------------------------------------------------------------------------
# Indexes for common queries
# ---------------------------------------------------------------------------

_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_phases_status ON phases(status)",
    "CREATE INDEX IF NOT EXISTS idx_phases_agent_type ON phases(agent_type, status)",
    "CREATE INDEX IF NOT EXISTS idx_phases_ticket ON phases(ticket_id)",
    "CREATE INDEX IF NOT EXISTS idx_phases_parallel ON phases(parallel_group, ticket_id)",
    "CREATE INDEX IF NOT EXISTS idx_agents_status ON agents(status)",
    "CREATE INDEX IF NOT EXISTS idx_agents_heartbeat ON agents(last_heartbeat)",
    "CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log(entity_type, entity_id)",
    # Partial index: only active (unreleased) file locks
    "CREATE INDEX IF NOT EXISTS idx_file_locks_active ON file_locks(file_path) WHERE released_at IS NULL",
]

# All DDL in dependency order
SCHEMA_STATEMENTS: list[str] = [
    _CREATE_TICKETS,
    _CREATE_PHASES,
    _CREATE_HUMAN_GATES,
    _CREATE_DEPENDENCIES,
    _CREATE_AGENTS,
    _CREATE_FILE_LOCKS,
    _CREATE_AUDIT_LOG,
    *_INDEXES,
]


# ---------------------------------------------------------------------------
# Migration runner
# ---------------------------------------------------------------------------


def get_schema_version(conn: sqlite3.Connection) -> int:
    """Read the current schema version from PRAGMA user_version."""
    return conn.execute("PRAGMA user_version").fetchone()[0]


def set_schema_version(conn: sqlite3.Connection, version: int) -> None:
    """Write schema version to PRAGMA user_version (no param binding — use f-string)."""
    conn.execute(f"PRAGMA user_version = {version}")


def migrate(conn: sqlite3.Connection) -> None:
    """
    Apply schema migrations incrementally.

    Idempotent — safe to call on an existing database. Uses PRAGMA user_version
    to track which migrations have been applied.

    Version history:
    0 → 1: Initial schema (tickets, phases, human_gates, dependencies, agents,
            file_locks, audit_log, indexes)
    """
    current = get_schema_version(conn)

    if current < 1:
        # Version 0 → 1: Create initial schema
        for stmt in SCHEMA_STATEMENTS:
            conn.execute(stmt)
        set_schema_version(conn, 1)
        conn.commit()

    # Future migrations:
    # if current < 2:
    #     conn.execute("ALTER TABLE tickets ADD COLUMN ...")
    #     set_schema_version(conn, 2)
    #     conn.commit()


def create_db(db_path: str | Path) -> sqlite3.Connection:
    """
    Create or open a workflow database, applying all migrations.

    Returns an open connection with WAL mode, busy_timeout=5000,
    and foreign_keys=ON. The caller is responsible for closing it.
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = open_db(path)
    migrate(conn)
    return conn
