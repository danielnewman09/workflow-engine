"""
Tests for engine/claim.py

Validates:
- Atomic claiming (no double-claims)
- Priority ordering
- claim_next returns ClaimEmpty when no work available
- claim_specific returns ClaimConflict when phase already taken
- list_available filters by agent type
- Concurrent claiming from two threads produces no duplicates

The concurrent test closely mirrors the P1 prototype validation
(40/40 trials with 0 duplicates).
"""

import threading
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from workflow_engine.engine.claim import (
    ClaimConflict,
    ClaimEmpty,
    ClaimSuccess,
    claim_next,
    claim_specific,
    list_available,
    release_phase,
)
from workflow_engine.engine.schema import create_db
from workflow_engine.engine import scheduler as sched_mod


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_conn(tmp_path):
    """Open a fresh database with schema."""
    conn = create_db(str(tmp_path / "test.db"))
    yield conn
    conn.close()


def _insert_ticket(conn, ticket_id="T001", priority="High"):
    conn.execute(
        "INSERT OR IGNORE INTO tickets (id, name, full_name, current_status, markdown_path, priority, languages) "
        "VALUES (?, ?, ?, 'Draft', ?, ?, 'C++')",
        (ticket_id, f"test_{ticket_id}", f"{ticket_id}_test", f"tickets/{ticket_id}.md", priority),
    )
    conn.commit()


def _insert_phase(conn, ticket_id, phase_name, phase_order, agent_type="cpp-implementer", status="available"):
    conn.execute(
        "INSERT INTO phases (ticket_id, phase_name, phase_order, status, agent_type) "
        "VALUES (?, ?, ?, ?, ?)",
        (ticket_id, phase_name, phase_order, status, agent_type),
    )
    conn.commit()
    return conn.execute(
        "SELECT id FROM phases WHERE ticket_id=? AND phase_name=?",
        (ticket_id, phase_name),
    ).fetchone()["id"]


def _insert_agent(conn, agent_id="agent-test-001", agent_type="cpp-implementer"):
    conn.execute(
        "INSERT INTO agents (id, agent_type, status) VALUES (?, ?, 'idle')",
        (agent_id, agent_type),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Basic claim tests
# ---------------------------------------------------------------------------


def test_claim_next_basic(db_conn):
    """claim_next should return a ClaimSuccess with the available phase."""
    _insert_ticket(db_conn)
    phase_id = _insert_phase(db_conn, "T001", "Design", 0, "cpp-architect")
    _insert_agent(db_conn, "agent-001", "cpp-architect")

    result = claim_next(db_conn, "agent-001", "cpp-architect")

    assert isinstance(result, ClaimSuccess)
    assert result.phase.id == phase_id
    assert result.phase.claimed_by == "agent-001"
    assert result.phase.status == "claimed"


def test_claim_next_empty_when_none_available(db_conn):
    """claim_next returns ClaimEmpty when no phases available for this type."""
    _insert_ticket(db_conn)
    _insert_phase(db_conn, "T001", "Design", 0, "cpp-architect")  # available, but wrong type
    _insert_agent(db_conn, "agent-001", "cpp-implementer")

    result = claim_next(db_conn, "agent-001", "cpp-implementer")

    assert isinstance(result, ClaimEmpty)
    assert result.agent_type == "cpp-implementer"


def test_claim_next_respects_agent_type(db_conn):
    """claim_next should only return phases for the requesting agent type."""
    _insert_ticket(db_conn)
    _insert_phase(db_conn, "T001", "Design", 0, "cpp-architect")
    _insert_phase(db_conn, "T001", "Implementation", 1, "cpp-implementer")
    _insert_agent(db_conn, "agent-001", "cpp-implementer")

    result = claim_next(db_conn, "agent-001", "cpp-implementer")

    assert isinstance(result, ClaimSuccess)
    assert result.phase.phase_name == "Implementation"


def test_claim_next_no_double_claim(db_conn):
    """Two calls to claim_next should not claim the same phase."""
    _insert_ticket(db_conn)
    _insert_phase(db_conn, "T001", "Design", 0, "cpp-architect")
    _insert_agent(db_conn, "agent-001", "cpp-architect")
    _insert_agent(db_conn, "agent-002", "cpp-architect")

    result1 = claim_next(db_conn, "agent-001", "cpp-architect")
    result2 = claim_next(db_conn, "agent-002", "cpp-architect")

    assert isinstance(result1, ClaimSuccess)
    assert isinstance(result2, ClaimEmpty)  # only one phase available


def test_claim_specific_success(db_conn):
    """claim_specific should claim the given phase ID."""
    _insert_ticket(db_conn)
    phase_id = _insert_phase(db_conn, "T001", "Design", 0, "cpp-architect")
    _insert_agent(db_conn, "agent-001", "cpp-architect")

    result = claim_specific(db_conn, "agent-001", phase_id)

    assert isinstance(result, ClaimSuccess)
    assert result.phase.id == phase_id


def test_claim_specific_conflict(db_conn):
    """claim_specific returns ClaimConflict if phase already claimed."""
    _insert_ticket(db_conn)
    phase_id = _insert_phase(db_conn, "T001", "Design", 0, "cpp-architect")
    _insert_agent(db_conn, "agent-001", "cpp-architect")
    _insert_agent(db_conn, "agent-002", "cpp-architect")

    # First claim succeeds
    result1 = claim_specific(db_conn, "agent-001", phase_id)
    assert isinstance(result1, ClaimSuccess)

    # Second claim conflicts
    result2 = claim_specific(db_conn, "agent-002", phase_id)
    assert isinstance(result2, ClaimConflict)
    assert result2.phase_id == phase_id


# ---------------------------------------------------------------------------
# Priority ordering test
# ---------------------------------------------------------------------------


def test_claim_next_priority_ordering(db_conn):
    """claim_next should return the highest-priority ticket's phase first."""
    for ticket_id, priority in [("T001", "Low"), ("T002", "Critical"), ("T003", "Medium")]:
        _insert_ticket(db_conn, ticket_id, priority)
        _insert_phase(db_conn, ticket_id, "Design", 0, "cpp-architect")

    _insert_agent(db_conn, "agent-001", "cpp-architect")

    result = claim_next(db_conn, "agent-001", "cpp-architect")

    assert isinstance(result, ClaimSuccess)
    assert result.phase.ticket_id == "T002"  # Critical should be first


# ---------------------------------------------------------------------------
# Release test
# ---------------------------------------------------------------------------


def test_release_phase_returns_to_available(db_conn):
    """release_phase should set phase back to available."""
    _insert_ticket(db_conn)
    phase_id = _insert_phase(db_conn, "T001", "Design", 0, "cpp-architect")
    _insert_agent(db_conn, "agent-001", "cpp-architect")

    claim_next(db_conn, "agent-001", "cpp-architect")
    released = release_phase(db_conn, "agent-001", phase_id)

    assert released is True
    status = db_conn.execute("SELECT status FROM phases WHERE id=?", (phase_id,)).fetchone()["status"]
    assert status == "available"


# ---------------------------------------------------------------------------
# list_available test
# ---------------------------------------------------------------------------


def test_list_available_filters_by_agent_type(db_conn):
    """list_available should only return phases for the given agent type."""
    _insert_ticket(db_conn)
    _insert_phase(db_conn, "T001", "Design", 0, "cpp-architect")
    _insert_phase(db_conn, "T001", "Implementation", 1, "cpp-implementer")

    architect_work = list_available(db_conn, "cpp-architect")
    implementer_work = list_available(db_conn, "cpp-implementer")

    assert len(architect_work) == 1
    assert architect_work[0]["phase_name"] == "Design"
    assert len(implementer_work) == 1
    assert implementer_work[0]["phase_name"] == "Implementation"


# ---------------------------------------------------------------------------
# Concurrent claim test (mirrors P1 prototype validation)
# ---------------------------------------------------------------------------


def test_concurrent_claim_no_duplicates(tmp_path):
    """
    Two threads claiming from the same queue should never claim the same phase.

    This mirrors the P1 prototype stress test:
    - 10 phases available
    - 2 threads each trying up to 50 claims
    - Expected: exactly 10 unique claims, 0 duplicates

    Uses separate connections (one per thread) to simulate true concurrency.
    """
    import sqlite3
    db_path = str(tmp_path / "concurrent_test.db")

    # Setup
    setup_conn = create_db(db_path)
    setup_conn.execute(
        "INSERT INTO tickets (id, name, full_name, current_status, markdown_path, languages) "
        "VALUES ('T001', 'test', 'T001_test', 'Draft', 'tickets/T001_test.md', 'C++')"
    )
    for i in range(10):
        setup_conn.execute(
            "INSERT INTO phases (ticket_id, phase_name, phase_order, status, agent_type) "
            f"VALUES ('T001', 'phase_{i}', {i}, 'available', 'cpp-implementer')"
        )
    for agent_id in ("agent_A", "agent_B"):
        setup_conn.execute(
            "INSERT INTO agents (id, agent_type, status) VALUES (?, 'cpp-implementer', 'idle')",
            (agent_id,),
        )
    setup_conn.commit()
    setup_conn.close()

    results: dict[str, list[int]] = {"agent_A": [], "agent_B": []}
    errors: dict[str, list] = {"agent_A": [], "agent_B": []}
    stop_event = threading.Event()

    def claim_loop(agent_id: str):
        # Each thread opens its own connection
        conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")

        attempts = 0
        while attempts < 50 and not stop_event.is_set():
            try:
                result = claim_next(conn, agent_id, "cpp-implementer")
                if isinstance(result, ClaimSuccess):
                    results[agent_id].append(result.phase.id)
                elif isinstance(result, ClaimEmpty):
                    break
            except Exception as exc:
                errors[agent_id].append(exc)
            attempts += 1

        conn.close()

    threads = [
        threading.Thread(target=claim_loop, args=(aid,), name=aid)
        for aid in ("agent_A", "agent_B")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30.0)

    all_claimed = results["agent_A"] + results["agent_B"]
    unique_ids = set(all_claimed)

    # Exactly 10 phases should be claimed total
    assert len(all_claimed) == 10, (
        f"Expected 10 claims, got {len(all_claimed)}. "
        f"agent_A: {results['agent_A']}, agent_B: {results['agent_B']}"
    )
    # No duplicates
    assert len(unique_ids) == 10, (
        f"Duplicate phase IDs: {[x for x in all_claimed if all_claimed.count(x) > 1]}"
    )
    # No errors
    assert not errors["agent_A"], f"agent_A errors: {errors['agent_A']}"
    assert not errors["agent_B"], f"agent_B errors: {errors['agent_B']}"
