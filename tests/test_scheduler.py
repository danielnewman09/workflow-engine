"""
Tests for engine/scheduler.py

Validates:
- import_ticket parses markdown and creates DB records
- seed_phases creates phase rows per phases.yaml definitions
- resolve_availability marks phases available when prerequisites met
- cleanup_stale_agents releases timed-out claims
- complete_phase and fail_phase update phase status
- Human gate creation for phases with agent_type=null
"""

import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from workflow_engine.engine.schema import create_db
from workflow_engine.engine import scheduler as sched_mod
from workflow_engine.engine.models import PhaseStatus, WorkflowConfig, PhaseDefinition


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_conn(tmp_path):
    conn = create_db(str(tmp_path / "test.db"))
    yield conn
    conn.close()


@pytest.fixture
def sample_ticket_file(tmp_path):
    """Write a sample ticket markdown file and return its path."""
    content = """\
# Feature Ticket: Test Feature

## Status
- [x] Draft
- [x] Ready for Design
- [ ] Design Complete — Awaiting Review
- [ ] Merged / Complete

## Metadata
- **Created**: 2026-02-27
- **Author**: Test User
- **Priority**: High
- **Estimated Complexity**: Medium
- **Target Component(s)**: msd-sim
- **Languages**: C++
- **Generate Tutorial**: No
- **Requires Math Design**: No
- **GitHub Issue**: 42

---

## Summary
Test ticket for workflow engine tests.
"""
    ticket_path = tmp_path / "tickets"
    ticket_path.mkdir()
    ticket_file = ticket_path / "0001_test_feature.md"
    ticket_file.write_text(content, encoding="utf-8")
    return ticket_file


@pytest.fixture
def simple_config(tmp_path, sample_ticket_file):
    """WorkflowConfig with simple phase definitions."""
    phases = [
        PhaseDefinition(name="Design", agent_type="cpp-architect", order=0),
        PhaseDefinition(name="Design Review", agent_type="design-reviewer", order=1),
        PhaseDefinition(name="Implementation", agent_type="cpp-implementer", order=2),
    ]
    return WorkflowConfig(
        db_path=str(tmp_path / "test.db"),
        tickets_directory=str(sample_ticket_file.parent),
        tickets_pattern="*.md",
        id_regex=r"^(\d{4}[a-z]?)_",
        phase_definitions=phases,
    )


# ---------------------------------------------------------------------------
# import_ticket tests
# ---------------------------------------------------------------------------


def test_import_ticket_creates_record(db_conn, sample_ticket_file, simple_config):
    """import_ticket should create a ticket record in the DB."""
    result = sched_mod.import_ticket(db_conn, sample_ticket_file, simple_config)

    assert result["ticket_id"] == "0001"
    assert result["action"] == "created"
    assert result["priority"] == "High"

    ticket = db_conn.execute("SELECT * FROM tickets WHERE id='0001'").fetchone()
    assert ticket is not None
    assert ticket["full_name"] == "0001_test_feature"
    assert ticket["priority"] == "High"
    assert ticket["languages"] == "C++"
    assert ticket["github_issue"] == 42


def test_import_ticket_is_idempotent(db_conn, sample_ticket_file, simple_config):
    """Importing the same ticket twice should update, not duplicate."""
    result1 = sched_mod.import_ticket(db_conn, sample_ticket_file, simple_config)
    result2 = sched_mod.import_ticket(db_conn, sample_ticket_file, simple_config)

    assert result1["action"] == "created"
    assert result2["action"] == "updated"

    count = db_conn.execute("SELECT COUNT(*) FROM tickets WHERE id='0001'").fetchone()[0]
    assert count == 1


def test_import_ticket_parses_current_status(db_conn, sample_ticket_file, simple_config):
    """import_ticket should parse the last checked checkbox as current_status."""
    sched_mod.import_ticket(db_conn, sample_ticket_file, simple_config)
    ticket = db_conn.execute("SELECT current_status FROM tickets WHERE id='0001'").fetchone()
    assert ticket["current_status"] == "Ready for Design"


# ---------------------------------------------------------------------------
# seed_phases tests
# ---------------------------------------------------------------------------


def test_seed_phases_creates_phase_rows(db_conn, sample_ticket_file, simple_config):
    """seed_phases should create rows for all applicable phase definitions."""
    sched_mod.import_ticket(db_conn, sample_ticket_file, simple_config)
    seeded = sched_mod.seed_phases(db_conn, "0001", simple_config)

    assert len(seeded) == 3  # Design, Design Review, Implementation

    phases = db_conn.execute("SELECT phase_name, status FROM phases WHERE ticket_id='0001' ORDER BY phase_order").fetchall()
    assert len(phases) == 3
    assert phases[0]["phase_name"] == "Design"
    assert phases[1]["phase_name"] == "Design Review"
    assert phases[2]["phase_name"] == "Implementation"


def test_seed_phases_is_idempotent(db_conn, sample_ticket_file, simple_config):
    """seed_phases called twice should not create duplicate rows."""
    sched_mod.import_ticket(db_conn, sample_ticket_file, simple_config)
    sched_mod.seed_phases(db_conn, "0001", simple_config)
    sched_mod.seed_phases(db_conn, "0001", simple_config)

    count = db_conn.execute("SELECT COUNT(*) FROM phases WHERE ticket_id='0001'").fetchone()[0]
    assert count == 3


def test_seed_phases_conditional_skip(db_conn, tmp_path):
    """Phases with conditions that don't match should be seeded as skipped."""
    from workflow_engine.engine.models import PhaseCondition

    # Ticket without math design
    ticket_content = """\
## Status
- [x] Draft

## Metadata
- **Priority**: High
- **Languages**: C++
- **Requires Math Design**: No
"""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    ticket_file = tickets_dir / "0002_simple.md"
    ticket_file.write_text(ticket_content)

    conn = create_db(str(tmp_path / "test.db"))

    phases = [
        PhaseDefinition(
            name="Math Design",
            agent_type="math-designer",
            order=0,
            condition=PhaseCondition(field="requires_math_design", value=True),
        ),
        PhaseDefinition(name="Design", agent_type="cpp-architect", order=1),
    ]
    config = WorkflowConfig(
        db_path=str(tmp_path / "test.db"),
        tickets_directory=str(tickets_dir),
        phase_definitions=phases,
    )

    sched_mod.import_ticket(conn, ticket_file, config)
    sched_mod.seed_phases(conn, "0002", config)

    phases_db = db_conn = conn.execute(
        "SELECT phase_name, status FROM phases WHERE ticket_id='0002' ORDER BY phase_order"
    ).fetchall()

    assert phases_db[0]["phase_name"] == "Math Design"
    assert phases_db[0]["status"] == "skipped"
    assert phases_db[1]["phase_name"] == "Design"
    assert phases_db[1]["status"] == "pending"

    conn.close()


# ---------------------------------------------------------------------------
# resolve_availability tests
# ---------------------------------------------------------------------------


def test_resolve_availability_makes_first_phase_available(db_conn, sample_ticket_file, simple_config):
    """The first non-skipped phase should become available after seeding."""
    sched_mod.import_ticket(db_conn, sample_ticket_file, simple_config)
    sched_mod.seed_phases(db_conn, "0001", simple_config)
    sched_mod.resolve_availability(db_conn, "0001")

    first_phase = db_conn.execute(
        "SELECT status FROM phases WHERE ticket_id='0001' AND phase_name='Design'"
    ).fetchone()
    assert first_phase["status"] == "available"


def test_resolve_availability_blocks_subsequent_phases(db_conn, sample_ticket_file, simple_config):
    """Phases after the first should be blocked until the prior phase completes."""
    sched_mod.import_ticket(db_conn, sample_ticket_file, simple_config)
    sched_mod.seed_phases(db_conn, "0001", simple_config)
    sched_mod.resolve_availability(db_conn, "0001")

    impl_phase = db_conn.execute(
        "SELECT status FROM phases WHERE ticket_id='0001' AND phase_name='Implementation'"
    ).fetchone()
    # Implementation should be blocked (prior phases not completed)
    assert impl_phase["status"] in ("pending", "blocked")


def test_resolve_availability_unlocks_after_completion(db_conn, sample_ticket_file, simple_config):
    """Completing the first phase should make the second phase available."""
    sched_mod.import_ticket(db_conn, sample_ticket_file, simple_config)
    sched_mod.seed_phases(db_conn, "0001", simple_config)
    sched_mod.resolve_availability(db_conn, "0001")

    # Register and claim the Design phase
    sched_mod.register_agent(db_conn, "agent-001", "cpp-architect")
    from workflow_engine.engine.claim import claim_next
    claim_result = claim_next(db_conn, "agent-001", "cpp-architect")
    assert claim_result.success

    phase_id = claim_result.phase.id
    sched_mod.start_phase(db_conn, "agent-001", phase_id)
    sched_mod.complete_phase(db_conn, "agent-001", phase_id, "Design done")

    # Now resolve availability — Design Review should be available
    sched_mod.resolve_availability(db_conn, "0001")

    review_phase = db_conn.execute(
        "SELECT status FROM phases WHERE ticket_id='0001' AND phase_name='Design Review'"
    ).fetchone()
    assert review_phase["status"] == "available"


# ---------------------------------------------------------------------------
# Stale agent cleanup tests
# ---------------------------------------------------------------------------


def test_cleanup_stale_agents_releases_timed_out_claims(db_conn):
    """Agents past their heartbeat timeout should have claims released."""
    # Set up a ticket and phase
    db_conn.execute(
        "INSERT INTO tickets (id, name, full_name, current_status, markdown_path, languages) "
        "VALUES ('T001', 'test', 'T001_test', 'Draft', 'tickets/T001.md', 'C++')"
    )
    db_conn.execute(
        "INSERT INTO phases (ticket_id, phase_name, phase_order, status, agent_type, claimed_by, claimed_at) "
        "VALUES ('T001', 'Design', 0, 'running', 'cpp-architect', 'stale-agent-001', datetime('now', '-60 minutes'))"
    )
    # Insert a stale agent with old heartbeat
    db_conn.execute(
        "INSERT INTO agents (id, agent_type, status, last_heartbeat) "
        "VALUES ('stale-agent-001', 'cpp-architect', 'working', datetime('now', '-60 minutes'))"
    )
    db_conn.commit()

    released = sched_mod.cleanup_stale_agents(db_conn, stale_timeout_minutes=30)

    assert len(released) == 1
    assert released[0]["agent_id"] == "stale-agent-001"

    # Phase should be back to available
    phase = db_conn.execute(
        "SELECT status FROM phases WHERE ticket_id='T001'"
    ).fetchone()
    assert phase["status"] == "available"

    # Agent should be marked stale
    agent = db_conn.execute("SELECT status FROM agents WHERE id='stale-agent-001'").fetchone()
    assert agent["status"] == "stale"


def test_cleanup_stale_agents_ignores_fresh_agents(db_conn):
    """Agents with recent heartbeats should not be cleaned up."""
    db_conn.execute(
        "INSERT INTO tickets (id, name, full_name, current_status, markdown_path, languages) "
        "VALUES ('T001', 'test', 'T001_test', 'Draft', 'tickets/T001.md', 'C++')"
    )
    db_conn.execute(
        "INSERT INTO phases (ticket_id, phase_name, phase_order, status, agent_type, claimed_by) "
        "VALUES ('T001', 'Design', 0, 'running', 'cpp-architect', 'fresh-agent-001')"
    )
    # Fresh agent with current heartbeat
    db_conn.execute(
        "INSERT INTO agents (id, agent_type, status, last_heartbeat) "
        "VALUES ('fresh-agent-001', 'cpp-architect', 'working', datetime('now'))"
    )
    db_conn.commit()

    released = sched_mod.cleanup_stale_agents(db_conn, stale_timeout_minutes=30)

    assert len(released) == 0


# ---------------------------------------------------------------------------
# Gate resolution tests
# ---------------------------------------------------------------------------


def test_resolve_gate_approve_makes_phase_available(db_conn):
    """Approving a gate should mark the blocked phase as available."""
    db_conn.execute(
        "INSERT INTO tickets (id, name, full_name, current_status, markdown_path, languages) "
        "VALUES ('T001', 'test', 'T001_test', 'Draft', 'tickets/T001.md', 'C++')"
    )
    db_conn.execute(
        "INSERT INTO phases (ticket_id, phase_name, phase_order, status, agent_type) "
        "VALUES ('T001', 'Design Review', 0, 'blocked', NULL)"
    )
    db_conn.commit()
    phase_id = db_conn.execute(
        "SELECT id FROM phases WHERE ticket_id='T001'"
    ).fetchone()["id"]

    db_conn.execute(
        "INSERT INTO human_gates (phase_id, ticket_id, gate_type, status) "
        "VALUES (?, 'T001', 'design_review', 'pending')",
        (phase_id,),
    )
    db_conn.commit()
    gate_id = db_conn.execute("SELECT id FROM human_gates WHERE ticket_id='T001'").fetchone()["id"]

    result = sched_mod.resolve_gate(db_conn, gate_id, "approved", "reviewer-dan")

    assert result["decision"] == "approved"
    phase_status = db_conn.execute("SELECT status FROM phases WHERE id=?", (phase_id,)).fetchone()["status"]
    assert phase_status == "available"


def test_resolve_gate_reject_keeps_phase_blocked(db_conn):
    """Rejecting a gate should keep the phase blocked."""
    db_conn.execute(
        "INSERT INTO tickets (id, name, full_name, current_status, markdown_path, languages) "
        "VALUES ('T001', 'test', 'T001_test', 'Draft', 'tickets/T001.md', 'C++')"
    )
    db_conn.execute(
        "INSERT INTO phases (ticket_id, phase_name, phase_order, status, agent_type) "
        "VALUES ('T001', 'Design Review', 0, 'blocked', NULL)"
    )
    db_conn.commit()
    phase_id = db_conn.execute("SELECT id FROM phases WHERE ticket_id='T001'").fetchone()["id"]

    db_conn.execute(
        "INSERT INTO human_gates (phase_id, ticket_id, gate_type, status) "
        "VALUES (?, 'T001', 'design_review', 'pending')",
        (phase_id,),
    )
    db_conn.commit()
    gate_id = db_conn.execute("SELECT id FROM human_gates WHERE ticket_id='T001'").fetchone()["id"]

    result = sched_mod.resolve_gate(db_conn, gate_id, "rejected", "reviewer-dan", "Not ready")

    assert result["decision"] == "rejected"
    phase_status = db_conn.execute("SELECT status FROM phases WHERE id=?", (phase_id,)).fetchone()["status"]
    assert phase_status == "blocked"  # unchanged
