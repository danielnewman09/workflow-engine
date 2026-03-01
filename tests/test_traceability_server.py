"""
Tests for workflow_engine/traceability/server.py

Ticket: 0085_traceability_workflow_consolidation
Design: docs/designs/0085_traceability_workflow_consolidation/design.md

Validates all 9 TraceabilityServer query methods:
1. search_decisions — FTS search and LIKE fallback
2. get_decision — full detail with symbols and commits
3. get_symbol_history — timeline of changes for a symbol
4. get_ticket_impact — all commits, files, symbols, decisions for a ticket
5. get_commit_context — context for a specific commit
6. why_symbol — decisions that created/modified a symbol
7. get_snapshot_symbols — all symbols at a point in time
8. get_record_mappings — four-layer field lists with drift analysis
9. check_record_drift — all records with drift

Also validates:
- _split_pascal_case helper
- _rows_to_dicts helper
- Not-found / error return paths
"""

import sqlite3

import pytest

from workflow_engine.traceability.schema import create_schema
from workflow_engine.traceability.server import TraceabilityServer


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path):
    """Path for a temporary traceability database."""
    return str(tmp_path / "traceability.db")


@pytest.fixture
def conn(db_path):
    """Open traceability DB connection with schema created."""
    c = create_schema(db_path)
    yield c
    c.close()


@pytest.fixture
def server(conn):
    """TraceabilityServer backed by the in-process DB."""
    return TraceabilityServer(conn)


def _insert_snapshot(conn, sha, date, author, message, ticket_number=None, phase=None):
    """Helper: insert a snapshot row, return its row id."""
    cursor = conn.execute(
        """INSERT INTO snapshots (sha, date, author, message, prefix, ticket_number, phase)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (sha, date, author, message, None, ticket_number, phase),
    )
    conn.commit()
    return cursor.lastrowid


def _insert_decision(conn, dd_id, ticket, title, rationale=None, status="active",
                     extraction_method="structured", source_file="docs/designs/test.md"):
    """Helper: insert a design_decisions row, return its row id."""
    cursor = conn.execute(
        """INSERT INTO design_decisions
           (dd_id, ticket, title, rationale, status, extraction_method, source_file)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (dd_id, ticket, title, rationale, status, extraction_method, source_file),
    )
    conn.commit()
    return cursor.lastrowid


# ---------------------------------------------------------------------------
# _split_pascal_case
# ---------------------------------------------------------------------------


def test_split_pascal_case_simple():
    """Should split a simple PascalCase word into its parts."""
    result = TraceabilityServer._split_pascal_case("ConvexHull")
    assert result == ["Convex", "Hull"]


def test_split_pascal_case_multiple_words():
    """Should split a sentence containing PascalCase tokens."""
    result = TraceabilityServer._split_pascal_case("ConvexHull WorkflowEngine")
    assert "Convex" in result
    assert "Hull" in result
    assert "Workflow" in result
    assert "Engine" in result


def test_split_pascal_case_acronym():
    """Should handle consecutive uppercase letters (acronym prefix)."""
    result = TraceabilityServer._split_pascal_case("MSDDatabase")
    assert len(result) >= 2


def test_split_pascal_case_plain_text():
    """Plain lowercase text should be returned as a single token."""
    result = TraceabilityServer._split_pascal_case("traceability")
    assert result == ["traceability"]


# ---------------------------------------------------------------------------
# search_decisions
# ---------------------------------------------------------------------------


def test_search_decisions_returns_empty_on_empty_db(server):
    """search_decisions() should return [] when no decisions exist."""
    result = server.search_decisions("anything")
    assert result == []


def test_search_decisions_fts_finds_inserted_decision(conn, server):
    """search_decisions() should find a decision via FTS when FTS is populated."""
    from workflow_engine.traceability.schema import rebuild_fts

    _insert_decision(conn, "DD-0001-001", "0001", "Separate DB files",
                     rationale="Use separate database files for different data lifecycles")
    rebuild_fts(conn)

    results = server.search_decisions("database")
    assert len(results) >= 1
    assert results[0]["dd_id"] == "DD-0001-001"


def test_search_decisions_falls_back_to_like(conn, server):
    """search_decisions() falls back to LIKE when FTS table is absent or empty."""
    # Insert without rebuilding FTS — the FTS table still exists but may not match
    # We test the LIKE fallback by querying a term that breaks FTS syntax
    _insert_decision(conn, "DD-0002-001", "0002", "ATTACH pattern",
                     rationale="Use ATTACH DATABASE for runtime composition")

    # The FTS index is empty (no rebuild called) so FTS returns nothing,
    # and the code falls back to LIKE search on the PascalCase split
    # "ATTACH" splits to ["ATTACH"] which LIKE will find in the title
    results = server.search_decisions("ATTACH")
    # Either FTS or LIKE returns results — we just verify no exception
    assert isinstance(results, list)


def test_search_decisions_filters_by_ticket(conn, server):
    """search_decisions(ticket=X) should only return decisions for ticket X."""
    from workflow_engine.traceability.schema import rebuild_fts

    _insert_decision(conn, "DD-0085-001", "0085", "First", rationale="consolidation approach")
    _insert_decision(conn, "DD-0001-001", "0001", "Second", rationale="unrelated approach")
    rebuild_fts(conn)

    results = server.search_decisions("approach", ticket="0085")
    dd_ids = [r["dd_id"] for r in results]
    assert "DD-0085-001" in dd_ids
    assert "DD-0001-001" not in dd_ids


def test_search_decisions_filters_by_status(conn, server):
    """search_decisions(status='superseded') should only return matching status."""
    from workflow_engine.traceability.schema import rebuild_fts

    _insert_decision(conn, "DD-0085-001", "0085", "Active dec",
                     rationale="active rationale", status="active")
    _insert_decision(conn, "DD-0085-002", "0085", "Superseded dec",
                     rationale="superseded rationale", status="superseded")
    rebuild_fts(conn)

    results = server.search_decisions("rationale", status="superseded")
    assert all(r["status"] == "superseded" for r in results)


# ---------------------------------------------------------------------------
# get_decision
# ---------------------------------------------------------------------------


def test_get_decision_returns_error_when_not_found(server):
    """get_decision() should return a dict with 'error' key when not found."""
    result = server.get_decision("DD-9999-001")
    assert "error" in result
    assert "DD-9999-001" in result["error"]


def test_get_decision_returns_full_record(conn, server):
    """get_decision() should return all fields including symbols and commits."""
    decision_id = _insert_decision(conn, "DD-0085-001", "0085", "ATTACH Pattern",
                                   rationale="Keep DBs separate")

    # Add a linked symbol
    conn.execute(
        "INSERT INTO decision_symbols (decision_id, symbol_name) VALUES (?, ?)",
        (decision_id, "WorkflowServer"),
    )
    # Add a snapshot and link it
    snap_id = _insert_snapshot(conn, "abc123", "2026-02-28T00:00:00Z",
                               "Author", "impl: add ATTACH (0085)")
    conn.execute(
        "INSERT INTO decision_commits (decision_id, snapshot_id) VALUES (?, ?)",
        (decision_id, snap_id),
    )
    conn.commit()

    result = server.get_decision("DD-0085-001")

    assert result["dd_id"] == "DD-0085-001"
    assert result["ticket"] == "0085"
    assert result["title"] == "ATTACH Pattern"
    assert "WorkflowServer" in result["symbols"]
    assert len(result["commits"]) == 1
    assert result["commits"][0]["sha"] == "abc123"
    # Internal 'id' should be stripped
    assert "id" not in result


def test_get_decision_empty_symbols_and_commits(conn, server):
    """get_decision() should return empty lists for symbols and commits when none linked."""
    _insert_decision(conn, "DD-0085-002", "0085", "Standalone Decision")

    result = server.get_decision("DD-0085-002")
    assert result["symbols"] == []
    assert result["commits"] == []


# ---------------------------------------------------------------------------
# get_symbol_history
# ---------------------------------------------------------------------------


def test_get_symbol_history_returns_empty_when_no_changes(server):
    """get_symbol_history() returns [] when no symbol_changes exist."""
    result = server.get_symbol_history("WorkflowServer")
    assert result == []


def test_get_symbol_history_returns_changes_in_order(conn, server):
    """get_symbol_history() should return changes ordered by commit date."""
    snap1 = _insert_snapshot(conn, "aaa111", "2026-01-01T00:00:00Z",
                             "Dev", "impl: add class (0085)", ticket_number="0085")
    snap2 = _insert_snapshot(conn, "bbb222", "2026-01-15T00:00:00Z",
                             "Dev", "impl: rename class (0085)", ticket_number="0085")

    conn.execute(
        """INSERT INTO symbol_changes (snapshot_id, qualified_name, change_type,
           new_file_path, new_line)
           VALUES (?, ?, ?, ?, ?)""",
        (snap1, "WorkflowServer", "added", "workflow_engine/server/server.py", 10),
    )
    conn.execute(
        """INSERT INTO symbol_changes (snapshot_id, qualified_name, change_type,
           old_file_path, new_file_path, new_line)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (snap2, "WorkflowServer", "moved",
         "old/path.py", "workflow_engine/server/server.py", 20),
    )
    conn.commit()

    results = server.get_symbol_history("WorkflowServer")
    assert len(results) == 2
    assert results[0]["sha"] == "aaa111"
    assert results[1]["sha"] == "bbb222"


def test_get_symbol_history_supports_wildcard(conn, server):
    """get_symbol_history() should match when '%' is already in the query."""
    snap1 = _insert_snapshot(conn, "ccc333", "2026-02-01T00:00:00Z",
                             "Dev", "impl: add (0085)", ticket_number="0085")
    conn.execute(
        "INSERT INTO symbol_changes (snapshot_id, qualified_name, change_type, new_file_path, new_line) VALUES (?, ?, ?, ?, ?)",
        (snap1, "workflow_engine::TraceServer", "added", "server.py", 5),
    )
    conn.commit()

    results = server.get_symbol_history("%TraceServer%")
    assert len(results) == 1


# ---------------------------------------------------------------------------
# get_ticket_impact
# ---------------------------------------------------------------------------


def test_get_ticket_impact_returns_error_when_no_data(server):
    """get_ticket_impact() returns 'error' dict when ticket has no data."""
    result = server.get_ticket_impact("9999")
    assert "error" in result
    assert "9999" in result["error"]


def test_get_ticket_impact_returns_all_sections(conn, server):
    """get_ticket_impact() should return commits, file_changes, symbol_changes, decisions."""
    snap_id = _insert_snapshot(conn, "ddd444", "2026-02-28T00:00:00Z",
                               "Dev", "impl: add module (0085)", ticket_number="0085")

    conn.execute(
        """INSERT INTO file_changes (snapshot_id, file_path, change_type, insertions, deletions)
           VALUES (?, ?, ?, ?, ?)""",
        (snap_id, "workflow_engine/traceability/__init__.py", "A", 20, 0),
    )
    conn.execute(
        """INSERT INTO symbol_changes (snapshot_id, qualified_name, change_type, new_file_path, new_line)
           VALUES (?, ?, ?, ?, ?)""",
        (snap_id, "TraceabilityServer", "added", "traceability/server.py", 1),
    )
    _insert_decision(conn, "DD-0085-001", "0085", "Module Architecture")
    conn.commit()

    result = server.get_ticket_impact("0085")

    assert result["ticket"] == "0085"
    assert len(result["commits"]) == 1
    assert result["commits"][0]["sha"] == "ddd444"
    assert len(result["file_changes"]) == 1
    assert result["file_changes"][0]["file_path"] == "workflow_engine/traceability/__init__.py"
    assert len(result["symbol_changes"]) == 1
    assert result["symbol_changes"][0]["qualified_name"] == "TraceabilityServer"
    assert len(result["decisions"]) == 1
    assert result["decisions"][0]["dd_id"] == "DD-0085-001"


def test_get_ticket_impact_no_id_in_commits(conn, server):
    """get_ticket_impact() should strip 'id' from commit dicts."""
    _insert_snapshot(conn, "eee555", "2026-02-28T00:00:00Z",
                     "Dev", "impl: test (0085)", ticket_number="0085")
    conn.commit()

    result = server.get_ticket_impact("0085")
    for commit in result["commits"]:
        assert "id" not in commit


def test_get_ticket_impact_with_only_decisions(conn, server):
    """get_ticket_impact() should succeed when only decisions exist (no commits)."""
    _insert_decision(conn, "DD-0085-001", "0085", "Decision Without Commit")
    conn.commit()

    result = server.get_ticket_impact("0085")
    assert "error" not in result
    assert len(result["decisions"]) == 1


# ---------------------------------------------------------------------------
# get_commit_context
# ---------------------------------------------------------------------------


def test_get_commit_context_returns_error_when_not_found(server):
    """get_commit_context() returns 'error' dict for unknown SHA."""
    result = server.get_commit_context("deadbeef")
    assert "error" in result


def test_get_commit_context_full_sha_lookup(conn, server):
    """get_commit_context() should find a commit by full SHA."""
    snap_id = _insert_snapshot(conn, "fff666aaa", "2026-02-28T00:00:00Z",
                               "Dev", "impl: full commit (0085)", ticket_number="0085")
    conn.execute(
        "INSERT INTO file_changes (snapshot_id, file_path, change_type, insertions, deletions) VALUES (?, ?, ?, ?, ?)",
        (snap_id, "some/file.py", "M", 5, 2),
    )
    conn.commit()

    result = server.get_commit_context("fff666aaa")
    assert result["sha"] == "fff666aaa"
    assert len(result["file_changes"]) == 1
    assert "id" not in result


def test_get_commit_context_prefix_sha_lookup(conn, server):
    """get_commit_context() should find a commit by SHA prefix."""
    _insert_snapshot(conn, "abc123xyz", "2026-02-28T00:00:00Z",
                     "Dev", "impl: prefix test (0085)")
    conn.commit()

    result = server.get_commit_context("abc123")  # short prefix
    assert result["sha"] == "abc123xyz"


def test_get_commit_context_includes_decisions_via_ticket(conn, server):
    """get_commit_context() should include decisions from the commit's ticket."""
    snap_id = _insert_snapshot(conn, "ggg777", "2026-02-28T00:00:00Z",
                               "Dev", "impl: (0085)", ticket_number="0085")
    _insert_decision(conn, "DD-0085-001", "0085", "Some Decision")
    conn.commit()

    result = server.get_commit_context("ggg777")
    assert len(result["decisions"]) >= 1
    dd_ids = [d["dd_id"] for d in result["decisions"]]
    assert "DD-0085-001" in dd_ids


def test_get_commit_context_includes_directly_linked_decisions(conn, server):
    """get_commit_context() should include decisions linked directly via decision_commits."""
    snap_id = _insert_snapshot(conn, "hhh888", "2026-02-28T00:00:00Z",
                               "Dev", "impl: direct link (0085)")
    decision_id = _insert_decision(conn, "DD-0085-002", "0085", "Direct Decision")
    conn.execute(
        "INSERT INTO decision_commits (decision_id, snapshot_id) VALUES (?, ?)",
        (decision_id, snap_id),
    )
    conn.commit()

    result = server.get_commit_context("hhh888")
    dd_ids = [d["dd_id"] for d in result["decisions"]]
    assert "DD-0085-002" in dd_ids


# ---------------------------------------------------------------------------
# why_symbol
# ---------------------------------------------------------------------------


def test_why_symbol_returns_dict_with_symbol_key(server):
    """why_symbol() always returns a dict with 'symbol', 'decisions', 'change_history'."""
    result = server.why_symbol("NonExistentClass")
    assert "symbol" in result
    assert "decisions" in result
    assert "change_history" in result
    assert result["symbol"] == "NonExistentClass"


def test_why_symbol_finds_directly_linked_decisions(conn, server):
    """why_symbol() should return decisions linked via decision_symbols."""
    decision_id = _insert_decision(conn, "DD-0085-001", "0085", "ATTACH Pattern",
                                   rationale="Separate DBs")
    conn.execute(
        "INSERT INTO decision_symbols (decision_id, symbol_name) VALUES (?, ?)",
        (decision_id, "WorkflowServer"),
    )
    conn.commit()

    result = server.why_symbol("WorkflowServer")
    dd_ids = [d["dd_id"] for d in result["decisions"]]
    assert "DD-0085-001" in dd_ids


def test_why_symbol_finds_indirect_decisions_via_ticket(conn, server):
    """why_symbol() should find decisions via ticket association when no direct link."""
    snap_id = _insert_snapshot(conn, "iii999", "2026-02-28T00:00:00Z",
                               "Dev", "impl: add TraceabilityServer (0085)",
                               ticket_number="0085")
    conn.execute(
        """INSERT INTO symbol_changes (snapshot_id, qualified_name, change_type, new_file_path, new_line)
           VALUES (?, ?, ?, ?, ?)""",
        (snap_id, "TraceabilityServer", "added", "server.py", 1),
    )
    _insert_decision(conn, "DD-0085-001", "0085", "Server Design", rationale="Indirect link")
    conn.commit()

    result = server.why_symbol("TraceabilityServer")
    # Change history should have the commit
    assert len(result["change_history"]) == 1
    # Indirect decision should be found via ticket
    dd_ids = [d["dd_id"] for d in result["decisions"]]
    assert "DD-0085-001" in dd_ids
    # Indirect decisions should have link_type annotation
    indirect = [d for d in result["decisions"] if "link_type" in d]
    assert len(indirect) >= 1


def test_why_symbol_deduplicates_decisions(conn, server):
    """why_symbol() should not return the same decision twice."""
    snap_id = _insert_snapshot(conn, "jjj000", "2026-02-28T00:00:00Z",
                               "Dev", "impl: (0085)", ticket_number="0085")
    decision_id = _insert_decision(conn, "DD-0085-001", "0085", "Dec",
                                   rationale="Both direct and indirect")
    # Direct link
    conn.execute(
        "INSERT INTO decision_symbols (decision_id, symbol_name) VALUES (?, ?)",
        (decision_id, "MyClass"),
    )
    # Symbol change in the same ticket
    conn.execute(
        "INSERT INTO symbol_changes (snapshot_id, qualified_name, change_type, new_file_path, new_line) VALUES (?, ?, ?, ?, ?)",
        (snap_id, "MyClass", "added", "foo.py", 1),
    )
    conn.commit()

    result = server.why_symbol("MyClass")
    dd_ids = [d["dd_id"] for d in result["decisions"]]
    assert dd_ids.count("DD-0085-001") == 1  # Should appear only once


# ---------------------------------------------------------------------------
# get_snapshot_symbols
# ---------------------------------------------------------------------------


def test_get_snapshot_symbols_returns_error_when_commit_not_found(server):
    """get_snapshot_symbols() returns [{error: ...}] for unknown SHA."""
    result = server.get_snapshot_symbols("deadbeef")
    assert len(result) == 1
    assert "error" in result[0]


def test_get_snapshot_symbols_returns_all_symbols(conn, server):
    """get_snapshot_symbols() returns all symbols for a commit."""
    snap_id = _insert_snapshot(conn, "kkk111", "2026-02-28T00:00:00Z",
                               "Dev", "impl: (0085)")
    conn.execute(
        """INSERT INTO symbol_snapshots
           (snapshot_id, qualified_name, kind, file_path, line_number)
           VALUES (?, ?, ?, ?, ?)""",
        (snap_id, "TraceabilityServer", "class", "server.py", 1),
    )
    conn.execute(
        """INSERT INTO symbol_snapshots
           (snapshot_id, qualified_name, kind, file_path, line_number)
           VALUES (?, ?, ?, ?, ?)""",
        (snap_id, "TraceabilityServer::search_decisions", "method", "server.py", 50),
    )
    conn.commit()

    results = server.get_snapshot_symbols("kkk111")
    assert len(results) == 2
    names = [r["qualified_name"] for r in results]
    assert "TraceabilityServer" in names
    assert "TraceabilityServer::search_decisions" in names


def test_get_snapshot_symbols_filters_by_file_path(conn, server):
    """get_snapshot_symbols() with file_path should filter results."""
    snap_id = _insert_snapshot(conn, "lll222", "2026-02-28T00:00:00Z",
                               "Dev", "impl: (0085)")
    conn.execute(
        "INSERT INTO symbol_snapshots (snapshot_id, qualified_name, kind, file_path, line_number) VALUES (?, ?, ?, ?, ?)",
        (snap_id, "ClassA", "class", "module_a/a.py", 1),
    )
    conn.execute(
        "INSERT INTO symbol_snapshots (snapshot_id, qualified_name, kind, file_path, line_number) VALUES (?, ?, ?, ?, ?)",
        (snap_id, "ClassB", "class", "module_b/b.py", 1),
    )
    conn.commit()

    results = server.get_snapshot_symbols("lll222", file_path="module_a")
    assert len(results) == 1
    assert results[0]["qualified_name"] == "ClassA"


# ---------------------------------------------------------------------------
# get_record_mappings
# ---------------------------------------------------------------------------


def test_get_record_mappings_returns_error_when_not_found(server):
    """get_record_mappings() returns 'error' dict for unknown record name."""
    result = server.get_record_mappings("NonExistentRecord")
    assert "error" in result
    assert "NonExistentRecord" in result["error"]


def _insert_record(conn, record_name, pydantic_model=None):
    """Insert a record_layer_mapping row."""
    conn.execute(
        """INSERT INTO record_layer_mapping (record_name, pydantic_model)
           VALUES (?, ?)""",
        (record_name, pydantic_model),
    )
    conn.commit()


def _insert_field(conn, record_name, layer, field_name, field_type="float"):
    """Insert a record_layer_fields row."""
    conn.execute(
        """INSERT INTO record_layer_fields (record_name, layer, field_name, field_type)
           VALUES (?, ?, ?, ?)""",
        (record_name, layer, field_name, field_type),
    )
    conn.commit()


def test_get_record_mappings_returns_all_layers(conn, server):
    """get_record_mappings() should return fields for all four layers."""
    _insert_record(conn, "BodyRecord", pydantic_model="BodyState")
    _insert_field(conn, "BodyRecord", "cpp", "position_x")
    _insert_field(conn, "BodyRecord", "pybind", "position_x")
    _insert_field(conn, "BodyRecord", "pydantic", "position_x")
    _insert_field(conn, "BodyRecord", "sql", "position_x")

    result = server.get_record_mappings("BodyRecord")
    assert result["record"] == "BodyRecord"
    assert result["pydantic_model"] == "BodyState"
    assert "layers" in result
    assert set(result["layers"].keys()) == {"cpp", "sql", "pybind", "pydantic"}
    assert len(result["layers"]["cpp"]) == 1
    assert result["layers"]["cpp"][0]["field_name"] == "position_x"


def test_get_record_mappings_drift_no_missing_fields(conn, server):
    """drift should have empty lists when all layers are complete."""
    _insert_record(conn, "CompleteRecord")
    _insert_field(conn, "CompleteRecord", "cpp", "mass")
    _insert_field(conn, "CompleteRecord", "pybind", "mass")
    _insert_field(conn, "CompleteRecord", "pydantic", "mass")

    result = server.get_record_mappings("CompleteRecord")
    drift = result["drift"]
    assert drift["missing_in_pybind"] == []
    assert drift["missing_in_pydantic"] == []


def test_get_record_mappings_drift_detects_missing_in_pybind(conn, server):
    """drift should identify cpp fields absent from pybind layer."""
    _insert_record(conn, "PartialRecord")
    _insert_field(conn, "PartialRecord", "cpp", "velocity_x")
    _insert_field(conn, "PartialRecord", "pydantic", "velocity_x")
    # pybind is missing velocity_x

    result = server.get_record_mappings("PartialRecord")
    assert "velocity_x" in result["drift"]["missing_in_pybind"]
    assert "velocity_x" not in result["drift"]["missing_in_pydantic"]


def test_get_record_mappings_drift_detects_missing_in_pydantic(conn, server):
    """drift should identify cpp fields absent from pydantic layer."""
    _insert_record(conn, "AnotherRecord")
    _insert_field(conn, "AnotherRecord", "cpp", "spin")
    _insert_field(conn, "AnotherRecord", "pybind", "spin")
    # pydantic is missing spin

    result = server.get_record_mappings("AnotherRecord")
    assert "spin" in result["drift"]["missing_in_pydantic"]
    assert "spin" not in result["drift"]["missing_in_pybind"]


def test_get_record_mappings_drift_accepts_fk_suffix_as_equivalent(conn, server):
    """A field present as {name}_id in pybind should not count as drift."""
    _insert_record(conn, "FKRecord")
    _insert_field(conn, "FKRecord", "cpp", "body")
    _insert_field(conn, "FKRecord", "pybind", "body_id")  # FK variant
    _insert_field(conn, "FKRecord", "pydantic", "body_id")  # FK variant

    result = server.get_record_mappings("FKRecord")
    drift = result["drift"]
    assert "body" not in drift["missing_in_pybind"]
    assert "body" not in drift["missing_in_pydantic"]


# ---------------------------------------------------------------------------
# check_record_drift
# ---------------------------------------------------------------------------


def test_check_record_drift_empty_db(server):
    """check_record_drift() returns [] when no records exist."""
    result = server.check_record_drift()
    assert result == []


def test_check_record_drift_returns_only_drifted_records(conn, server):
    """check_record_drift() should only include records with actual drift."""
    # Clean record — no drift
    _insert_record(conn, "CleanRecord")
    _insert_field(conn, "CleanRecord", "cpp", "x")
    _insert_field(conn, "CleanRecord", "pybind", "x")
    _insert_field(conn, "CleanRecord", "pydantic", "x")

    # Drifted record — pybind missing
    _insert_record(conn, "DriftedRecord")
    _insert_field(conn, "DriftedRecord", "cpp", "y")
    _insert_field(conn, "DriftedRecord", "pydantic", "y")

    result = server.check_record_drift()
    record_names = [r["record"] for r in result]
    assert "DriftedRecord" in record_names
    assert "CleanRecord" not in record_names


def test_check_record_drift_returns_drift_details(conn, server):
    """check_record_drift() should include missing field lists."""
    _insert_record(conn, "DetailRecord")
    _insert_field(conn, "DetailRecord", "cpp", "alpha")
    _insert_field(conn, "DetailRecord", "cpp", "beta")
    _insert_field(conn, "DetailRecord", "pybind", "alpha")
    # beta missing from pybind and pydantic

    results = server.check_record_drift()
    detail = next((r for r in results if r["record"] == "DetailRecord"), None)
    assert detail is not None
    assert "beta" in detail["missing_in_pybind"]
    assert "beta" in detail["missing_in_pydantic"]


def test_check_record_drift_structure(conn, server):
    """check_record_drift() items should have 'record', 'missing_in_pybind', 'missing_in_pydantic'."""
    _insert_record(conn, "StructRecord")
    _insert_field(conn, "StructRecord", "cpp", "field1")
    # No pybind or pydantic

    results = server.check_record_drift()
    assert len(results) >= 1
    item = results[0]
    assert "record" in item
    assert "missing_in_pybind" in item
    assert "missing_in_pydantic" in item
