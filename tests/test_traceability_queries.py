"""Tests for workflow_engine/traceability/table_readers/decision_queries.py

Validates that query builder functions return correct SQL and params
without needing a database connection.
"""

from workflow_engine.traceability.table_readers.decision_queries import (
    search_decisions_fts,
    search_decisions_like,
)


# ---------------------------------------------------------------------------
# search_decisions_fts
# ---------------------------------------------------------------------------


def test_fts_basic():
    """Base query with no optional filters."""
    sql, params = search_decisions_fts("convex hull")
    assert params == ["convex hull"]
    assert "MATCH ?" in sql
    assert "ORDER BY rank LIMIT 20" in sql
    assert "ticket" not in sql.split("MATCH ?")[1].split("ORDER")[0]


def test_fts_with_ticket():
    """Ticket filter appends an AND clause."""
    sql, params = search_decisions_fts("convex hull", ticket="0085")
    assert params == ["convex hull", "0085"]
    assert "dd.ticket = ?" in sql


def test_fts_with_status():
    """Status filter appends an AND clause."""
    sql, params = search_decisions_fts("convex hull", status="active")
    assert params == ["convex hull", "active"]
    assert "dd.status = ?" in sql


def test_fts_with_both_filters():
    """Both filters produce two AND clauses."""
    sql, params = search_decisions_fts("hull", ticket="0085", status="active")
    assert params == ["hull", "0085", "active"]
    assert "dd.ticket = ?" in sql
    assert "dd.status = ?" in sql


# ---------------------------------------------------------------------------
# search_decisions_like
# ---------------------------------------------------------------------------


def test_like_single_word():
    """Single word produces one LIKE group with 4 params."""
    sql, params = search_decisions_like(["convex"])
    assert len(params) == 4
    assert all(p == "%convex%" for p in params)
    assert "LIKE ?" in sql
    assert "LIMIT 20" in sql


def test_like_multiple_words():
    """Multiple words produce AND-joined LIKE groups."""
    sql, params = search_decisions_like(["convex", "hull"])
    assert len(params) == 8
    assert "AND" in sql


def test_like_with_ticket():
    """Ticket filter adds an extra param."""
    sql, params = search_decisions_like(["convex"], ticket="0085")
    assert params[-1] == "0085"
    assert "ticket = ?" in sql


def test_like_with_both_filters():
    """Both filters append correctly after the LIKE clauses."""
    sql, params = search_decisions_like(["hull"], ticket="0085", status="active")
    assert params[-2:] == ["0085", "active"]
    assert "ticket = ?" in sql
    assert "status = ?" in sql
