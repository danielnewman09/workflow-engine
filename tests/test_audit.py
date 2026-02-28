"""
Tests for engine/audit.py

Validates:
- audit.log() inserts entries with correct fields
- audit.query_audit() filters by ticket_id, entity_type, action, actor
- JSON details are serialized/deserialized correctly
- Audit entries are in reverse chronological order (newest first)
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from workflow_engine.engine.audit import log, query_audit
from workflow_engine.engine.schema import create_db


@pytest.fixture
def db_conn(tmp_path):
    conn = create_db(str(tmp_path / "test.db"))
    # Seed a ticket for FK references in phase/gate lookups
    conn.execute(
        "INSERT INTO tickets (id, name, full_name, current_status, markdown_path, languages) "
        "VALUES ('T001', 'test', 'T001_test', 'Draft', 'tickets/T001.md', 'C++')"
    )
    conn.commit()
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Insertion tests
# ---------------------------------------------------------------------------


def test_log_inserts_entry(db_conn):
    """log() should insert an audit entry."""
    db_conn.execute("BEGIN IMMEDIATE")
    log(
        db_conn,
        actor="agent-001",
        action="claim_phase",
        entity_type="phase",
        entity_id="42",
        old_state="available",
        new_state="claimed",
        details={"ticket_id": "T001"},
    )
    db_conn.commit()

    row = db_conn.execute("SELECT * FROM audit_log WHERE actor='agent-001'").fetchone()
    assert row is not None
    assert row["action"] == "claim_phase"
    assert row["entity_type"] == "phase"
    assert row["entity_id"] == "42"
    assert row["old_state"] == "available"
    assert row["new_state"] == "claimed"


def test_log_serializes_details_as_json(db_conn):
    """log() should serialize the details dict to JSON."""
    details = {"ticket_id": "T001", "phase_name": "Design", "priority": "High"}
    db_conn.execute("BEGIN IMMEDIATE")
    log(
        db_conn,
        actor="scheduler",
        action="seed_phases",
        entity_type="phase",
        entity_id="T001/Design",
        details=details,
    )
    db_conn.commit()

    row = db_conn.execute("SELECT details FROM audit_log WHERE actor='scheduler'").fetchone()
    stored = json.loads(row["details"])
    assert stored == details


def test_log_without_details(db_conn):
    """log() should work without details."""
    db_conn.execute("BEGIN IMMEDIATE")
    log(
        db_conn,
        actor="human:dan",
        action="approve_gate",
        entity_type="gate",
        entity_id="5",
    )
    db_conn.commit()

    row = db_conn.execute("SELECT * FROM audit_log WHERE actor='human:dan'").fetchone()
    assert row is not None
    assert row["details"] is None


# ---------------------------------------------------------------------------
# Query tests
# ---------------------------------------------------------------------------


def test_query_audit_returns_all_entries(db_conn):
    """query_audit() with no filters should return all entries."""
    for i in range(3):
        db_conn.execute("BEGIN IMMEDIATE")
        log(db_conn, actor=f"agent-{i}", action="register_agent", entity_type="agent", entity_id=f"agent-{i}")
        db_conn.commit()

    entries = query_audit(db_conn, limit=100)
    assert len(entries) >= 3


def test_query_audit_filters_by_actor(db_conn):
    """query_audit() should filter by actor."""
    db_conn.execute("BEGIN IMMEDIATE")
    log(db_conn, actor="agent-001", action="claim_phase", entity_type="phase", entity_id="1")
    log(db_conn, actor="scheduler", action="seed_phases", entity_type="phase", entity_id="T001/Design")
    db_conn.commit()

    entries = query_audit(db_conn, actor="agent-001")
    assert all(e["actor"] == "agent-001" for e in entries)


def test_query_audit_filters_by_action(db_conn):
    """query_audit() should filter by action."""
    db_conn.execute("BEGIN IMMEDIATE")
    log(db_conn, actor="agent-001", action="claim_phase", entity_type="phase", entity_id="1")
    log(db_conn, actor="agent-001", action="complete_phase", entity_type="phase", entity_id="1")
    db_conn.commit()

    entries = query_audit(db_conn, action="claim_phase")
    assert all(e["action"] == "claim_phase" for e in entries)


def test_query_audit_filters_by_entity_type(db_conn):
    """query_audit() should filter by entity_type."""
    db_conn.execute("BEGIN IMMEDIATE")
    log(db_conn, actor="agent-001", action="claim_phase", entity_type="phase", entity_id="1")
    log(db_conn, actor="human:dan", action="approve_gate", entity_type="gate", entity_id="5")
    db_conn.commit()

    phase_entries = query_audit(db_conn, entity_type="phase")
    assert all(e["entity_type"] == "phase" for e in phase_entries)


def test_query_audit_deserializes_details(db_conn):
    """query_audit() should deserialize details JSON into a dict."""
    db_conn.execute("BEGIN IMMEDIATE")
    log(
        db_conn,
        actor="scheduler",
        action="import_ticket",
        entity_type="ticket",
        entity_id="T001",
        details={"action": "created", "priority": "High"},
    )
    db_conn.commit()

    entries = query_audit(db_conn, entity_type="ticket", entity_id="T001")
    assert len(entries) >= 1
    entry = next(e for e in entries if e["action"] == "import_ticket")
    assert isinstance(entry["details"], dict)
    assert entry["details"]["action"] == "created"


def test_query_audit_newest_first(db_conn):
    """query_audit() should return entries in reverse chronological order."""
    for i in range(5):
        db_conn.execute("BEGIN IMMEDIATE")
        log(db_conn, actor="agent-001", action=f"action_{i}", entity_type="phase", entity_id=str(i))
        db_conn.commit()

    entries = query_audit(db_conn, limit=5)
    ids = [e["id"] for e in entries]
    assert ids == sorted(ids, reverse=True), "Entries should be newest first"


def test_query_audit_limit(db_conn):
    """query_audit() should respect the limit parameter."""
    for i in range(10):
        db_conn.execute("BEGIN IMMEDIATE")
        log(db_conn, actor="agent-001", action="action", entity_type="phase", entity_id=str(i))
        db_conn.commit()

    entries = query_audit(db_conn, limit=3)
    assert len(entries) <= 3
