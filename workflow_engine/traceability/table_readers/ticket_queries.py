"""SQL query builders for ticket queries.

Each function returns a (sql, params) tuple, keeping query construction
separate from execution.  This makes queries independently testable and
self-documenting via their docstrings.
"""

from __future__ import annotations


def search_tickets_fts(
    query: str,
    *,
    phase: str | None = None,
    priority: str | None = None,
    component: str | None = None,
) -> tuple[str, list]:
    """FTS5 ranked search across ticket titles and summaries.

    Uses bm25 ranking on the tickets_fts virtual table.
    Results are ordered by relevance and capped at 20.

    Optional filters narrow results to a specific phase, priority,
    or component.
    """
    sql = """
        SELECT t.ticket_number, t.title, t.canonical_phase, t.priority,
               t.summary, t.target_components, t.source_file,
               bm25(tickets_fts) as rank
        FROM tickets_fts fts
        JOIN tickets t ON t.id = fts.rowid
        WHERE tickets_fts MATCH ?
    """
    params: list = [query]

    if phase:
        sql += " AND t.canonical_phase = ?"
        params.append(phase)
    if priority:
        sql += " AND t.priority = ?"
        params.append(priority)
    if component:
        sql += " AND t.target_components LIKE ?"
        params.append(f"%{component}%")

    sql += " ORDER BY rank LIMIT 20"
    return sql, params


def search_tickets_like(
    words: list[str],
    *,
    phase: str | None = None,
    priority: str | None = None,
    component: str | None = None,
) -> tuple[str, list]:
    """LIKE-based fallback search when FTS is unavailable.

    Builds a WHERE clause requiring every word to appear in at least one
    of: title or summary.  Results capped at 20.

    Optional filters narrow results to a specific phase, priority,
    or component.
    """
    word_clauses = []
    params: list = []
    for word in words:
        pattern = f"%{word}%"
        word_clauses.append("(title LIKE ? OR summary LIKE ?)")
        params.extend([pattern] * 2)

    sql = f"""
        SELECT ticket_number, title, canonical_phase, priority,
               summary, target_components, source_file
        FROM tickets
        WHERE {' AND '.join(word_clauses)}
    """

    if phase:
        sql += " AND canonical_phase = ?"
        params.append(phase)
    if priority:
        sql += " AND priority = ?"
        params.append(priority)
    if component:
        sql += " AND target_components LIKE ?"
        params.append(f"%{component}%")

    sql += " LIMIT 20"
    return sql, params


def list_tickets_filtered(
    *,
    phase: str | None = None,
    priority: str | None = None,
    component: str | None = None,
    limit: int = 50,
) -> tuple[str, list]:
    """List tickets filtered by phase, priority, and/or component.

    Returns up to ``limit`` tickets ordered by ticket_number.
    """
    sql = """
        SELECT ticket_number, title, canonical_phase, priority,
               complexity, target_components, ticket_type, source_file
        FROM tickets
        WHERE 1=1
    """
    params: list = []

    if phase:
        sql += " AND canonical_phase = ?"
        params.append(phase)
    if priority:
        sql += " AND priority = ?"
        params.append(priority)
    if component:
        sql += " AND target_components LIKE ?"
        params.append(f"%{component}%")

    sql += " ORDER BY ticket_number LIMIT ?"
    params.append(limit)
    return sql, params


def get_ticket_detail(ticket_number: str) -> tuple[str, list]:
    """Fetch a single ticket row by ticket_number."""
    return "SELECT * FROM tickets WHERE ticket_number = ?", [ticket_number]


def get_ticket_acceptance_criteria(ticket_number: str) -> tuple[str, list]:
    """Acceptance criteria for a ticket."""
    sql = """SELECT criterion_id, description, is_met, category
             FROM ticket_acceptance_criteria
             WHERE ticket_number = ?"""
    return sql, [ticket_number]


def get_ticket_workflow_log(ticket_number: str) -> tuple[str, list]:
    """Workflow log entries for a ticket, ordered by id."""
    sql = """SELECT id, phase_name, started_at, completed_at,
                    branch, pr_url, status, notes
             FROM ticket_workflow_log
             WHERE ticket_number = ?
             ORDER BY id"""
    return sql, [ticket_number]


def get_ticket_artifacts(workflow_log_id: int) -> tuple[str, list]:
    """Artifacts for a specific workflow log entry."""
    sql = "SELECT artifact_path FROM ticket_artifacts WHERE workflow_log_id = ?"
    return sql, [workflow_log_id]


def get_ticket_files(ticket_number: str) -> tuple[str, list]:
    """File declarations for a ticket."""
    sql = """SELECT file_path, change_type, description
             FROM ticket_files
             WHERE ticket_number = ?"""
    return sql, [ticket_number]


def get_ticket_references(ticket_number: str) -> tuple[str, list]:
    """References for a ticket."""
    sql = """SELECT ref_type, ref_target
             FROM ticket_references
             WHERE ticket_number = ?"""
    return sql, [ticket_number]
