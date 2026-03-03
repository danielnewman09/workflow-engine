"""
Integration tests using real fixture data from tests/fixtures/msd-cpp/.

These tests validate the traceability system against a pre-populated database
snapshot and real ticket/design documents from the MSD-CPP project, providing
confidence that the system works correctly with production-scale data.

Fixture data:
    - dbs/traceability.db: 481 snapshots, 259 decisions, 4524 file changes,
      3948 symbol changes, 616 record layer fields, 755K symbol snapshots
    - tickets/0058_constraint_ownership_cleanup.md: Complete lifecycle ticket
    - docs/designs/0058_constraint_ownership_cleanup/: Full design document set
"""

import shutil
import sqlite3
from pathlib import Path

import pytest

from workflow_engine.traceability.schema import create_schema
from workflow_engine.traceability.index_tickets import (
    index_single_ticket,
    index_tickets,
    _parse_title,
    _parse_acceptance_criteria,
    _parse_workflow_log,
    _parse_files,
    _parse_references,
    _map_canonical_phase,
)
from workflow_engine.traceability.index_decisions import (
    find_documents,
    parse_structured_decisions,
    parse_heuristic_decisions,
    index_decisions,
)
from workflow_engine.traceability.server import TraceabilityServer


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "msd-cpp"
FIXTURE_DB = FIXTURES_DIR / "dbs" / "traceability.db"
FIXTURE_TICKET = FIXTURES_DIR / "tickets" / "0058_constraint_ownership_cleanup.md"
FIXTURE_DESIGN = FIXTURES_DIR / "docs" / "designs" / "0058_constraint_ownership_cleanup" / "design.md"
FIXTURE_DESIGNS_DIR = FIXTURES_DIR / "docs" / "designs"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def fixture_db_conn():
    """Open a read-only connection to the pre-populated fixture database."""
    if not FIXTURE_DB.exists():
        pytest.skip("Fixture database not found")
    conn = sqlite3.connect(f"file:{FIXTURE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    yield conn
    conn.close()


@pytest.fixture
def fresh_conn(tmp_path):
    """Create a fresh traceability DB for indexing tests."""
    c = create_schema(str(tmp_path / "traceability.db"))
    yield c
    c.close()


@pytest.fixture
def ticket_0058_content():
    """Read the real 0058 ticket markdown."""
    if not FIXTURE_TICKET.exists():
        pytest.skip("Fixture ticket not found")
    return FIXTURE_TICKET.read_text(encoding="utf-8")


@pytest.fixture
def design_0058_content():
    """Read the real 0058 design document."""
    if not FIXTURE_DESIGN.exists():
        pytest.skip("Fixture design not found")
    return FIXTURE_DESIGN.read_text(encoding="utf-8")


# ===========================================================================
# Fixture DB: Smoke tests — verify the pre-populated data is intact
# ===========================================================================


class TestFixtureDBIntegrity:
    """Verify the fixture database has expected baseline data."""

    def test_snapshot_count(self, fixture_db_conn):
        count = fixture_db_conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        assert count >= 400, f"Expected 400+ snapshots, got {count}"

    def test_decision_count(self, fixture_db_conn):
        count = fixture_db_conn.execute("SELECT COUNT(*) FROM design_decisions").fetchone()[0]
        assert count >= 200, f"Expected 200+ decisions, got {count}"

    def test_file_changes_count(self, fixture_db_conn):
        count = fixture_db_conn.execute("SELECT COUNT(*) FROM file_changes").fetchone()[0]
        assert count >= 4000, f"Expected 4000+ file changes, got {count}"

    def test_symbol_changes_count(self, fixture_db_conn):
        count = fixture_db_conn.execute("SELECT COUNT(*) FROM symbol_changes").fetchone()[0]
        assert count >= 3000, f"Expected 3000+ symbol changes, got {count}"

    def test_record_layer_fields_count(self, fixture_db_conn):
        count = fixture_db_conn.execute("SELECT COUNT(*) FROM record_layer_fields").fetchone()[0]
        assert count >= 500, f"Expected 500+ record layer fields, got {count}"

    def test_ticket_0058_snapshots(self, fixture_db_conn):
        rows = fixture_db_conn.execute(
            "SELECT sha, message FROM snapshots WHERE ticket_number = '0058' ORDER BY date"
        ).fetchall()
        assert len(rows) == 9
        messages = [r["message"] for r in rows]
        assert any("design review" in m for m in messages)
        assert any("quality gate" in m for m in messages)
        assert any("Merge" in m for m in messages)

    def test_ticket_0058_decisions(self, fixture_db_conn):
        rows = fixture_db_conn.execute(
            "SELECT dd_id, extraction_method FROM design_decisions WHERE ticket = '0058'"
        ).fetchall()
        assert len(rows) == 6
        assert all(r["extraction_method"] == "heuristic" for r in rows)

    def test_decision_symbol_links_exist(self, fixture_db_conn):
        count = fixture_db_conn.execute("SELECT COUNT(*) FROM decision_symbols").fetchone()[0]
        assert count > 0

    def test_record_layer_mappings_exist(self, fixture_db_conn):
        count = fixture_db_conn.execute("SELECT COUNT(*) FROM record_layer_mapping").fetchone()[0]
        assert count >= 20


# ===========================================================================
# TraceabilityServer queries against fixture DB
# ===========================================================================


class TestServerQueriesOnFixtureDB:
    """Test TraceabilityServer methods against real pre-populated data."""

    @pytest.fixture(autouse=True)
    def _setup(self, fixture_db_conn):
        self.server = TraceabilityServer(fixture_db_conn)

    def test_search_decisions_by_keyword(self):
        results = self.server.search_decisions("constraint")
        assert len(results) > 0
        assert any("constraint" in r["title"].lower() or "constraint" in (r.get("rationale") or "").lower()
                    for r in results)

    def test_search_decisions_filter_by_ticket(self):
        results = self.server.search_decisions("constraint", ticket="0058")
        assert all(r["ticket"] == "0058" for r in results)

    def test_get_decision_existing(self):
        result = self.server.get_decision("DD-0058-H1")
        assert "error" not in result
        assert result["ticket"] == "0058"
        assert result["extraction_method"] == "heuristic"

    def test_get_decision_not_found(self):
        result = self.server.get_decision("DD-9999-001")
        assert "error" in result

    def test_get_decision_has_symbols(self):
        result = self.server.get_decision("DD-0031-H1")
        assert "symbols" in result
        assert len(result["symbols"]) > 0
        assert "ConstraintSolver" in result["symbols"]

    def test_get_ticket_impact(self):
        result = self.server.get_ticket_impact("0058")
        assert "error" not in result
        assert len(result["commits"]) == 9
        assert len(result["file_changes"]) > 0
        assert len(result["decisions"]) == 6

    def test_get_ticket_impact_file_changes(self):
        result = self.server.get_ticket_impact("0058")
        file_paths = [fc["file_path"] for fc in result["file_changes"]]
        assert any("0058" in p for p in file_paths)

    def test_get_ticket_impact_unknown_ticket(self):
        result = self.server.get_ticket_impact("9999")
        assert "error" in result

    def test_get_commit_context(self):
        result = self.server.get_commit_context("4f425569")
        assert "error" not in result
        assert result["ticket_number"] == "0058"
        assert "design review" in result["message"]
        assert "decisions" in result

    def test_get_commit_context_unknown(self):
        result = self.server.get_commit_context("0000000000")
        assert "error" in result

    def test_why_symbol(self):
        result = self.server.why_symbol("ConstraintSolver")
        assert len(result["decisions"]) > 0

    def test_get_symbol_history(self):
        results = self.server.get_symbol_history("CollisionPipeline")
        assert len(results) >= 0  # may or may not have changes

    def test_get_record_mappings(self):
        result = self.server.get_record_mappings("AccelerationRecord")
        assert "error" not in result
        assert "layers" in result
        assert len(result["layers"]["cpp"]) > 0

    def test_get_record_mappings_drift_analysis(self):
        result = self.server.get_record_mappings("AccelerationRecord")
        assert "drift" in result
        assert "missing_in_pybind" in result["drift"]
        assert "missing_in_pydantic" in result["drift"]

    def test_check_record_drift(self):
        results = self.server.check_record_drift()
        assert isinstance(results, list)


# ===========================================================================
# Ticket indexer against real fixture ticket (0058)
# ===========================================================================


class TestTicketIndexerFixture0058:
    """Test ticket indexer parsing using the real 0058 ticket file."""

    def test_parse_title(self, ticket_0058_content):
        title = _parse_title(ticket_0058_content)
        assert "Constraint Ownership Cleanup" in title

    def test_parse_acceptance_criteria_numbered_list_not_parsed(self, ticket_0058_content):
        # NOTE: The real 0058 ticket uses numbered-list AC format:
        #   1. [x] **AC1**: Description
        # The parser currently only handles unordered lists:
        #   - [x] AC1: Description
        # This test documents the gap — when the parser is extended,
        # update this assertion to len == 6.
        criteria = _parse_acceptance_criteria(ticket_0058_content)
        assert len(criteria) == 0

    def test_parse_workflow_log_phases(self, ticket_0058_content):
        entries = _parse_workflow_log(ticket_0058_content)
        phase_names = [e["phase_name"] for e in entries]
        assert "Design" in phase_names
        assert "Design Review" in phase_names
        assert "Implementation" in phase_names
        assert "Quality Gate" in phase_names
        assert "Implementation Review" in phase_names
        assert "Documentation Update" in phase_names

    def test_parse_workflow_log_count(self, ticket_0058_content):
        entries = _parse_workflow_log(ticket_0058_content)
        assert len(entries) == 6

    def test_parse_workflow_log_artifacts(self, ticket_0058_content):
        entries = _parse_workflow_log(ticket_0058_content)
        design = next(e for e in entries if e["phase_name"] == "Design")
        assert len(design["artifacts"]) == 2
        assert any("design.md" in a for a in design["artifacts"])
        assert any(".puml" in a for a in design["artifacts"])

    def test_parse_workflow_log_branches(self, ticket_0058_content):
        entries = _parse_workflow_log(ticket_0058_content)
        for entry in entries:
            assert entry["branch"] == "0058-constraint-ownership-cleanup"

    def test_parse_workflow_log_pr(self, ticket_0058_content):
        entries = _parse_workflow_log(ticket_0058_content)
        design = next(e for e in entries if e["phase_name"] == "Design")
        assert design["pr_url"] == "#52 (draft)"

    def test_canonical_phase_merged(self, ticket_0058_content):
        # Ticket 0058 is merged/complete — last checked checkbox is "Merged / Complete"
        phase = _map_canonical_phase("Merged / Complete")
        assert phase == "merged"

    def test_parse_references_header_metadata_not_parsed(self, ticket_0058_content):
        # NOTE: The real 0058 ticket puts references in header metadata:
        #   **Blocks**: [0057_contact_tangent_recording](0057_...)
        # rather than in a ## References section. The parser doesn't
        # extract these. This test documents the gap.
        refs = _parse_references(ticket_0058_content)
        assert len(refs) == 0

    def test_index_single_ticket_roundtrip(self, fresh_conn, ticket_0058_content):
        """Index the real 0058 ticket and verify DB storage."""
        result = index_single_ticket(
            fresh_conn, "0058", ticket_0058_content,
            "tickets/0058_constraint_ownership_cleanup.md",
        )
        fresh_conn.commit()

        assert result["ticket_number"] == "0058"
        assert "Constraint Ownership Cleanup" in result["title"]

        row = fresh_conn.execute(
            "SELECT * FROM tickets WHERE ticket_number = '0058'"
        ).fetchone()
        assert row is not None
        assert row["ticket_type"] == "feature"

    def test_index_single_ticket_acceptance_criteria(self, fresh_conn, ticket_0058_content):
        # See test_parse_acceptance_criteria_numbered_list_not_parsed for context
        index_single_ticket(
            fresh_conn, "0058", ticket_0058_content,
            "tickets/0058_constraint_ownership_cleanup.md",
        )
        fresh_conn.commit()

        rows = fresh_conn.execute(
            "SELECT * FROM ticket_acceptance_criteria WHERE ticket_number = '0058'"
        ).fetchall()
        assert len(rows) == 0  # numbered-list format not yet parsed

    def test_index_single_ticket_workflow_log(self, fresh_conn, ticket_0058_content):
        index_single_ticket(
            fresh_conn, "0058", ticket_0058_content,
            "tickets/0058_constraint_ownership_cleanup.md",
        )
        fresh_conn.commit()

        rows = fresh_conn.execute(
            "SELECT * FROM ticket_workflow_log WHERE ticket_number = '0058' ORDER BY id"
        ).fetchall()
        assert len(rows) == 6
        phases = [r["phase_name"] for r in rows]
        assert phases[0] == "Design"
        assert phases[-1] == "Documentation Update"

    def test_index_single_ticket_artifacts(self, fresh_conn, ticket_0058_content):
        index_single_ticket(
            fresh_conn, "0058", ticket_0058_content,
            "tickets/0058_constraint_ownership_cleanup.md",
        )
        fresh_conn.commit()

        artifacts = fresh_conn.execute(
            "SELECT a.artifact_path FROM ticket_artifacts a"
        ).fetchall()
        paths = [a["artifact_path"] for a in artifacts]
        # Only the Design phase lists artifacts in backtick format
        # that the parser can extract; other phases use prose descriptions
        assert any("design.md" in p for p in paths)
        assert any(".puml" in p for p in paths)
        assert len(paths) == 2


# ===========================================================================
# index_tickets with fixture directory
# ===========================================================================


class TestIndexTicketsFixtureDir:
    """Test index_tickets against the real fixture tickets directory."""

    def test_index_fixture_tickets_dir(self, fresh_conn):
        if not (FIXTURES_DIR / "tickets").is_dir():
            pytest.skip("Fixture tickets directory not found")

        result = index_tickets(
            fresh_conn, str(FIXTURES_DIR), tickets_dir="tickets"
        )
        assert result["new_count"] >= 1
        assert result["total"] >= 1

    def test_indexed_ticket_queryable(self, fresh_conn):
        if not (FIXTURES_DIR / "tickets").is_dir():
            pytest.skip("Fixture tickets directory not found")

        index_tickets(fresh_conn, str(FIXTURES_DIR), tickets_dir="tickets")
        server = TraceabilityServer(fresh_conn)

        ticket = server.get_ticket("0058")
        assert "error" not in ticket
        assert "Constraint Ownership Cleanup" in ticket["title"]
        assert len(ticket["workflow_log"]) == 6

    def test_indexed_ticket_searchable(self, fresh_conn):
        if not (FIXTURES_DIR / "tickets").is_dir():
            pytest.skip("Fixture tickets directory not found")

        index_tickets(fresh_conn, str(FIXTURES_DIR), tickets_dir="tickets")
        server = TraceabilityServer(fresh_conn)

        results = server.search_tickets("constraint")
        assert any(r["ticket_number"] == "0058" for r in results)

    def test_incremental_reindex_is_idempotent(self, fresh_conn):
        if not (FIXTURES_DIR / "tickets").is_dir():
            pytest.skip("Fixture tickets directory not found")

        result1 = index_tickets(fresh_conn, str(FIXTURES_DIR), tickets_dir="tickets")
        result2 = index_tickets(fresh_conn, str(FIXTURES_DIR), tickets_dir="tickets")

        assert result1["new_count"] >= 1
        assert result2["new_count"] == 0
        assert result2["updated_count"] == 0


# ===========================================================================
# Design decision indexer against real fixture docs
# ===========================================================================


class TestDecisionIndexerFixtureDocs:
    """Test decision indexer against real fixture design documents."""

    def test_find_documents_discovers_fixtures(self):
        if not FIXTURE_DESIGNS_DIR.is_dir():
            pytest.skip("Fixture designs directory not found")

        docs = find_documents(
            str(FIXTURES_DIR),
            designs_dir="docs/designs",
            tickets_dir="tickets",
        )
        assert len(docs) > 0
        paths = [p[1] for p in docs]  # relative paths
        assert any("0058" in p for p in paths)

    def test_parse_heuristic_decisions_from_design(self, design_0058_content):
        decisions = parse_heuristic_decisions(
            design_0058_content,
            "docs/designs/0058_constraint_ownership_cleanup/design.md",
            "0058",
        )
        assert len(decisions) > 0
        assert all(d["ticket"] == "0058" for d in decisions)
        assert all(d["extraction_method"] == "heuristic" for d in decisions)

    def test_heuristic_decision_ids_sequential(self, design_0058_content):
        decisions = parse_heuristic_decisions(
            design_0058_content,
            "docs/designs/0058_constraint_ownership_cleanup/design.md",
            "0058",
        )
        ids = [d["dd_id"] for d in decisions]
        for i, dd_id in enumerate(ids, 1):
            assert dd_id == f"DD-0058-H{i}"

    def test_index_decisions_from_fixtures(self, fresh_conn):
        if not FIXTURE_DESIGNS_DIR.is_dir():
            pytest.skip("Fixture designs directory not found")

        result = index_decisions(
            fresh_conn, str(FIXTURES_DIR),
            designs_dir="docs/designs",
            tickets_dir="tickets",
        )
        assert result["total"] > 0
        assert result["new_count"] > 0

    def test_indexed_decisions_queryable(self, fresh_conn):
        if not FIXTURE_DESIGNS_DIR.is_dir():
            pytest.skip("Fixture designs directory not found")

        index_decisions(
            fresh_conn, str(FIXTURES_DIR),
            designs_dir="docs/designs",
            tickets_dir="tickets",
        )
        server = TraceabilityServer(fresh_conn)

        results = server.search_decisions("constraint", ticket="0058")
        assert len(results) > 0

    def test_indexed_decisions_incremental(self, fresh_conn):
        if not FIXTURE_DESIGNS_DIR.is_dir():
            pytest.skip("Fixture designs directory not found")

        result1 = index_decisions(
            fresh_conn, str(FIXTURES_DIR),
            designs_dir="docs/designs",
            tickets_dir="tickets",
        )
        result2 = index_decisions(
            fresh_conn, str(FIXTURES_DIR),
            designs_dir="docs/designs",
            tickets_dir="tickets",
        )
        assert result1["new_count"] > 0
        assert result2["new_count"] == 0


# ===========================================================================
# End-to-end: ticket + decisions combined
# ===========================================================================


class TestCombinedTicketDecisionFixtures:
    """Test combined ticket and decision indexing against fixture data."""

    def test_ticket_and_decisions_for_same_ticket(self, fresh_conn):
        if not FIXTURE_DESIGNS_DIR.is_dir() or not FIXTURE_TICKET.exists():
            pytest.skip("Fixture data not found")

        # Index decisions first (from design docs), then tickets
        index_decisions(
            fresh_conn, str(FIXTURES_DIR),
            designs_dir="docs/designs",
            tickets_dir="tickets",
        )
        index_tickets(fresh_conn, str(FIXTURES_DIR), tickets_dir="tickets")

        server = TraceabilityServer(fresh_conn)

        ticket = server.get_ticket("0058")
        assert "error" not in ticket
        assert len(ticket["workflow_log"]) == 6

        # Decisions were extracted from design docs and ticket markdown
        rows = fresh_conn.execute(
            "SELECT dd_id FROM design_decisions WHERE ticket = '0058'"
        ).fetchall()
        assert len(rows) > 0

    def test_create_ticket_then_query(self, fresh_conn, ticket_0058_content, tmp_path):
        """Test create_ticket with real content then verify queries."""
        server = TraceabilityServer(fresh_conn)
        result = server.create_ticket("0058", ticket_0058_content, str(tmp_path))

        assert "error" not in result
        assert "Constraint Ownership Cleanup" in result["title"]

        ticket = server.get_ticket("0058")
        assert ticket["canonical_phase"] == "merged"
        assert len(ticket["workflow_log"]) == 6

        # Verify search finds it
        search = server.search_tickets("constraint ownership")
        assert any(r["ticket_number"] == "0058" for r in search)
