"""SQL query builders for TraceabilityServer.

Each function returns a (sql, params) tuple, keeping query construction
separate from execution.  This makes queries independently testable and
self-documenting via their docstrings.
"""

from __future__ import annotations


def search_decisions_fts(
    query: str, *, ticket: str | None = None, status: str | None = None
) -> tuple[str, list]:
    """FTS5 ranked search across design decision text fields.

    Uses bm25 ranking on the design_decisions_fts virtual table.
    Results are ordered by relevance and capped at 20.

    Optional filters narrow results to a specific ticket or status.
    """
    sql = """
        SELECT dd.dd_id, dd.ticket, dd.title, dd.rationale,
               dd.status, dd.extraction_method, dd.source_file,
               bm25(design_decisions_fts) as rank
        FROM design_decisions_fts fts
        JOIN design_decisions dd ON dd.id = fts.rowid
        WHERE design_decisions_fts MATCH ?
    """
    params: list = [query]

    if ticket:
        sql += " AND dd.ticket = ?"
        params.append(ticket)
    if status:
        sql += " AND dd.status = ?"
        params.append(status)

    sql += " ORDER BY rank LIMIT 20"
    return sql, params


def search_decisions_like(
    words: list[str], *, ticket: str | None = None, status: str | None = None
) -> tuple[str, list]:
    """LIKE-based fallback search when FTS is unavailable.

    Builds a WHERE clause requiring every word to appear in at least one
    of: title, rationale, alternatives, or trade_offs.  Results capped at 20.

    Optional filters narrow results to a specific ticket or status.
    """
    word_clauses = []
    params: list = []
    for word in words:
        pattern = f"%{word}%"
        word_clauses.append(
            "(title LIKE ? OR rationale LIKE ? OR alternatives LIKE ? OR trade_offs LIKE ?)"
        )
        params.extend([pattern] * 4)

    sql = f"""
        SELECT dd_id, ticket, title, rationale, status,
               extraction_method, source_file
        FROM design_decisions
        WHERE {' AND '.join(word_clauses)}
    """

    if ticket:
        sql += " AND ticket = ?"
        params.append(ticket)
    if status:
        sql += " AND status = ?"
        params.append(status)

    sql += " LIMIT 20"
    return sql, params
