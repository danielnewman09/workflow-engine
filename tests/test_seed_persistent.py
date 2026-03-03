"""
Seed persistent databases with dummy data for manual inspection.

Run with:
    pytest tests/test_seed_persistent.py -v

Databases are written to tests/output/:
    - tests/output/workflow.db    (tickets, phases, agents, gates, audit log, build log)
    - tests/output/traceability.db (tickets, decisions, coverage, snapshots, symbols)

Open with any SQLite viewer (e.g. DB Browser for SQLite, sqlite3 CLI).
"""

import json
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from workflow_engine.engine.schema import create_db
from workflow_engine.engine.audit import log as audit_log
from workflow_engine.engine import scheduler as sched_mod
from workflow_engine.engine.models import PhaseDefinition, PhaseStatus, WorkflowConfig
from workflow_engine.engine.claim import claim_next, claim_specific
from workflow_engine.engine.schema import insert_build_attempt

from workflow_engine.traceability.schema import create_schema
from workflow_engine.traceability.index_tickets import index_tickets
from workflow_engine.traceability.index_coverage import index_coverage

OUTPUT_DIR = Path(__file__).parent / "output"


# ---------------------------------------------------------------------------
# Workflow DB
# ---------------------------------------------------------------------------


def test_seed_workflow_db():
    """Populate tests/output/workflow.db with realistic dummy data."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    db_path = OUTPUT_DIR / "workflow.db"
    if db_path.exists():
        db_path.unlink()

    conn = create_db(str(db_path))

    # -- Tickets --------------------------------------------------------
    tickets = [
        ("0040", "broad_phase_sweep", "0040_broad_phase_sweep", "High", "Large", "msd-sim", "C++", 40, "Merged / Complete", "tickets/0040_broad_phase_sweep.md"),
        ("0050", "collision_narrow", "0050_collision_narrow", "High", "Large", "msd-sim, msd-assets", "C++", 50, "Design Approved — Ready for Prototype", "tickets/0050_collision_narrow.md"),
        ("0058", "constraint_ownership", "0058_constraint_ownership", "Critical", "XL", "msd-sim", "C++", 58, "Implementation Complete — Awaiting Test Writing", "tickets/0058_constraint_ownership.md"),
        ("0060", "rigid_body_dynamics", "0060_rigid_body_dynamics", "High", "Large", "msd-sim", "C++", 60, "Ready for Design", "tickets/0060_rigid_body_dynamics.md"),
        ("0070", "tutorial_generator", "0070_tutorial_generator", "Medium", "Medium", "msd-tools", "Python", 70, "Draft", "tickets/0070_tutorial_generator.md"),
        ("0083", "database_agent_orch", "0083_database_agent_orchestration", "Critical", "XL", "workflow-engine", "Python", 83, "Quality Gate Passed — Awaiting Review", "tickets/0083_database_agent_orchestration.md"),
        ("0085", "traceability_consolidation", "0085_traceability_workflow_consolidation", "High", "Large", "workflow-engine", "Python", 85, "Design Complete — Awaiting Review", "tickets/0085_traceability_workflow_consolidation.md"),
        ("0090", "asset_pipeline", "0090_asset_pipeline_v2", "Medium", "Medium", "msd-assets", "C++", 90, "Draft", "tickets/0090_asset_pipeline_v2.md"),
    ]
    for t in tickets:
        conn.execute(
            "INSERT INTO tickets (id, name, full_name, priority, complexity, components, languages, github_issue, current_status, markdown_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            t,
        )
    conn.commit()

    # -- Phase definitions (Design → Review → Impl → Test → QG) --------
    phase_defs = [
        ("Design", 0, "cpp-architect"),
        ("Design Review", 1, None),  # human gate
        ("Prototype", 2, "cpp-implementer"),
        ("Prototype Review", 3, None),
        ("Implementation", 4, "cpp-implementer"),
        ("Test Writing", 5, "test-writer"),
        ("Quality Gate", 6, "qa-agent"),
        ("Final Review", 7, None),
    ]

    # Ticket 0040: fully completed
    for name, order, agent_type in phase_defs:
        conn.execute(
            "INSERT INTO phases (ticket_id, phase_name, phase_order, status, agent_type, completed_at, result_summary) "
            "VALUES ('0040', ?, ?, 'completed', ?, datetime('now', '-30 days'), ?)",
            (name, order, agent_type, f"{name} completed successfully"),
        )

    # Ticket 0050: Design done, Design Review approved, Prototype in progress
    for name, order, agent_type in phase_defs:
        if order < 2:
            status = "completed"
        elif order == 2:
            status = "running"
        elif order == 3:
            status = "blocked"
        else:
            status = "pending"
        conn.execute(
            "INSERT INTO phases (ticket_id, phase_name, phase_order, status, agent_type) "
            "VALUES ('0050', ?, ?, ?, ?)",
            (name, order, status, agent_type),
        )

    # Ticket 0058: through Implementation, Test Writing available
    for name, order, agent_type in phase_defs:
        if order <= 4:
            status = "completed"
        elif order == 5:
            status = "available"
        else:
            status = "pending"
        conn.execute(
            "INSERT INTO phases (ticket_id, phase_name, phase_order, status, agent_type) "
            "VALUES ('0058', ?, ?, ?, ?)",
            (name, order, status, agent_type),
        )

    # Ticket 0060: only Design is available
    for name, order, agent_type in phase_defs:
        status = "available" if order == 0 else "pending"
        conn.execute(
            "INSERT INTO phases (ticket_id, phase_name, phase_order, status, agent_type) "
            "VALUES ('0060', ?, ?, ?, ?)",
            (name, order, status, agent_type),
        )

    # Ticket 0083: QG completed, Final Review pending (human gate)
    for name, order, agent_type in phase_defs:
        if order <= 6:
            status = "completed"
        else:
            status = "blocked"
        conn.execute(
            "INSERT INTO phases (ticket_id, phase_name, phase_order, status, agent_type) "
            "VALUES ('0083', ?, ?, ?, ?)",
            (name, order, status, agent_type),
        )

    # Ticket 0085: Design done, Review pending
    for name, order, agent_type in phase_defs:
        if order == 0:
            status = "completed"
        elif order == 1:
            status = "blocked"
        else:
            status = "pending"
        conn.execute(
            "INSERT INTO phases (ticket_id, phase_name, phase_order, status, agent_type) "
            "VALUES ('0085', ?, ?, ?, ?)",
            (name, order, status, agent_type),
        )

    # Ticket 0070 & 0090: all pending (Draft)
    for tid in ("0070", "0090"):
        for name, order, agent_type in phase_defs:
            conn.execute(
                "INSERT INTO phases (ticket_id, phase_name, phase_order, status, agent_type) "
                "VALUES (?, ?, ?, 'pending', ?)",
                (tid, name, order, agent_type),
            )

    conn.commit()

    # -- Agents ---------------------------------------------------------
    agents = [
        ("agent-arch-001", "cpp-architect", "idle"),
        ("agent-arch-002", "cpp-architect", "working"),
        ("agent-impl-001", "cpp-implementer", "working"),
        ("agent-impl-002", "cpp-implementer", "idle"),
        ("agent-test-001", "test-writer", "idle"),
        ("agent-qa-001", "qa-agent", "idle"),
        ("agent-stale-001", "cpp-implementer", "stale"),
    ]
    for aid, atype, status in agents:
        conn.execute(
            "INSERT INTO agents (id, agent_type, status, last_heartbeat) "
            "VALUES (?, ?, ?, datetime('now', CASE WHEN ? = 'stale' THEN '-2 hours' ELSE '-30 seconds' END))",
            (aid, atype, status, status),
        )
    conn.commit()

    # Assign agent-impl-001 to the running prototype phase on 0050
    proto_phase = conn.execute(
        "SELECT id FROM phases WHERE ticket_id='0050' AND phase_name='Prototype'"
    ).fetchone()
    if proto_phase:
        conn.execute(
            "UPDATE phases SET claimed_by='agent-impl-001', claimed_at=datetime('now', '-1 hour'), "
            "started_at=datetime('now', '-55 minutes') WHERE id=?",
            (proto_phase["id"],),
        )
        conn.execute(
            "UPDATE agents SET current_phase_id=?, status='working' WHERE id='agent-impl-001'",
            (proto_phase["id"],),
        )
    conn.commit()

    # -- Human gates ----------------------------------------------------
    # Design Review gate for 0050 (approved)
    dr_phase = conn.execute(
        "SELECT id FROM phases WHERE ticket_id='0050' AND phase_name='Design Review'"
    ).fetchone()
    if dr_phase:
        conn.execute(
            "INSERT INTO human_gates (phase_id, ticket_id, gate_type, status, decided_at, decided_by, decision_notes) "
            "VALUES (?, '0050', 'design_review', 'approved', datetime('now', '-10 days'), 'human:dan', 'LGTM — minor naming nits')",
            (dr_phase["id"],),
        )

    # Final Review gate for 0083 (pending)
    fr_phase = conn.execute(
        "SELECT id FROM phases WHERE ticket_id='0083' AND phase_name='Final Review'"
    ).fetchone()
    if fr_phase:
        conn.execute(
            "INSERT INTO human_gates (phase_id, ticket_id, gate_type, status, context) "
            "VALUES (?, '0083', 'final_review', 'pending', ?)",
            (fr_phase["id"], json.dumps({"pr_url": "https://github.com/org/repo/pull/83", "summary": "Database orchestration implementation ready for final review"})),
        )
    conn.commit()

    # -- Dependencies ---------------------------------------------------
    deps = [
        ("0050", "0040", "completion", 1),   # 0050 needed 0040 (resolved)
        ("0060", "0050", "design", 0),        # 0060 blocked by 0050's design
        ("0060", "0058", "implementation", 0), # 0060 also blocked by 0058
    ]
    for blocked, blocking, dtype, resolved in deps:
        conn.execute(
            "INSERT INTO dependencies (blocked_ticket_id, blocking_ticket_id, dependency_type, resolved) "
            "VALUES (?, ?, ?, ?)",
            (blocked, blocking, dtype, resolved),
        )
    conn.commit()

    # -- File locks (active) -------------------------------------------
    if proto_phase:
        for fpath in ["msd/msd-sim/src/collision/gjk.cpp", "msd/msd-sim/src/collision/epa.cpp"]:
            conn.execute(
                "INSERT INTO file_locks (file_path, phase_id, agent_id) VALUES (?, ?, 'agent-impl-001')",
                (fpath, proto_phase["id"]),
            )
    conn.commit()

    # -- Audit log (a realistic sequence) -------------------------------
    audit_entries = [
        ("scheduler", "import_ticket", "ticket", "0050", None, None, {"action": "created", "priority": "High"}),
        ("scheduler", "seed_phases", "ticket", "0050", None, None, {"phases_created": 8}),
        ("scheduler", "resolve_availability", "phase", "0050/Design", "pending", "available", None),
        ("agent-arch-001", "claim_phase", "phase", "0050/Design", "available", "claimed", {"agent_type": "cpp-architect"}),
        ("agent-arch-001", "start_phase", "phase", "0050/Design", "claimed", "running", None),
        ("agent-arch-001", "complete_phase", "phase", "0050/Design", "running", "completed", {"result": "Design document produced"}),
        ("human:dan", "approve_gate", "gate", "0050/Design Review", "pending", "approved", {"notes": "LGTM"}),
        ("agent-impl-001", "claim_phase", "phase", "0050/Prototype", "available", "claimed", {"agent_type": "cpp-implementer"}),
        ("agent-impl-001", "start_phase", "phase", "0050/Prototype", "claimed", "running", None),
        ("agent-impl-001", "acquire_lock", "file_lock", "msd/msd-sim/src/collision/gjk.cpp", None, "locked", {"phase": "Prototype"}),
        ("scheduler", "import_ticket", "ticket", "0058", None, None, {"action": "created", "priority": "Critical"}),
        ("scheduler", "import_ticket", "ticket", "0083", None, None, {"action": "created", "priority": "Critical"}),
        ("scheduler", "cleanup_stale_agents", "agent", "agent-stale-001", "working", "stale", {"timeout_minutes": 30}),
    ]
    for actor, action, etype, eid, old, new, details in audit_entries:
        conn.execute("BEGIN IMMEDIATE")
        audit_log(conn, actor=actor, action=action, entity_type=etype, entity_id=eid, old_state=old, new_state=new, details=details)
        conn.commit()

    # -- Build log (implementation attempts for 0058) -------------------
    impl_phase = conn.execute(
        "SELECT id FROM phases WHERE ticket_id='0058' AND phase_name='Implementation'"
    ).fetchone()
    if impl_phase:
        builds = [
            (1, "Fix ownership transfer for constraint handles", ["msd/msd-sim/src/constraints/handle.cpp", "msd/msd-sim/src/constraints/ownership.cpp"], "fail", "error: use of deleted function 'ConstraintHandle::operator='"),
            (2, "Add move semantics to ConstraintHandle", ["msd/msd-sim/src/constraints/handle.cpp", "msd/msd-sim/include/constraints/handle.h"], "fail", "error: cannot bind rvalue reference to lvalue"),
            (3, "Use std::unique_ptr for ownership, add ConstraintHandle(ConstraintHandle&&)", ["msd/msd-sim/src/constraints/handle.cpp", "msd/msd-sim/include/constraints/handle.h", "msd/msd-sim/src/constraints/ownership.cpp"], "pass", "Build succeeded. 142 tests passed."),
        ]
        for attempt, hyp, files, result, output in builds:
            conn.execute(
                "INSERT INTO impl_build_log (phase_id, agent_id, ticket_id, attempt_number, hypothesis, files_changed, build_result, compiler_output) "
                "VALUES (?, 'agent-impl-002', '0058', ?, ?, ?, ?, ?)",
                (impl_phase["id"], attempt, hyp, json.dumps(files), result, output),
            )
    conn.commit()
    conn.close()

    assert db_path.exists()
    size = db_path.stat().st_size
    assert size > 0
    print(f"\n  Workflow DB: {db_path} ({size:,} bytes)")


# ---------------------------------------------------------------------------
# Traceability DB
# ---------------------------------------------------------------------------


TICKET_0050 = """\
# Feature Ticket: Collision Detection Narrow Phase

## Status
- [x] Draft
- [x] Ready for Design
- [x] Design Complete — Awaiting Review
- [x] Design Approved — Ready for Prototype
- [ ] Prototype Complete — Awaiting Review
- [ ] Ready for Implementation
- [ ] Implementation Complete — Awaiting Test Writing
- [ ] Test Writing Complete — Awaiting Quality Gate
- [ ] Quality Gate Passed — Awaiting Review
- [ ] Approved — Ready to Merge
- [ ] Merged / Complete

## Metadata
- **Created**: 2025-06-15
- **Author**: Daniel Newman
- **Priority**: High
- **Estimated Complexity**: Large
- **Target Component(s)**: msd-sim, msd-assets
- **Languages**: C++
- **Generate Tutorial**: No
- **Requires Math Design**: Yes

---

## Summary
Implement narrow-phase collision detection using GJK and EPA algorithms
for convex polytopes. This builds on the broad-phase sweep from ticket 0040.

## Acceptance Criteria
- [x] AC1: GJK algorithm correctly determines intersection for convex shapes
- [ ] AC2: EPA provides contact normal and penetration depth within tolerance
- [ ] AC3: Performance meets 60fps target for 100 rigid bodies
- [ ] AC4: Unit tests cover edge cases (degenerate simplices, coplanar faces)

## Workflow Log

### Design Phase
- **Started**: 2025-06-16T10:00:00Z
- **Completed**: 2025-06-18T14:30:00Z
- **Branch**: 0050-design
- **Artifacts**:
  - `docs/designs/0050_collision_narrow/design.md`
  - `docs/designs/0050_collision_narrow/collision.puml`
- **Notes**: Approved with minor revisions to EPA tolerance handling

### Implementation Phase
- **Started**: 2025-06-20T09:00:00Z

## Files

### New Files
- `msd/msd-sim/src/collision/gjk.cpp` — GJK algorithm implementation
- `msd/msd-sim/src/collision/epa.cpp` — EPA algorithm implementation
- `msd/msd-sim/include/collision/gjk.h` — GJK public header

### Modified Files
- `msd/msd-sim/src/collision/pipeline.cpp` — Integration with collision pipeline
- `msd/msd-sim/CMakeLists.txt` — Add new source files

## References
- Ticket 0040 — Broad phase sweep and prune
- `msd/msd-sim/src/collision/broad_phase.cpp`

## Dependencies
- Parent ticket 0035
- Blocks 0060
"""

TICKET_0058 = """\
# Feature Ticket: Constraint Ownership Cleanup

## Status
- [x] Draft
- [x] Ready for Design
- [x] Design Complete — Awaiting Review
- [x] Design Approved — Ready for Prototype
- [x] Prototype Complete — Awaiting Review
- [x] Ready for Implementation
- [x] Implementation Complete — Awaiting Test Writing
- [ ] Test Writing Complete — Awaiting Quality Gate
- [ ] Quality Gate Passed — Awaiting Review
- [ ] Approved — Ready to Merge
- [ ] Merged / Complete

## Metadata
- **Created**: 2025-05-20
- **Author**: Daniel Newman
- **Priority**: Critical
- **Estimated Complexity**: XL
- **Target Component(s)**: msd-sim
- **Languages**: C++
- **Generate Tutorial**: No
- **Requires Math Design**: No

---

## Summary
Refactor the constraint handle ownership model to use RAII with move semantics.
The current raw-pointer approach causes double-free crashes when constraints
are transferred between solver iterations.

## Acceptance Criteria
- [x] AC1: ConstraintHandle uses std::unique_ptr for ownership
- [x] AC2: Move constructor and move assignment work correctly
- [x] AC3: No double-free or use-after-free detected by ASAN
- [ ] AC4: Existing constraint tests updated for new ownership API

## Workflow Log

### Design Phase
- **Started**: 2025-05-21T08:00:00Z
- **Completed**: 2025-05-22T16:00:00Z
- **Branch**: 0058-design
- **Artifacts**:
  - `docs/designs/0058_constraint_ownership_cleanup/design.md`
- **Notes**: Straightforward RAII refactor

### Implementation Phase
- **Started**: 2025-05-25T09:00:00Z
- **Completed**: 2025-05-28T17:30:00Z
- **Branch**: 0058-impl
- **PR**: https://github.com/org/repo/pull/58
- **Notes**: 3 build attempts, final pass after adding move semantics

## Files

### New Files
- `msd/msd-sim/include/constraints/handle.h` — ConstraintHandle RAII wrapper

### Modified Files
- `msd/msd-sim/src/constraints/handle.cpp` — Move semantics implementation
- `msd/msd-sim/src/constraints/ownership.cpp` — Remove raw pointer transfers
- `msd/msd-sim/src/solver/iteration.cpp` — Use new ConstraintHandle API

## References
- Ticket 0035 — Original constraint system
- `msd/msd-sim/src/constraints/manager.cpp`
"""

TICKET_0083 = """\
# Feature Ticket: Database Agent Orchestration

## Status
- [x] Draft
- [x] Ready for Design
- [x] Design Complete — Awaiting Review
- [x] Design Approved — Ready for Prototype
- [x] Prototype Complete — Awaiting Review
- [x] Ready for Implementation
- [x] Implementation Complete — Awaiting Test Writing
- [x] Test Writing Complete — Awaiting Quality Gate
- [x] Quality Gate Passed — Awaiting Review
- [ ] Approved — Ready to Merge
- [ ] Merged / Complete

## Metadata
- **Created**: 2025-07-01
- **Author**: Daniel Newman
- **Priority**: Critical
- **Estimated Complexity**: XL
- **Target Component(s)**: workflow-engine
- **Languages**: Python
- **Generate Tutorial**: No
- **Requires Math Design**: No

---

## Summary
Implement SQLite-based workflow coordination with atomic claim semantics,
human gates, file lock detection, and immutable audit logging. This enables
multi-agent orchestration for the MSD build pipeline.

## Acceptance Criteria
- [x] AC1: Atomic phase claiming with no double-claims under concurrency
- [x] AC2: Human gates block phase progression until approved
- [x] AC3: Audit log captures all state transitions immutably
- [x] AC4: File locks prevent conflicting edits across agents
- [x] AC5: Stale agent cleanup releases timed-out claims

## Files

### New Files
- `workflow_engine/engine/schema.py` — Database schema and migrations
- `workflow_engine/engine/claim.py` — Atomic claim logic
- `workflow_engine/engine/scheduler.py` — Phase lifecycle management
- `workflow_engine/engine/audit.py` — Audit logging
- `workflow_engine/engine/state_machine.py` — Phase status FSM

## References
- `workflow_engine/engine/models.py`
"""

TICKET_0070 = """\
# Feature Ticket: Tutorial Generator

## Status
- [x] Draft
- [ ] Ready for Design

## Metadata
- **Created**: 2025-06-25
- **Author**: Daniel Newman
- **Priority**: Medium
- **Estimated Complexity**: Medium
- **Target Component(s)**: msd-tools
- **Languages**: Python
- **Generate Tutorial**: Yes
- **Requires Math Design**: No

---

## Summary
Auto-generate tutorial documents from annotated code examples and docstrings
in the MSD codebase.

## Acceptance Criteria
- [ ] AC1: Extracts annotated tutorial sections from source files
- [ ] AC2: Generates markdown tutorial with code blocks and explanations
- [ ] AC3: Supports both C++ and Python source files
"""


SAMPLE_LCOV = """\
TN:unit_tests
SF:msd/msd-sim/src/collision/gjk.cpp
FNF:8
FNH:6
DA:10,5
DA:11,5
DA:12,5
DA:15,3
DA:16,3
DA:20,0
DA:21,0
DA:30,10
DA:31,10
DA:32,10
LF:10
LH:8
BRF:6
BRH:4
end_of_record
SF:msd/msd-sim/src/collision/epa.cpp
FNF:5
FNH:3
DA:10,4
DA:11,4
DA:12,0
DA:13,0
DA:20,2
DA:21,2
LF:6
LH:4
BRF:4
BRH:2
end_of_record
SF:msd/msd-sim/src/collision/pipeline.cpp
FNF:12
FNH:12
DA:1,20
DA:2,20
DA:3,20
DA:10,15
DA:11,15
DA:50,8
DA:51,8
DA:52,8
LF:8
LH:8
BRF:3
BRH:3
end_of_record
SF:msd/msd-sim/src/constraints/handle.cpp
FNF:6
FNH:6
DA:10,12
DA:11,12
DA:12,12
DA:20,8
DA:21,8
DA:30,4
DA:31,0
LF:7
LH:6
BRF:2
BRH:1
end_of_record
SF:msd/msd-sim/src/constraints/ownership.cpp
FNF:4
FNH:4
DA:5,10
DA:6,10
DA:7,10
DA:15,5
DA:16,5
LF:5
LH:5
BRF:2
BRH:2
end_of_record
"""


def test_seed_traceability_db(tmp_path):
    """Populate tests/output/traceability.db with realistic dummy data."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    db_path = OUTPUT_DIR / "traceability.db"
    if db_path.exists():
        db_path.unlink()
    # Also remove WAL/SHM leftovers
    for ext in (".db-wal", ".db-shm"):
        p = OUTPUT_DIR / f"traceability{ext}"
        if p.exists():
            p.unlink()

    conn = create_schema(str(db_path))

    # -- Index tickets --------------------------------------------------
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    (tickets_dir / "0050_collision_narrow.md").write_text(TICKET_0050)
    (tickets_dir / "0058_constraint_ownership.md").write_text(TICKET_0058)
    (tickets_dir / "0083_database_agent_orch.md").write_text(TICKET_0083)
    (tickets_dir / "0070_tutorial_generator.md").write_text(TICKET_0070)

    result = index_tickets(conn, str(tmp_path), tickets_dir="tickets")
    assert result["total"] == 4

    # -- Snapshots (git history) ----------------------------------------
    snapshots = [
        ("aaa1111", "2025-05-21", "Daniel Newman", "feat: initial constraint handle design (0058)", "0058", "design"),
        ("aaa2222", "2025-05-22", "Daniel Newman", "docs: constraint ownership design doc (0058)", "0058", "design"),
        ("bbb1111", "2025-05-25", "Agent impl-002", "feat: add move semantics to ConstraintHandle (0058)", "0058", "implementation"),
        ("bbb2222", "2025-05-26", "Agent impl-002", "fix: rvalue reference binding in handle (0058)", "0058", "implementation"),
        ("bbb3333", "2025-05-28", "Agent impl-002", "feat: complete ownership refactor with unique_ptr (0058)", "0058", "implementation"),
        ("ccc1111", "2025-06-16", "Daniel Newman", "feat: GJK narrow phase design (0050)", "0050", "design"),
        ("ccc2222", "2025-06-17", "Daniel Newman", "docs: collision detection design document (0050)", "0050", "design"),
        ("ccc3333", "2025-06-18", "Daniel Newman", "fix: EPA tolerance values in design (0050)", "0050", "design"),
        ("ddd1111", "2025-07-01", "Daniel Newman", "feat: workflow engine schema design (0083)", "0083", "design"),
        ("ddd2222", "2025-07-05", "Daniel Newman", "feat: atomic claim semantics (0083)", "0083", "implementation"),
        ("ddd3333", "2025-07-08", "Daniel Newman", "feat: audit logging and human gates (0083)", "0083", "implementation"),
        ("ddd4444", "2025-07-10", "Daniel Newman", "test: workflow engine unit tests (0083)", "0083", "testing"),
        ("eee1111", "2025-07-12", "Daniel Newman", "chore: CI pipeline update", None, None),
        ("eee2222", "2025-07-15", "Daniel Newman", "docs: update README with workflow instructions", None, None),
    ]
    for sha, date, author, msg, ticket, phase in snapshots:
        conn.execute(
            "INSERT INTO snapshots (sha, date, author, message, ticket_number, phase) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (sha, date, author, msg, ticket, phase),
        )
    conn.commit()

    # -- File changes per snapshot --------------------------------------
    file_changes = [
        ("aaa1111", "msd/msd-sim/include/constraints/handle.h", "add", 45, 0),
        ("aaa2222", "docs/designs/0058_constraint_ownership_cleanup/design.md", "add", 120, 0),
        ("bbb1111", "msd/msd-sim/src/constraints/handle.cpp", "modify", 30, 10),
        ("bbb1111", "msd/msd-sim/include/constraints/handle.h", "modify", 15, 5),
        ("bbb2222", "msd/msd-sim/src/constraints/handle.cpp", "modify", 8, 3),
        ("bbb3333", "msd/msd-sim/src/constraints/handle.cpp", "modify", 25, 12),
        ("bbb3333", "msd/msd-sim/src/constraints/ownership.cpp", "modify", 18, 30),
        ("bbb3333", "msd/msd-sim/src/solver/iteration.cpp", "modify", 10, 8),
        ("ccc1111", "msd/msd-sim/src/collision/gjk.cpp", "add", 200, 0),
        ("ccc2222", "docs/designs/0050_collision_narrow/design.md", "add", 180, 0),
        ("ccc2222", "docs/designs/0050_collision_narrow/collision.puml", "add", 60, 0),
        ("ccc3333", "docs/designs/0050_collision_narrow/design.md", "modify", 15, 8),
        ("ddd1111", "workflow_engine/engine/schema.py", "add", 150, 0),
        ("ddd2222", "workflow_engine/engine/claim.py", "add", 90, 0),
        ("ddd2222", "workflow_engine/engine/scheduler.py", "add", 200, 0),
        ("ddd3333", "workflow_engine/engine/audit.py", "add", 60, 0),
        ("ddd4444", "tests/test_claim.py", "add", 120, 0),
        ("ddd4444", "tests/test_scheduler.py", "add", 180, 0),
    ]
    # Build sha→id mapping
    sha_to_id = {}
    for row in conn.execute("SELECT id, sha FROM snapshots").fetchall():
        sha_to_id[row["sha"]] = row["id"]

    for sha, fpath, ctype, ins, dels in file_changes:
        conn.execute(
            "INSERT INTO file_changes (snapshot_id, file_path, change_type, insertions, deletions) "
            "VALUES (?, ?, ?, ?, ?)",
            (sha_to_id[sha], fpath, ctype, ins, dels),
        )
    conn.commit()

    # -- Design decisions -----------------------------------------------
    decisions = [
        ("DD-0058-001", "0058", "Use std::unique_ptr for constraint ownership",
         "Raw pointers cause double-free when constraints move between solver iterations. unique_ptr enforces single ownership.",
         "Considered shared_ptr but overhead is unnecessary — constraints have strictly single ownership.",
         "active", "structured", "docs/designs/0058_constraint_ownership_cleanup/design.md"),
        ("DD-0058-002", "0058", "Provide move semantics instead of copy",
         "Constraint handles must transfer between iterations without cloning. Move semantics are natural for unique_ptr.",
         "Copy-on-write was considered but adds complexity for no benefit.",
         "active", "structured", "docs/designs/0058_constraint_ownership_cleanup/design.md"),
        ("DD-0050-001", "0050", "GJK with Voronoi region simplex caching",
         "Caching the simplex between frames exploits temporal coherence for 10x speedup on persistent contacts.",
         "Standard GJK without caching was tested — 60fps target missed with 80+ bodies.",
         "active", "structured", "docs/designs/0050_collision_narrow/design.md"),
        ("DD-0050-002", "0050", "EPA for penetration depth over SAT",
         "EPA naturally extends GJK's final simplex, avoiding separate SAT pass. Simpler pipeline.",
         "SAT would require maintaining separate support mappings. MPR was also considered but less mature.",
         "active", "structured", "docs/designs/0050_collision_narrow/design.md"),
        ("DD-0083-001", "0083", "SQLite with WAL mode for workflow coordination",
         "SQLite provides ACID transactions without a separate server process. WAL mode enables concurrent reads during writes.",
         "PostgreSQL was considered but adds deployment complexity. Redis was rejected for lack of ACID.",
         "active", "structured", "docs/designs/0083_database_agent_orchestration/design.md"),
        ("DD-0083-002", "0083", "BEGIN IMMEDIATE for all write transactions",
         "Prevents OperationalError under concurrent agent access by acquiring write lock at transaction start.",
         "Deferred transactions cause intermittent failures under concurrent claiming — validated in P1 prototype.",
         "active", "structured", "docs/designs/0083_database_agent_orchestration/design.md"),
    ]
    for dd_id, ticket, title, rationale, alternatives, status, method, source in decisions:
        conn.execute(
            "INSERT INTO design_decisions (dd_id, ticket, title, rationale, alternatives, status, extraction_method, source_file) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (dd_id, ticket, title, rationale, alternatives, status, method, source),
        )
    conn.commit()

    # Build dd_id → integer PK mapping (FK references design_decisions.id, not dd_id)
    dd_pk = {}
    for row in conn.execute("SELECT id, dd_id FROM design_decisions").fetchall():
        dd_pk[row["dd_id"]] = row["id"]

    # Link decisions to symbols
    decision_symbols = [
        ("DD-0058-001", "ConstraintHandle"),
        ("DD-0058-001", "ConstraintHandle::~ConstraintHandle"),
        ("DD-0058-002", "ConstraintHandle::ConstraintHandle(ConstraintHandle&&)"),
        ("DD-0050-001", "gjk_intersect"),
        ("DD-0050-001", "SimplexCache"),
        ("DD-0050-002", "epa_penetration_depth"),
        ("DD-0083-001", "create_db"),
        ("DD-0083-002", "claim_next"),
    ]
    for dd_id, symbol in decision_symbols:
        conn.execute(
            "INSERT INTO decision_symbols (decision_id, symbol_name) VALUES (?, ?)",
            (dd_pk[dd_id], symbol),
        )

    # Link decisions to commits
    decision_commits = [
        ("DD-0058-001", sha_to_id["bbb3333"]),
        ("DD-0058-002", sha_to_id["bbb1111"]),
        ("DD-0050-001", sha_to_id["ccc1111"]),
        ("DD-0050-002", sha_to_id["ccc1111"]),
        ("DD-0083-001", sha_to_id["ddd1111"]),
        ("DD-0083-002", sha_to_id["ddd2222"]),
    ]
    for dd_id, snap_id in decision_commits:
        conn.execute(
            "INSERT INTO decision_commits (decision_id, snapshot_id) VALUES (?, ?)",
            (dd_pk[dd_id], snap_id),
        )
    conn.commit()

    # -- Symbol snapshots -----------------------------------------------
    symbols = [
        ("ConstraintHandle", "class", "msd/msd-sim/include/constraints/handle.h", 15, "class ConstraintHandle"),
        ("ConstraintHandle::ConstraintHandle(ConstraintHandle&&)", "constructor", "msd/msd-sim/src/constraints/handle.cpp", 25, "ConstraintHandle::ConstraintHandle(ConstraintHandle&& other) noexcept"),
        ("ConstraintHandle::~ConstraintHandle", "destructor", "msd/msd-sim/src/constraints/handle.cpp", 40, "ConstraintHandle::~ConstraintHandle()"),
        ("ConstraintHandle::operator=(ConstraintHandle&&)", "method", "msd/msd-sim/src/constraints/handle.cpp", 50, "ConstraintHandle& ConstraintHandle::operator=(ConstraintHandle&& other) noexcept"),
        ("gjk_intersect", "function", "msd/msd-sim/src/collision/gjk.cpp", 30, "bool gjk_intersect(const ConvexShape& a, const ConvexShape& b, SimplexCache* cache)"),
        ("SimplexCache", "struct", "msd/msd-sim/include/collision/gjk.h", 10, "struct SimplexCache"),
        ("epa_penetration_depth", "function", "msd/msd-sim/src/collision/epa.cpp", 20, "ContactInfo epa_penetration_depth(const ConvexShape& a, const ConvexShape& b, const Simplex& simplex)"),
        ("CollisionPipeline::narrow_phase", "method", "msd/msd-sim/src/collision/pipeline.cpp", 80, "void CollisionPipeline::narrow_phase(const BroadPhaseResult& pairs)"),
        ("create_db", "function", "workflow_engine/engine/schema.py", 274, "def create_db(db_path: str | Path) -> sqlite3.Connection"),
        ("claim_next", "function", "workflow_engine/engine/claim.py", 40, "def claim_next(conn, agent_id, agent_type) -> ClaimResult"),
        ("claim_specific", "function", "workflow_engine/engine/claim.py", 80, "def claim_specific(conn, agent_id, phase_id) -> ClaimResult"),
        ("TraceabilityServer", "class", "workflow_engine/traceability/server.py", 15, "class TraceabilityServer"),
    ]
    # Add symbols to the last relevant snapshot
    for qname, kind, fpath, line, sig in symbols:
        # Pick the latest snapshot
        snap_id = sha_to_id["eee2222"]
        conn.execute(
            "INSERT INTO symbol_snapshots (snapshot_id, qualified_name, kind, file_path, line_number, signature) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (snap_id, qname, kind, fpath, line, sig),
        )
    conn.commit()

    # -- Symbol changes -------------------------------------------------
    symbol_changes = [
        (sha_to_id["bbb1111"], "ConstraintHandle::ConstraintHandle(ConstraintHandle&&)", "added", None, "msd/msd-sim/src/constraints/handle.cpp"),
        (sha_to_id["bbb3333"], "ConstraintHandle::operator=(ConstraintHandle&&)", "added", None, "msd/msd-sim/src/constraints/handle.cpp"),
        (sha_to_id["ccc1111"], "gjk_intersect", "added", None, "msd/msd-sim/src/collision/gjk.cpp"),
        (sha_to_id["ccc1111"], "SimplexCache", "added", None, "msd/msd-sim/include/collision/gjk.h"),
        (sha_to_id["ccc1111"], "epa_penetration_depth", "added", None, "msd/msd-sim/src/collision/epa.cpp"),
        (sha_to_id["ddd2222"], "claim_next", "added", None, "workflow_engine/engine/claim.py"),
        (sha_to_id["ddd2222"], "claim_specific", "added", None, "workflow_engine/engine/claim.py"),
    ]
    for snap_id, qname, ctype, old_path, new_path in symbol_changes:
        conn.execute(
            "INSERT INTO symbol_changes (snapshot_id, qualified_name, change_type, old_file_path, new_file_path) "
            "VALUES (?, ?, ?, ?, ?)",
            (snap_id, qname, ctype, old_path, new_path),
        )
    conn.commit()

    # -- Record layer mappings ------------------------------------------
    records = [
        ("ConstraintHandle", "ConstraintHandleModel", "ConstraintHandle", "constraints"),
        ("CollisionResult", "CollisionResultModel", "CollisionResult", "collision_results"),
    ]
    for rec, pydantic, pybind, sql_table in records:
        conn.execute(
            "INSERT INTO record_layer_mapping (record_name, pydantic_model, pybind_class, sql_table) "
            "VALUES (?, ?, ?, ?)",
            (rec, pydantic, pybind, sql_table),
        )

    fields = [
        ("ConstraintHandle", "pydantic", "owner_id", "int", "owner_id", None),
        ("ConstraintHandle", "pydantic", "constraint_type", "str", "constraint_type", None),
        ("ConstraintHandle", "pydantic", "is_active", "bool", "is_active", None),
        ("ConstraintHandle", "cpp", "owner_id_", "int32_t", "owner_id", "private member"),
        ("ConstraintHandle", "cpp", "type_", "ConstraintType", "constraint_type", "enum class"),
        ("ConstraintHandle", "sql", "owner_id", "INTEGER", "owner_id", None),
        ("ConstraintHandle", "sql", "constraint_type", "TEXT", "constraint_type", None),
        ("CollisionResult", "pydantic", "body_a", "int", "body_a", None),
        ("CollisionResult", "pydantic", "body_b", "int", "body_b", None),
        ("CollisionResult", "pydantic", "penetration_depth", "float", "depth", None),
        ("CollisionResult", "cpp", "body_a_", "BodyId", "body_a", None),
        ("CollisionResult", "cpp", "body_b_", "BodyId", "body_b", None),
        ("CollisionResult", "sql", "body_a", "INTEGER", "body_a", None),
        ("CollisionResult", "sql", "body_b", "INTEGER", "body_b", None),
        ("CollisionResult", "sql", "penetration_depth", "REAL", "depth", None),
    ]
    for rec, layer, fname, ftype, src, notes in fields:
        conn.execute(
            "INSERT INTO record_layer_fields (record_name, layer, field_name, field_type, source_field, notes) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (rec, layer, fname, ftype, src, notes),
        )
    conn.commit()

    # -- Coverage data --------------------------------------------------
    info_file = tmp_path / "coverage.info"
    info_file.write_text(SAMPLE_LCOV)

    with patch(
        "workflow_engine.traceability.index_coverage._get_git_sha",
        return_value="bbb3333",
    ):
        cov_result = index_coverage(
            conn, str(tmp_path), info_file="coverage.info", store_lines=True
        )
    assert cov_result["skipped"] is False

    # -- Rebuild FTS indexes -------------------------------------------
    from workflow_engine.traceability.schema import rebuild_fts
    rebuild_fts(conn)

    conn.close()

    assert db_path.exists()
    size = db_path.stat().st_size
    assert size > 0
    print(f"\n  Traceability DB: {db_path} ({size:,} bytes)")
