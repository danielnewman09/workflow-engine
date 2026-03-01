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
SCHEMA_VERSION = 2

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

_CREATE_IMPL_BUILD_LOG = """
CREATE TABLE IF NOT EXISTS impl_build_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    phase_id        INTEGER NOT NULL REFERENCES phases(id),
    agent_id        TEXT NOT NULL,
    ticket_id       TEXT NOT NULL,
    attempt_number  INTEGER NOT NULL,           -- 1, 2, 3, ...
    hypothesis      TEXT,                       -- what the agent intended to fix/implement
    files_changed   TEXT,                       -- JSON array of file paths modified
    build_result    TEXT NOT NULL               -- 'pass' or 'fail'
                        CHECK(build_result IN ('pass', 'fail')),
    compiler_output TEXT,                       -- truncated stdout/stderr
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
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
    # impl_build_log indexes
    "CREATE INDEX IF NOT EXISTS idx_impl_build_log_phase ON impl_build_log(phase_id)",
    "CREATE INDEX IF NOT EXISTS idx_impl_build_log_ticket ON impl_build_log(ticket_id)",
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
    _CREATE_IMPL_BUILD_LOG,
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

    if current < 2:
        # Version 1 → 2: Add impl_build_log table for TDD workflow
        conn.execute(_CREATE_IMPL_BUILD_LOG)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_impl_build_log_phase "
            "ON impl_build_log(phase_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_impl_build_log_ticket "
            "ON impl_build_log(ticket_id)"
        )
        set_schema_version(conn, 2)
        conn.commit()


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


# ---------------------------------------------------------------------------
# Implementation build log helpers
# ---------------------------------------------------------------------------


def insert_build_attempt(
    conn: sqlite3.Connection,
    phase_id: int,
    agent_id: str,
    ticket_id: str,
    hypothesis: str | None = None,
    files_changed: list[str] | None = None,
    build_result: str = "fail",
    compiler_output: str | None = None,
) -> dict:
    """
    Insert a row into impl_build_log and return attempt number + circle status.

    Circle detection: if 3+ attempts modify overlapping files with similar
    hypotheses, returns circle_detected=True.
    """
    import json as _json

    # Determine next attempt number for this phase
    row = conn.execute(
        "SELECT COALESCE(MAX(attempt_number), 0) AS max_attempt "
        "FROM impl_build_log WHERE phase_id = ?",
        (phase_id,),
    ).fetchone()
    attempt_number = row["max_attempt"] + 1

    files_json = _json.dumps(files_changed) if files_changed else None
    # Truncate compiler output to 4000 chars to keep DB manageable
    if compiler_output and len(compiler_output) > 4000:
        compiler_output = compiler_output[:4000] + "\n... (truncated)"

    conn.execute(
        "BEGIN IMMEDIATE"
    )
    conn.execute(
        """INSERT INTO impl_build_log
           (phase_id, agent_id, ticket_id, attempt_number, hypothesis,
            files_changed, build_result, compiler_output)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            phase_id, agent_id, ticket_id, attempt_number,
            hypothesis, files_json, build_result, compiler_output,
        ),
    )
    conn.commit()

    # Circle detection: check for 3+ attempts modifying similar files
    circle_detected = False
    if attempt_number >= 3:
        recent = conn.execute(
            "SELECT files_changed, hypothesis FROM impl_build_log "
            "WHERE phase_id = ? ORDER BY attempt_number DESC LIMIT 3",
            (phase_id,),
        ).fetchall()

        if len(recent) >= 3:
            # Check file overlap: if all 3 recent attempts touch the same files
            file_sets = []
            for r in recent:
                if r["files_changed"]:
                    file_sets.append(set(_json.loads(r["files_changed"])))
                else:
                    file_sets.append(set())

            if file_sets and all(len(fs) > 0 for fs in file_sets):
                common = file_sets[0]
                for fs in file_sets[1:]:
                    common = common & fs
                if len(common) > 0:
                    circle_detected = True

    return {
        "attempt_number": attempt_number,
        "circle_detected": circle_detected,
        "phase_id": phase_id,
        "ticket_id": ticket_id,
        "build_result": build_result,
    }
