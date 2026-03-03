import sqlite3

from workflow_engine.utils.sqlite import rows_to_dicts


# ---------------------------------------------------------------------------
# rows_to_dicts
# ---------------------------------------------------------------------------


def test_rows_to_dicts_converts_rows():
    """Should convert sqlite3.Row objects to plain dicts."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE t (id INTEGER, name TEXT)")
    conn.execute("INSERT INTO t VALUES (1, 'alice')")
    conn.execute("INSERT INTO t VALUES (2, 'bob')")
    rows = conn.execute("SELECT * FROM t ORDER BY id").fetchall()
    result = rows_to_dicts(rows)
    assert result == [{"id": 1, "name": "alice"}, {"id": 2, "name": "bob"}]
    conn.close()


def test_rows_to_dicts_empty():
    """Should return an empty list for no rows."""
    assert rows_to_dicts([]) == []
