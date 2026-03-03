"""
Tests for ticket and coverage indexers and their MCP query methods.

Validates:
- Ticket indexer: parsing title, summary, metadata, acceptance criteria,
  workflow log, files, references, canonical phase mapping, incremental behavior
- Coverage indexer: lcov .info parsing, DB storage, deduplication
- TraceabilityServer: search_tickets, get_ticket, list_tickets,
  get_coverage_summary, get_file_coverage, get_ticket_coverage
"""

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from workflow_engine.traceability.schema import create_schema
from workflow_engine.traceability.index_tickets import (
    _map_canonical_phase,
    _parse_title,
    _parse_summary,
    _parse_acceptance_criteria,
    _parse_workflow_log,
    _parse_files,
    _parse_references,
    _detect_ticket_type,
    index_single_ticket,
    index_tickets,
)
from workflow_engine.traceability.index_coverage import (
    parse_lcov_info,
    index_coverage,
)
from workflow_engine.traceability.server import TraceabilityServer


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "traceability.db")


@pytest.fixture
def conn(db_path):
    c = create_schema(db_path)
    yield c
    c.close()


SAMPLE_TICKET = """\
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
- [x] AC1: GJK algorithm correctly determines intersection
- [ ] AC2: EPA provides contact normal and penetration depth
- [ ] AC3: Performance meets 60fps target for 100 bodies

## Workflow Log

### Design Phase
- **Started**: 2025-06-16T10:00:00Z
- **Completed**: 2025-06-18T14:30:00Z
- **Branch**: 0050-design
- **Artifacts**:
  - `docs/designs/0050_collision_narrow/design.md`
  - `docs/designs/0050_collision_narrow/collision.puml`
- **Notes**: Approved with minor revisions

### Implementation Phase
- **Started**: 2025-06-20T09:00:00Z

## Files

### New Files
- `msd/msd-sim/src/collision/gjk.cpp` — GJK algorithm implementation
- `msd/msd-sim/src/collision/epa.cpp` — EPA algorithm implementation

### Modified Files
- `msd/msd-sim/src/collision/pipeline.cpp` — Integration with collision pipeline

## References
- Ticket 0040 — Broad phase sweep and prune
- `msd/msd-sim/src/collision/broad_phase.cpp`

## Dependencies
- Parent ticket 0035
- Blocks 0060
"""


# ===========================================================================
# Canonical phase mapping
# ===========================================================================


def test_map_canonical_phase_draft():
    assert _map_canonical_phase("Draft") == "draft"


def test_map_canonical_phase_design_complete():
    assert _map_canonical_phase("Design Complete — Awaiting Review") == "design"


def test_map_canonical_phase_design_approved():
    assert _map_canonical_phase("Design Approved — Ready for Prototype") == "design_review"


def test_map_canonical_phase_implementation():
    assert _map_canonical_phase("Implementation Complete — Awaiting Test Writing") == "implementation"


def test_map_canonical_phase_merged():
    assert _map_canonical_phase("Merged / Complete") == "merged"


def test_map_canonical_phase_quality_gate():
    assert _map_canonical_phase("Quality Gate Passed — Awaiting Review") == "quality_gate"


def test_map_canonical_phase_testing():
    assert _map_canonical_phase("Test Writing Complete — Awaiting Quality Gate") == "testing"


def test_map_canonical_phase_approved():
    assert _map_canonical_phase("Approved — Ready to Merge") == "review"


def test_map_canonical_phase_ready_for_implementation():
    assert _map_canonical_phase("Ready for Implementation") == "prototype"


def test_map_canonical_phase_math():
    assert _map_canonical_phase("Math Formulation Complete") == "math"


def test_map_canonical_phase_unknown_defaults_to_draft():
    assert _map_canonical_phase("Something Unknown") == "draft"


# ===========================================================================
# Title parsing
# ===========================================================================


def test_parse_title_feature_ticket():
    assert _parse_title(SAMPLE_TICKET) == "Collision Detection Narrow Phase"


def test_parse_title_plain_heading():
    content = "# My Simple Title\n\nSome text."
    assert _parse_title(content) == "My Simple Title"


def test_parse_title_empty_content():
    assert _parse_title("") == "Untitled"


# ===========================================================================
# Summary parsing
# ===========================================================================


def test_parse_summary():
    summary = _parse_summary(SAMPLE_TICKET)
    assert summary is not None
    assert "GJK" in summary
    assert "EPA" in summary


def test_parse_summary_missing():
    assert _parse_summary("# No summary here") is None


# ===========================================================================
# Acceptance criteria parsing
# ===========================================================================


def test_parse_acceptance_criteria_count():
    criteria = _parse_acceptance_criteria(SAMPLE_TICKET)
    assert len(criteria) == 3


def test_parse_acceptance_criteria_ids():
    criteria = _parse_acceptance_criteria(SAMPLE_TICKET)
    ids = [c["criterion_id"] for c in criteria]
    assert "AC1" in ids
    assert "AC2" in ids
    assert "AC3" in ids


def test_parse_acceptance_criteria_met_status():
    criteria = _parse_acceptance_criteria(SAMPLE_TICKET)
    ac1 = next(c for c in criteria if c["criterion_id"] == "AC1")
    ac2 = next(c for c in criteria if c["criterion_id"] == "AC2")
    assert ac1["is_met"] is True
    assert ac2["is_met"] is False


def test_parse_acceptance_criteria_without_ids():
    content = """## Acceptance Criteria
- [x] First criterion without ID
- [ ] Second criterion without ID
"""
    criteria = _parse_acceptance_criteria(content)
    assert len(criteria) == 2
    assert criteria[0]["criterion_id"] is None
    assert criteria[0]["is_met"] is True


def test_parse_acceptance_criteria_empty():
    assert _parse_acceptance_criteria("# No criteria") == []


# ===========================================================================
# Workflow log parsing
# ===========================================================================


def test_parse_workflow_log_count():
    entries = _parse_workflow_log(SAMPLE_TICKET)
    assert len(entries) == 2


def test_parse_workflow_log_phase_names():
    entries = _parse_workflow_log(SAMPLE_TICKET)
    phases = [e["phase_name"] for e in entries]
    assert "Design" in phases
    assert "Implementation" in phases


def test_parse_workflow_log_timestamps():
    entries = _parse_workflow_log(SAMPLE_TICKET)
    design = next(e for e in entries if e["phase_name"] == "Design")
    assert design["started_at"] == "2025-06-16T10:00:00Z"
    assert design["completed_at"] == "2025-06-18T14:30:00Z"


def test_parse_workflow_log_artifacts():
    entries = _parse_workflow_log(SAMPLE_TICKET)
    design = next(e for e in entries if e["phase_name"] == "Design")
    assert len(design["artifacts"]) == 2
    assert any("design.md" in a for a in design["artifacts"])


def test_parse_workflow_log_branch():
    entries = _parse_workflow_log(SAMPLE_TICKET)
    design = next(e for e in entries if e["phase_name"] == "Design")
    assert design["branch"] == "0050-design"


def test_parse_workflow_log_notes():
    entries = _parse_workflow_log(SAMPLE_TICKET)
    design = next(e for e in entries if e["phase_name"] == "Design")
    assert design["notes"] == "Approved with minor revisions"


def test_parse_workflow_log_empty():
    assert _parse_workflow_log("# No log") == []


# ===========================================================================
# Files parsing
# ===========================================================================


def test_parse_files_count():
    files = _parse_files(SAMPLE_TICKET)
    assert len(files) >= 3


def test_parse_files_change_types():
    files = _parse_files(SAMPLE_TICKET)
    types = {f["change_type"] for f in files}
    assert "new" in types
    assert "modified" in types


def test_parse_files_paths():
    files = _parse_files(SAMPLE_TICKET)
    paths = [f["file_path"] for f in files]
    assert any("gjk.cpp" in p for p in paths)


def test_parse_files_descriptions():
    files = _parse_files(SAMPLE_TICKET)
    gjk = next(f for f in files if "gjk.cpp" in f["file_path"])
    assert gjk["description"] is not None
    assert "GJK" in gjk["description"]


def test_parse_files_empty():
    assert _parse_files("# No files") == []


# ===========================================================================
# References parsing
# ===========================================================================


def test_parse_references_ticket_refs():
    refs = _parse_references(SAMPLE_TICKET)
    ticket_refs = [r for r in refs if r["ref_type"] == "ticket"]
    targets = [r["ref_target"] for r in ticket_refs]
    assert "0040" in targets


def test_parse_references_code_refs():
    refs = _parse_references(SAMPLE_TICKET)
    code_refs = [r for r in refs if r["ref_type"] == "code"]
    assert len(code_refs) >= 1


def test_parse_references_parent():
    refs = _parse_references(SAMPLE_TICKET)
    parent_refs = [r for r in refs if r["ref_type"] == "parent"]
    assert any(r["ref_target"] == "0035" for r in parent_refs)


def test_parse_references_blocks():
    refs = _parse_references(SAMPLE_TICKET)
    block_refs = [r for r in refs if r["ref_type"] == "blocks"]
    assert any(r["ref_target"] == "0060" for r in block_refs)


def test_parse_references_empty():
    assert _parse_references("# No references") == []


# ===========================================================================
# Ticket type detection
# ===========================================================================


def test_detect_ticket_type_feature():
    assert _detect_ticket_type(SAMPLE_TICKET) == "feature"


def test_detect_ticket_type_debug():
    assert _detect_ticket_type("# Debug Ticket: Fix crash") == "debug"


# ===========================================================================
# index_tickets integration
# ===========================================================================


def test_index_tickets_basic(conn, tmp_path):
    """index_tickets() should parse and store a ticket."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    (tickets_dir / "0050_collision_narrow.md").write_text(SAMPLE_TICKET)

    result = index_tickets(conn, str(tmp_path), tickets_dir="tickets")
    assert result["new_count"] == 1
    assert result["total"] == 1


def test_index_tickets_stored_fields(conn, tmp_path):
    """index_tickets() should correctly store parsed fields."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    (tickets_dir / "0050_collision_narrow.md").write_text(SAMPLE_TICKET)
    index_tickets(conn, str(tmp_path), tickets_dir="tickets")

    row = conn.execute("SELECT * FROM tickets WHERE ticket_number = '0050'").fetchone()
    assert row is not None
    assert row["title"] == "Collision Detection Narrow Phase"
    assert row["canonical_phase"] == "design_review"
    assert row["priority"] == "High"
    assert row["author"] == "Daniel Newman"
    assert row["requires_math"] == 1


def test_index_tickets_acceptance_criteria_stored(conn, tmp_path):
    """index_tickets() should store acceptance criteria."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    (tickets_dir / "0050_collision_narrow.md").write_text(SAMPLE_TICKET)
    index_tickets(conn, str(tmp_path), tickets_dir="tickets")

    rows = conn.execute(
        "SELECT * FROM ticket_acceptance_criteria WHERE ticket_number = '0050'"
    ).fetchall()
    assert len(rows) == 3


def test_index_tickets_workflow_log_stored(conn, tmp_path):
    """index_tickets() should store workflow log entries."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    (tickets_dir / "0050_collision_narrow.md").write_text(SAMPLE_TICKET)
    index_tickets(conn, str(tmp_path), tickets_dir="tickets")

    rows = conn.execute(
        "SELECT * FROM ticket_workflow_log WHERE ticket_number = '0050'"
    ).fetchall()
    assert len(rows) == 2


def test_index_tickets_artifacts_stored(conn, tmp_path):
    """index_tickets() should store artifacts linked to workflow log entries."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    (tickets_dir / "0050_collision_narrow.md").write_text(SAMPLE_TICKET)
    index_tickets(conn, str(tmp_path), tickets_dir="tickets")

    rows = conn.execute("SELECT * FROM ticket_artifacts").fetchall()
    assert len(rows) == 2


def test_index_tickets_files_stored(conn, tmp_path):
    """index_tickets() should store file declarations."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    (tickets_dir / "0050_collision_narrow.md").write_text(SAMPLE_TICKET)
    index_tickets(conn, str(tmp_path), tickets_dir="tickets")

    rows = conn.execute(
        "SELECT * FROM ticket_files WHERE ticket_number = '0050'"
    ).fetchall()
    assert len(rows) >= 3


def test_index_tickets_references_stored(conn, tmp_path):
    """index_tickets() should store cross-references."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    (tickets_dir / "0050_collision_narrow.md").write_text(SAMPLE_TICKET)
    index_tickets(conn, str(tmp_path), tickets_dir="tickets")

    rows = conn.execute(
        "SELECT * FROM ticket_references WHERE ticket_number = '0050'"
    ).fetchall()
    assert len(rows) >= 2


def test_index_tickets_incremental_skips_unchanged(conn, tmp_path):
    """index_tickets() should skip files that haven't changed."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    (tickets_dir / "0050_collision_narrow.md").write_text(SAMPLE_TICKET)

    result1 = index_tickets(conn, str(tmp_path), tickets_dir="tickets")
    result2 = index_tickets(conn, str(tmp_path), tickets_dir="tickets")

    assert result1["new_count"] == 1
    assert result2["new_count"] == 0
    assert result2["updated_count"] == 0


def test_index_tickets_skips_non_ticket_files(conn, tmp_path):
    """index_tickets() should skip files without ticket number prefix."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    (tickets_dir / "README.md").write_text("# Not a ticket")

    result = index_tickets(conn, str(tmp_path), tickets_dir="tickets")
    assert result["total"] == 0


def test_index_tickets_missing_dir(conn, tmp_path):
    """index_tickets() should return zeros when tickets dir doesn't exist."""
    result = index_tickets(conn, str(tmp_path), tickets_dir="nonexistent")
    assert result["total"] == 0


def test_index_tickets_multiple_tickets(conn, tmp_path):
    """index_tickets() should handle multiple ticket files."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    (tickets_dir / "0050_collision.md").write_text(SAMPLE_TICKET)
    (tickets_dir / "0051_physics.md").write_text(
        "# Feature Ticket: Physics Engine\n\n## Status\n- [x] Draft\n\n## Metadata\n- **Priority**: Medium\n\n## Summary\nPhysics engine implementation.\n"
    )

    result = index_tickets(conn, str(tmp_path), tickets_dir="tickets")
    assert result["total"] == 2


# ===========================================================================
# lcov .info parsing
# ===========================================================================


SAMPLE_LCOV = """\
TN:test
SF:/home/user/project/src/foo.cpp
FNF:5
FNH:3
DA:10,1
DA:11,5
DA:12,0
DA:20,1
LF:4
LH:3
BRF:2
BRH:1
end_of_record
SF:/home/user/project/src/bar.cpp
FNF:2
FNH:2
DA:1,10
DA:2,0
LF:2
LH:1
end_of_record
"""


def test_parse_lcov_info_file_count():
    result = parse_lcov_info(SAMPLE_LCOV)
    assert len(result["files"]) == 2


def test_parse_lcov_info_totals():
    result = parse_lcov_info(SAMPLE_LCOV)
    assert result["total_lines"] == 6
    assert result["total_hits"] == 4


def test_parse_lcov_info_file_summary():
    result = parse_lcov_info(SAMPLE_LCOV)
    foo = next(f for f in result["files"] if "foo" in f["file_path"])
    assert foo["lines_found"] == 4
    assert foo["lines_hit"] == 3
    assert foo["functions_found"] == 5
    assert foo["functions_hit"] == 3
    assert foo["branches_found"] == 2
    assert foo["branches_hit"] == 1


def test_parse_lcov_info_line_data():
    result = parse_lcov_info(SAMPLE_LCOV)
    foo = next(f for f in result["files"] if "foo" in f["file_path"])
    assert len(foo["line_data"]) == 4
    assert (10, 1) in foo["line_data"]
    assert (12, 0) in foo["line_data"]


def test_parse_lcov_info_relative_paths():
    result = parse_lcov_info(SAMPLE_LCOV, repo_root="/home/user/project")
    foo = next(f for f in result["files"] if "foo" in f["file_path"])
    assert foo["file_path"] == "src/foo.cpp"


def test_parse_lcov_info_empty():
    result = parse_lcov_info("")
    assert result["files"] == []
    assert result["total_lines"] == 0


# ===========================================================================
# index_coverage integration
# ===========================================================================


def test_index_coverage_basic(conn, tmp_path):
    """index_coverage() should parse and store coverage data."""
    info_file = tmp_path / "coverage.info"
    info_file.write_text(SAMPLE_LCOV)

    with patch(
        "workflow_engine.traceability.index_coverage._get_git_sha",
        return_value="abc123",
    ):
        result = index_coverage(
            conn, str(tmp_path), info_file="coverage.info"
        )

    assert result["skipped"] is False
    assert result["files_count"] == 2
    assert result["total_lines"] == 6
    assert result["total_hits"] == 4
    assert result["run_id"] is not None


def test_index_coverage_stores_run(conn, tmp_path):
    """index_coverage() should create a coverage_runs record."""
    info_file = tmp_path / "coverage.info"
    info_file.write_text(SAMPLE_LCOV)

    with patch(
        "workflow_engine.traceability.index_coverage._get_git_sha",
        return_value="abc123",
    ):
        index_coverage(conn, str(tmp_path), info_file="coverage.info")

    row = conn.execute("SELECT * FROM coverage_runs").fetchone()
    assert row is not None
    assert row["commit_sha"] == "abc123"
    assert row["total_lines"] == 6


def test_index_coverage_stores_files(conn, tmp_path):
    """index_coverage() should create coverage_files records."""
    info_file = tmp_path / "coverage.info"
    info_file.write_text(SAMPLE_LCOV)

    with patch(
        "workflow_engine.traceability.index_coverage._get_git_sha",
        return_value="abc123",
    ):
        index_coverage(conn, str(tmp_path), info_file="coverage.info")

    rows = conn.execute("SELECT * FROM coverage_files").fetchall()
    assert len(rows) == 2


def test_index_coverage_stores_lines(conn, tmp_path):
    """index_coverage() should store per-line coverage data."""
    info_file = tmp_path / "coverage.info"
    info_file.write_text(SAMPLE_LCOV)

    with patch(
        "workflow_engine.traceability.index_coverage._get_git_sha",
        return_value="abc123",
    ):
        index_coverage(
            conn, str(tmp_path), info_file="coverage.info", store_lines=True
        )

    rows = conn.execute("SELECT * FROM coverage_lines").fetchall()
    assert len(rows) == 6  # 4 lines in foo + 2 in bar


def test_index_coverage_skip_lines(conn, tmp_path):
    """index_coverage(store_lines=False) should not store line-level data."""
    info_file = tmp_path / "coverage.info"
    info_file.write_text(SAMPLE_LCOV)

    with patch(
        "workflow_engine.traceability.index_coverage._get_git_sha",
        return_value="abc123",
    ):
        index_coverage(
            conn, str(tmp_path), info_file="coverage.info", store_lines=False
        )

    rows = conn.execute("SELECT * FROM coverage_lines").fetchall()
    assert len(rows) == 0


def test_index_coverage_deduplication(conn, tmp_path):
    """index_coverage() should skip if the same file was already indexed."""
    info_file = tmp_path / "coverage.info"
    info_file.write_text(SAMPLE_LCOV)

    with patch(
        "workflow_engine.traceability.index_coverage._get_git_sha",
        return_value="abc123",
    ):
        result1 = index_coverage(conn, str(tmp_path), info_file="coverage.info")
        result2 = index_coverage(conn, str(tmp_path), info_file="coverage.info")

    assert result1["skipped"] is False
    assert result2["skipped"] is True


def test_index_coverage_missing_file(conn, tmp_path):
    """index_coverage() should return skipped when info file doesn't exist."""
    result = index_coverage(conn, str(tmp_path), info_file="nonexistent.info")
    assert result["skipped"] is True
    assert result["run_id"] is None


# ===========================================================================
# TraceabilityServer: ticket queries
# ===========================================================================


@pytest.fixture
def server_with_tickets(conn, tmp_path):
    """Create a server with indexed tickets."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    (tickets_dir / "0050_collision_narrow.md").write_text(SAMPLE_TICKET)
    (tickets_dir / "0051_physics.md").write_text(
        "# Feature Ticket: Physics Engine\n\n## Status\n- [x] Draft\n\n"
        "## Metadata\n- **Priority**: Medium\n- **Target Component(s)**: msd-sim\n\n"
        "## Summary\nPhysics engine for rigid body dynamics.\n"
    )
    index_tickets(conn, str(tmp_path), tickets_dir="tickets")
    return TraceabilityServer(conn)


def test_search_tickets_fts(server_with_tickets):
    """search_tickets() should find tickets by FTS query."""
    results = server_with_tickets.search_tickets("collision")
    assert len(results) >= 1
    assert any(r["ticket_number"] == "0050" for r in results)


def test_search_tickets_filter_phase(server_with_tickets):
    """search_tickets() should filter by canonical phase."""
    results = server_with_tickets.search_tickets("", phase="draft")
    tickets = [r["ticket_number"] for r in results]
    assert "0051" in tickets


def test_search_tickets_filter_component(server_with_tickets):
    """search_tickets() should filter by target component."""
    results = server_with_tickets.search_tickets("", component="msd-sim")
    assert len(results) >= 1


def test_get_ticket_full_detail(server_with_tickets):
    """get_ticket() should return complete ticket detail."""
    result = server_with_tickets.get_ticket("0050")
    assert "error" not in result
    assert result["title"] == "Collision Detection Narrow Phase"
    assert len(result["acceptance_criteria"]) == 3
    assert len(result["workflow_log"]) == 2
    assert len(result["files"]) >= 3
    assert len(result["references"]) >= 2


def test_get_ticket_not_found(server_with_tickets):
    """get_ticket() should return error for unknown ticket."""
    result = server_with_tickets.get_ticket("9999")
    assert "error" in result


def test_list_tickets_all(server_with_tickets):
    """list_tickets() should return all tickets."""
    results = server_with_tickets.list_tickets()
    assert len(results) == 2


def test_list_tickets_filter_priority(server_with_tickets):
    """list_tickets() should filter by priority."""
    results = server_with_tickets.list_tickets(priority="High")
    assert len(results) == 1
    assert results[0]["ticket_number"] == "0050"


# ===========================================================================
# TraceabilityServer: coverage queries
# ===========================================================================


@pytest.fixture
def server_with_coverage(conn, tmp_path):
    """Create a server with indexed coverage data."""
    info_file = tmp_path / "coverage.info"
    info_file.write_text(SAMPLE_LCOV)

    with patch(
        "workflow_engine.traceability.index_coverage._get_git_sha",
        return_value="abc123",
    ):
        index_coverage(conn, str(tmp_path), info_file="coverage.info")

    return TraceabilityServer(conn)


def test_get_coverage_summary(server_with_coverage):
    """get_coverage_summary() should return the latest run."""
    result = server_with_coverage.get_coverage_summary()
    assert "error" not in result
    assert result["total_lines"] == 6
    assert result["total_hits"] == 4
    assert len(result["top_files"]) == 2


def test_get_coverage_summary_no_runs(conn):
    """get_coverage_summary() should return error when no runs exist."""
    server = TraceabilityServer(conn)
    result = server.get_coverage_summary()
    assert "error" in result


def test_get_file_coverage(server_with_coverage):
    """get_file_coverage() should return per-file detail."""
    result = server_with_coverage.get_file_coverage("foo")
    assert "error" not in result
    assert result["lines_found"] == 4
    assert len(result["lines"]) == 4


def test_get_file_coverage_not_found(server_with_coverage):
    """get_file_coverage() should return error for unknown file."""
    result = server_with_coverage.get_file_coverage("nonexistent.cpp")
    assert "error" in result


def test_get_ticket_coverage_no_files(conn):
    """get_ticket_coverage() should handle tickets with no declared files."""
    # Insert a ticket without files
    conn.execute(
        """INSERT INTO tickets (ticket_number, title, canonical_phase, source_file, indexed_at)
           VALUES ('0099', 'Test', 'draft', 'tickets/0099.md', '2025-01-01')"""
    )
    conn.commit()

    server = TraceabilityServer(conn)
    result = server.get_ticket_coverage("0099")
    assert "error" in result


# ===========================================================================
# reindex.py integration
# ===========================================================================


def test_reindex_all_includes_tickets_and_coverage(conn, tmp_path):
    """reindex_all() should include tickets and coverage results."""
    from workflow_engine.traceability.reindex import reindex_all

    (tmp_path / "tickets").mkdir()
    (tmp_path / "tickets" / "0001_test.md").write_text(
        "# Feature Ticket: Test\n\n## Status\n- [x] Draft\n\n## Metadata\n\n## Summary\nTest.\n"
    )

    with (
        patch("workflow_engine.traceability.reindex.index_git_history", return_value={"new_count": 0, "total": 0}),
        patch("workflow_engine.traceability.reindex.index_decisions", return_value={"new_count": 0, "total": 0, "symbol_links": 0}),
        patch("workflow_engine.traceability.reindex.index_symbols", return_value={}),
        patch("workflow_engine.traceability.reindex.index_records", return_value={"composite_count": 0, "generated_count": 0}),
    ):
        result = reindex_all(conn, str(tmp_path))

    assert "tickets" in result
    assert "coverage" in result
    assert result["tickets"]["total"] >= 1
    assert result["coverage"]["skipped"] is True  # no .info file


# ===========================================================================
# index_single_ticket unit tests
# ===========================================================================


def test_index_single_ticket_basic(conn):
    """index_single_ticket() should parse content and insert into DB."""
    result = index_single_ticket(conn, "0050", SAMPLE_TICKET, "tickets/0050_collision.md")
    conn.commit()

    assert result["ticket_number"] == "0050"
    assert result["title"] == "Collision Detection Narrow Phase"
    assert result["canonical_phase"] == "design_review"

    row = conn.execute("SELECT * FROM tickets WHERE ticket_number = '0050'").fetchone()
    assert row is not None
    assert row["title"] == "Collision Detection Narrow Phase"


def test_index_single_ticket_replaces_existing(conn):
    """index_single_ticket() should replace an existing ticket on re-index."""
    index_single_ticket(conn, "0050", SAMPLE_TICKET, "tickets/0050_collision.md")
    conn.commit()

    updated_content = SAMPLE_TICKET.replace(
        "Collision Detection Narrow Phase", "Updated Title"
    )
    result = index_single_ticket(conn, "0050", updated_content, "tickets/0050_collision.md")
    conn.commit()

    assert result["title"] == "Updated Title"
    count = conn.execute("SELECT COUNT(*) FROM tickets WHERE ticket_number = '0050'").fetchone()[0]
    assert count == 1


# ===========================================================================
# TraceabilityServer.create_ticket tests
# ===========================================================================


NEW_TICKET_CONTENT = """\
# Feature Ticket: Widget Factory

## Status
- [x] Draft
- [ ] Ready for Design

## Metadata
- **Created**: 2026-03-01
- **Author**: Test User
- **Priority**: Medium
- **Estimated Complexity**: Small
- **Target Component(s)**: msd-assets

## Summary
Implement a widget factory for creating widgets.

## Acceptance Criteria
- [ ] AC1: Factory creates widgets
"""


def test_create_ticket_writes_file(conn, tmp_path):
    """create_ticket() should write a markdown file to the tickets directory."""
    server = TraceabilityServer(conn)
    result = server.create_ticket("0090", NEW_TICKET_CONTENT, str(tmp_path))

    assert "error" not in result
    assert result["ticket_number"] == "0090"
    assert result["title"] == "Widget Factory"

    # Verify file exists on disk
    written_file = Path(result["file_path"])
    assert written_file.exists()
    assert written_file.read_text(encoding="utf-8") == NEW_TICKET_CONTENT
    assert written_file.name.startswith("0090_")
    assert written_file.name.endswith(".md")


def test_create_ticket_indexes_into_db(conn, tmp_path):
    """create_ticket() should insert the ticket into the DB and be queryable."""
    server = TraceabilityServer(conn)
    server.create_ticket("0090", NEW_TICKET_CONTENT, str(tmp_path))

    ticket = server.get_ticket("0090")
    assert "error" not in ticket
    assert ticket["title"] == "Widget Factory"
    assert ticket["priority"] == "Medium"
    assert ticket["canonical_phase"] == "draft"
    assert len(ticket["acceptance_criteria"]) == 1


def test_create_ticket_duplicate_returns_error(conn, tmp_path):
    """create_ticket() should return error for duplicate ticket_number."""
    server = TraceabilityServer(conn)
    result1 = server.create_ticket("0090", NEW_TICKET_CONTENT, str(tmp_path))
    assert "error" not in result1

    result2 = server.create_ticket("0090", NEW_TICKET_CONTENT, str(tmp_path))
    assert "error" in result2
    assert "already exists" in result2["error"]


def test_create_ticket_invalid_format_returns_error(conn, tmp_path):
    """create_ticket() should return error for invalid ticket_number format."""
    server = TraceabilityServer(conn)

    result = server.create_ticket("12", NEW_TICKET_CONTENT, str(tmp_path))
    assert "error" in result
    assert "Invalid" in result["error"]

    result = server.create_ticket("abcd", NEW_TICKET_CONTENT, str(tmp_path))
    assert "error" in result

    result = server.create_ticket("00901", NEW_TICKET_CONTENT, str(tmp_path))
    assert "error" in result


def test_create_ticket_valid_suffix(conn, tmp_path):
    """create_ticket() should accept ticket numbers with a lowercase letter suffix."""
    server = TraceabilityServer(conn)
    result = server.create_ticket("0090a", NEW_TICKET_CONTENT, str(tmp_path))
    assert "error" not in result
    assert result["ticket_number"] == "0090a"


def test_create_ticket_roundtrip(conn, tmp_path):
    """create_ticket() → search_tickets() → get_ticket() should return consistent data."""
    server = TraceabilityServer(conn)
    create_result = server.create_ticket("0090", NEW_TICKET_CONTENT, str(tmp_path))
    assert "error" not in create_result

    # Search should find it
    search_results = server.search_tickets("widget")
    assert any(r["ticket_number"] == "0090" for r in search_results)

    # get_ticket should return consistent data
    ticket = server.get_ticket("0090")
    assert ticket["title"] == create_result["title"]
    assert ticket["ticket_number"] == create_result["ticket_number"]
    assert ticket["source_file"] == "tickets/0090_widget_factory.md"
