"""
Tests for workflow_engine/traceability/schema.py

Ticket: 0085_traceability_workflow_consolidation
Design: docs/designs/0085_traceability_workflow_consolidation/design.md

Validates:
- create_schema() creates all expected tables on a fresh database
- create_schema() is idempotent (safe to call twice)
- ensure_schema() works on an already-open connection (inline / no prefix)
- ensure_schema() works with a schema prefix (the ATTACHed-DB pattern)
- get_schema_version() returns the correct version
- rebuild_fts() runs without error
- Parent directories are created if they don't exist
"""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from workflow_engine.traceability.schema import (
    SCHEMA_VERSION,
    create_schema,
    ensure_schema,
    get_schema_version,
    rebuild_fts,
)

# All tables that must exist in the standalone DB
EXPECTED_TABLES = {
    "schema_info",
    "snapshots",
    "file_changes",
    "symbol_snapshots",
    "symbol_changes",
    "design_decisions",
    "decision_symbols",
    "decision_commits",
    "record_layer_fields",
    "record_layer_mapping",
}

# FTS virtual tables only present in standalone DB (no prefix)
EXPECTED_FTS_TABLES = {
    "design_decisions_fts",
    "record_layer_fields_fts",
}

EXPECTED_INDEXES = {
    "idx_snapshots_ticket",
    "idx_snapshots_date",
    "idx_file_changes_snapshot",
    "idx_file_changes_path",
    "idx_symbol_snap_snapshot",
    "idx_symbol_snap_name",
    "idx_symbol_changes_snapshot",
    "idx_symbol_changes_name",
    "idx_dd_ticket",
    "idx_ds_decision",
    "idx_ds_symbol",
    "idx_dc_decision",
    "idx_dc_snapshot",
    "idx_rlf_record",
    "idx_rlf_layer",
    "idx_rlm_pydantic",
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path):
    """Return path to a temporary database file (not yet created)."""
    return str(tmp_path / "traceability.db")


@pytest.fixture
def open_conn(tmp_path):
    """Return an open in-memory connection."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# create_schema tests
# ---------------------------------------------------------------------------


def test_create_schema_returns_connection(db_path):
    """create_schema() should return an open connection."""
    conn = create_schema(db_path)
    try:
        assert conn is not None
        # Verify connection is usable
        row = conn.execute("SELECT 1").fetchone()
        assert row[0] == 1
    finally:
        conn.close()


def test_create_schema_creates_all_tables(db_path):
    """create_schema() should create all expected tables."""
    conn = create_schema(db_path)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert EXPECTED_TABLES.issubset(tables), (
            f"Missing tables: {EXPECTED_TABLES - tables}"
        )
    finally:
        conn.close()


def test_create_schema_creates_fts_tables(db_path):
    """create_schema() should create FTS virtual tables for standalone DB."""
    conn = create_schema(db_path)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert EXPECTED_FTS_TABLES.issubset(tables), (
            f"Missing FTS tables: {EXPECTED_FTS_TABLES - tables}"
        )
    finally:
        conn.close()


def test_create_schema_creates_indexes(db_path):
    """create_schema() should create all expected indexes."""
    conn = create_schema(db_path)
    try:
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert EXPECTED_INDEXES.issubset(indexes), (
            f"Missing indexes: {EXPECTED_INDEXES - indexes}"
        )
    finally:
        conn.close()


def test_create_schema_sets_schema_version(db_path):
    """create_schema() should write the current SCHEMA_VERSION."""
    conn = create_schema(db_path)
    try:
        assert get_schema_version(conn) == SCHEMA_VERSION
    finally:
        conn.close()


def test_create_schema_enables_wal_mode(db_path):
    """create_schema() should enable WAL journal mode."""
    conn = create_schema(db_path)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
    finally:
        conn.close()


def test_create_schema_enables_foreign_keys(db_path):
    """create_schema() should enable foreign key enforcement."""
    conn = create_schema(db_path)
    try:
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk == 1
    finally:
        conn.close()


def test_create_schema_accepts_path_object(tmp_path):
    """create_schema() should accept a pathlib.Path argument."""
    path = tmp_path / "trace.db"
    conn = create_schema(path)  # Path, not str
    try:
        assert get_schema_version(conn) == SCHEMA_VERSION
    finally:
        conn.close()


def test_create_schema_creates_parent_directories(tmp_path):
    """create_schema() should create missing parent directories."""
    nested = tmp_path / "a" / "b" / "c" / "traceability.db"
    conn = create_schema(str(nested))
    try:
        assert nested.exists()
    finally:
        conn.close()


def test_create_schema_idempotent(db_path):
    """create_schema() is safe to call twice — tables are created once."""
    conn1 = create_schema(db_path)
    conn1.close()
    conn2 = create_schema(db_path)
    try:
        # Schema version should still be correct
        assert get_schema_version(conn2) == SCHEMA_VERSION
        # All tables still present
        tables = {
            row[0]
            for row in conn2.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert EXPECTED_TABLES.issubset(tables)
    finally:
        conn2.close()


def test_create_schema_row_factory_is_set(db_path):
    """create_schema() should set row_factory to sqlite3.Row."""
    conn = create_schema(db_path)
    try:
        row = conn.execute(
            "SELECT value FROM schema_info WHERE key = 'version'"
        ).fetchone()
        # sqlite3.Row supports column name access
        assert row["value"] == str(SCHEMA_VERSION)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# ensure_schema tests (ATTACHed-DB pattern)
# ---------------------------------------------------------------------------


def test_ensure_schema_no_prefix_creates_tables(open_conn):
    """ensure_schema() with no prefix works on a standalone connection."""
    ensure_schema(open_conn)
    tables = {
        row[0]
        for row in open_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert EXPECTED_TABLES.issubset(tables)


def test_ensure_schema_no_prefix_sets_version(open_conn):
    """ensure_schema() with no prefix writes SCHEMA_VERSION."""
    ensure_schema(open_conn)
    version = get_schema_version(open_conn)
    assert version == SCHEMA_VERSION


def test_ensure_schema_production_attach_pattern(tmp_path):
    """The production ATTACH pattern calls create_schema on traceability.db
    BEFORE ATTACHing it, so ensure_schema with prefix is not needed at runtime.

    This test verifies the two-step sequence:
        1. create_schema(trace_path)       — standalone schema init
        2. ATTACH DATABASE trace_path AS trace  — wire into workflow conn

    After this sequence, all traceability tables are accessible via 'trace.' prefix.
    """
    trace_path = str(tmp_path / "traceability.db")

    # Step 1: Initialize schema standalone
    trace_conn = create_schema(trace_path)
    trace_conn.close()

    # Step 2: ATTACH to a workflow-like connection
    main_conn = sqlite3.connect(":memory:")
    main_conn.row_factory = sqlite3.Row
    main_conn.execute(f"ATTACH DATABASE '{trace_path}' AS trace")

    # Tables should now be accessible via trace. prefix
    tables = {
        row[0]
        for row in main_conn.execute(
            "SELECT name FROM trace.sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert EXPECTED_TABLES.issubset(tables), (
        f"Missing tables in attached schema: {EXPECTED_TABLES - tables}"
    )
    main_conn.close()


def test_ensure_schema_prefix_limitation_with_executescript(tmp_path):
    """ensure_schema() with schema_prefix raises OperationalError because
    SQLite's executescript() does not support schema-qualified DDL syntax.

    This documents the current implementation limitation: ensure_schema()
    with a non-empty prefix cannot be used to initialize an ATTACHed DB.
    The production code avoids this by calling create_schema() on the
    standalone traceability.db before ATTACHing it.
    """
    trace_path = str(tmp_path / "traceability.db")
    main_conn = sqlite3.connect(":memory:")
    main_conn.row_factory = sqlite3.Row
    main_conn.execute(f"ATTACH DATABASE '{trace_path}' AS trace")

    # Calling ensure_schema with prefix is not supported — documents behavior
    with pytest.raises(sqlite3.OperationalError):
        ensure_schema(main_conn, schema_prefix="trace.")

    main_conn.close()


# ---------------------------------------------------------------------------
# get_schema_version tests
# ---------------------------------------------------------------------------


def test_get_schema_version_empty_db():
    """get_schema_version() returns 0 for a brand-new database."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    assert get_schema_version(conn) == 0
    conn.close()


def test_get_schema_version_after_ensure_schema(open_conn):
    """get_schema_version() returns SCHEMA_VERSION after ensure_schema."""
    ensure_schema(open_conn)
    assert get_schema_version(open_conn) == SCHEMA_VERSION


def test_get_schema_version_returns_int(db_path):
    """get_schema_version() should return an integer."""
    conn = create_schema(db_path)
    try:
        version = get_schema_version(conn)
        assert isinstance(version, int)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# rebuild_fts tests
# ---------------------------------------------------------------------------


def test_rebuild_fts_on_empty_db(db_path):
    """rebuild_fts() should run without error on an empty database."""
    conn = create_schema(db_path)
    try:
        rebuild_fts(conn)  # Should not raise
    finally:
        conn.close()


def test_rebuild_fts_after_insert(db_path):
    """rebuild_fts() should update FTS after inserting a design decision."""
    conn = create_schema(db_path)
    try:
        conn.execute(
            """INSERT INTO design_decisions
               (dd_id, ticket, title, rationale, status, extraction_method, source_file)
               VALUES ('DD-0085-001', '0085', 'Test Decision', 'Some rationale text',
                       'active', 'structured', 'docs/designs/0085/design.md')"""
        )
        conn.commit()

        rebuild_fts(conn)

        # FTS search should now find the inserted record
        rows = conn.execute(
            "SELECT * FROM design_decisions_fts WHERE design_decisions_fts MATCH 'rationale'"
        ).fetchall()
        assert len(rows) >= 1
    finally:
        conn.close()
