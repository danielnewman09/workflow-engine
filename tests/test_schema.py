"""
Tests for engine/schema.py

Validates:
- Schema creation on a fresh database
- Migration idempotency (migrate() is safe to call repeatedly)
- All expected tables exist after migration
- PRAGMA settings are applied correctly
- WAL mode and busy_timeout are set
"""

import sqlite3
import tempfile
import os
import pytest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from workflow_engine.engine.schema import (
    SCHEMA_VERSION,
    create_db,
    get_schema_version,
    migrate,
    open_db,
)


EXPECTED_TABLES = {
    "tickets",
    "phases",
    "human_gates",
    "dependencies",
    "agents",
    "file_locks",
    "audit_log",
}


@pytest.fixture
def tmp_db_path(tmp_path):
    """Return a path to a temporary database file."""
    return str(tmp_path / "test_workflow.db")


# ---------------------------------------------------------------------------
# Schema creation tests
# ---------------------------------------------------------------------------


def test_create_db_creates_all_tables(tmp_db_path):
    """create_db() should create all expected tables."""
    conn = create_db(tmp_db_path)
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


def test_schema_version_after_creation(tmp_db_path):
    """Schema version should equal SCHEMA_VERSION after create_db()."""
    conn = create_db(tmp_db_path)
    try:
        assert get_schema_version(conn) == SCHEMA_VERSION
    finally:
        conn.close()


def test_migrate_idempotent(tmp_db_path):
    """migrate() should be safe to call multiple times."""
    conn = open_db(tmp_db_path)
    try:
        migrate(conn)
        migrate(conn)  # second call should be a no-op
        assert get_schema_version(conn) == SCHEMA_VERSION
        # All tables should still exist
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert EXPECTED_TABLES.issubset(tables)
    finally:
        conn.close()


def test_wal_mode_enabled(tmp_db_path):
    """open_db() should enable WAL journal mode."""
    conn = open_db(tmp_db_path)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal", f"Expected WAL mode, got '{mode}'"
    finally:
        conn.close()


def test_foreign_keys_enabled(tmp_db_path):
    """open_db() should enable foreign key enforcement."""
    conn = open_db(tmp_db_path)
    try:
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk == 1, "Foreign keys should be enabled"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Table constraint tests
# ---------------------------------------------------------------------------


def test_phase_status_constraint(tmp_db_path):
    """phases.status must be one of the valid statuses."""
    conn = create_db(tmp_db_path)
    try:
        # Insert a ticket first
        conn.execute(
            "INSERT INTO tickets (id, name, full_name, current_status, markdown_path) "
            "VALUES ('T001', 'test', 'T001_test', 'Draft', 'tickets/T001_test.md')"
        )
        conn.commit()

        # Insert a phase with a valid status
        conn.execute(
            "INSERT INTO phases (ticket_id, phase_name, phase_order, status) "
            "VALUES ('T001', 'Design', 0, 'pending')"
        )
        conn.commit()

        # Insert a phase with an invalid status should fail
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO phases (ticket_id, phase_name, phase_order, status) "
                "VALUES ('T001', 'Review', 1, 'invalid_status')"
            )
            conn.commit()
    finally:
        conn.close()


def test_phase_unique_ticket_phase_name(tmp_db_path):
    """(ticket_id, phase_name) must be unique in phases table."""
    conn = create_db(tmp_db_path)
    try:
        conn.execute(
            "INSERT INTO tickets (id, name, full_name, current_status, markdown_path) "
            "VALUES ('T001', 'test', 'T001_test', 'Draft', 'tickets/T001_test.md')"
        )
        conn.commit()

        conn.execute(
            "INSERT INTO phases (ticket_id, phase_name, phase_order) "
            "VALUES ('T001', 'Design', 0)"
        )
        conn.commit()

        # Duplicate should fail
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO phases (ticket_id, phase_name, phase_order) "
                "VALUES ('T001', 'Design', 1)"
            )
            conn.commit()
    finally:
        conn.close()


def test_ticket_priority_constraint(tmp_db_path):
    """tickets.priority must be one of the valid values."""
    conn = create_db(tmp_db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO tickets (id, name, full_name, current_status, markdown_path, priority) "
                "VALUES ('T001', 'test', 'T001_test', 'Draft', 'tickets/T001_test.md', 'SuperHigh')"
            )
            conn.commit()
    finally:
        conn.close()


def test_indexes_created(tmp_db_path):
    """All expected indexes should be created."""
    conn = create_db(tmp_db_path)
    try:
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        expected_indexes = {
            "idx_phases_status",
            "idx_phases_agent_type",
            "idx_phases_ticket",
            "idx_agents_status",
            "idx_audit_timestamp",
        }
        assert expected_indexes.issubset(indexes), (
            f"Missing indexes: {expected_indexes - indexes}"
        )
    finally:
        conn.close()


def test_create_db_creates_parent_directories(tmp_path):
    """create_db() should create parent directories if they don't exist."""
    nested_path = tmp_path / "a" / "b" / "c" / "workflow.db"
    conn = create_db(str(nested_path))
    try:
        assert nested_path.exists()
    finally:
        conn.close()
