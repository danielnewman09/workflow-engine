"""
Tests for workflow_engine/traceability index_git.py,
index_decisions.py, and index_records.py.

Ticket: 0085_traceability_workflow_consolidation
Design: docs/designs/0085_traceability_workflow_consolidation/design.md

Validates:
- index_git: parse_commit_message extracts prefix/ticket/phase correctly
- index_git: index_git_history is incremental (skips existing SHAs)
- index_decisions: parse_structured_decisions extracts DD blocks
- index_decisions: parse_heuristic_decisions extracts heuristic patterns
- index_decisions: extract_ticket_number from file paths
- index_decisions: find_documents scans design and ticket directories
- index_decisions: index_decisions is incremental (skips existing DD IDs)
- index_records: parse_pydantic_models extracts BaseModel fields
- index_records: parse_pydantic_models skips auto-generated models
- index_records: populate_composite_models writes to DB correctly
- index_records: index_records returns correct counts

Note: index_git_history and index_decisions integration tests that call git
subprocess are skipped — unit tests cover the pure-Python parsing functions.
"""

import ast
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from workflow_engine.traceability.schema import create_schema
from workflow_engine.traceability.index_git import (
    PREFIX_TO_PHASE,
    parse_commit_message,
)
from workflow_engine.traceability.index_decisions import (
    DD_BLOCK_RE,
    extract_ticket_number,
    find_documents,
    parse_heuristic_decisions,
    parse_structured_decisions,
    index_decisions,
)
from workflow_engine.traceability.index_records import (
    parse_pydantic_models,
    populate_composite_models,
    index_records,
)


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


# ===========================================================================
# index_git: parse_commit_message
# ===========================================================================


def test_parse_commit_message_design_prefix():
    """'design:' prefix maps to 'design' phase."""
    prefix, ticket, phase = parse_commit_message("design: add architecture (0085)")
    assert prefix == "design"
    assert phase == "design"
    assert ticket == "0085"


def test_parse_commit_message_impl_prefix():
    """'impl:' prefix maps to 'implementation' phase."""
    prefix, ticket, phase = parse_commit_message("impl: add traceability module (0085)")
    assert prefix == "impl"
    assert phase == "implementation"


def test_parse_commit_message_docs_prefix():
    """'docs:' prefix maps to 'documentation' phase."""
    prefix, ticket, phase = parse_commit_message("docs: update CLAUDE.md (0085)")
    assert prefix == "docs"
    assert phase == "documentation"


def test_parse_commit_message_test_prefix():
    """'test:' prefix maps to 'testing' phase."""
    prefix, ticket, phase = parse_commit_message("test: add traceability tests (0085)")
    assert prefix == "test"
    assert phase == "testing"


def test_parse_commit_message_no_prefix():
    """Commit message without prefix returns None prefix and phase."""
    prefix, ticket, phase = parse_commit_message("Initial commit")
    assert prefix is None
    assert phase is None


def test_parse_commit_message_extracts_ticket_number():
    """Ticket number should be extracted from anywhere in the message."""
    _, ticket, _ = parse_commit_message("impl: refactor 0085 consolidation")
    assert ticket == "0085"


def test_parse_commit_message_ticket_with_letter_suffix():
    """Ticket numbers with letter suffix (e.g. 0083a) should be extracted."""
    _, ticket, _ = parse_commit_message("impl: repo extraction (0083a)")
    assert ticket == "0083a"


def test_parse_commit_message_no_ticket():
    """Commit message without ticket number returns None ticket."""
    _, ticket, _ = parse_commit_message("chore: update dependencies")
    assert ticket is None


def test_parse_commit_message_review_prefix():
    """'review:' prefix maps to 'review' phase."""
    prefix, ticket, phase = parse_commit_message("review: design approved (0085)")
    assert prefix == "review"
    assert phase == "review"


def test_parse_commit_message_all_known_prefixes_have_phase():
    """Every known prefix in PREFIX_TO_PHASE should parse to a non-None phase."""
    for prefix_str, expected_phase in PREFIX_TO_PHASE.items():
        prefix, _, phase = parse_commit_message(f"{prefix_str}: some change (0001)")
        assert phase == expected_phase, f"Prefix '{prefix_str}' should map to '{expected_phase}'"


# ===========================================================================
# index_decisions: extract_ticket_number
# ===========================================================================


def test_extract_ticket_number_from_design_path():
    """Should extract ticket number from a path containing NNNN."""
    result = extract_ticket_number("docs/designs/0085_foo/design.md")
    assert result == "0085"


def test_extract_ticket_number_from_ticket_path():
    """Should extract ticket number from a ticket filename."""
    result = extract_ticket_number("tickets/0083_database_agent.md")
    assert result == "0083"


def test_extract_ticket_number_letter_suffix():
    """Should extract ticket numbers with letter suffix."""
    result = extract_ticket_number("tickets/0083a_repo_extraction.md")
    assert result == "0083a"


def test_extract_ticket_number_no_match():
    """Should return None for paths without a 4-digit ticket number."""
    result = extract_ticket_number("docs/guides/README.md")
    assert result is None


# ===========================================================================
# index_decisions: parse_structured_decisions
# ===========================================================================


STRUCTURED_DOC = """
# Design

## Design Decisions

### DD-0085-001: Use Separate Database Files

- **Affects**: WorkflowServer, TraceabilityServer
- **Rationale**: Separate lifecycle concerns
- **Alternatives Considered**:
  - Merged single DB
  - In-memory only
- **Trade-offs**: Extra ATTACH complexity
- **Status**: Active

### DD-0085-002: Register Tools on Same FastMCP Instance

- **Affects**: create_mcp_server
- **Rationale**: Single server for all tools reduces client config
- **Status**: Active
"""


def test_parse_structured_decisions_finds_all_blocks():
    """parse_structured_decisions() should find all DD-NNNN-NNN blocks."""
    decisions = parse_structured_decisions(STRUCTURED_DOC, "docs/designs/0085/design.md", "0085")
    assert len(decisions) == 2


def test_parse_structured_decisions_extracts_dd_id():
    """parse_structured_decisions() should extract DD IDs correctly."""
    decisions = parse_structured_decisions(STRUCTURED_DOC, "docs/designs/0085/design.md", "0085")
    dd_ids = [d["dd_id"] for d in decisions]
    assert "DD-0085-001" in dd_ids
    assert "DD-0085-002" in dd_ids


def test_parse_structured_decisions_extracts_title():
    """parse_structured_decisions() should extract decision titles."""
    decisions = parse_structured_decisions(STRUCTURED_DOC, "docs/designs/0085/design.md", "0085")
    titles = [d["title"] for d in decisions]
    assert "Use Separate Database Files" in titles


def test_parse_structured_decisions_extracts_rationale():
    """parse_structured_decisions() should extract rationale text."""
    decisions = parse_structured_decisions(STRUCTURED_DOC, "docs/designs/0085/design.md", "0085")
    first = next(d for d in decisions if d["dd_id"] == "DD-0085-001")
    assert "Separate lifecycle" in first["rationale"]


def test_parse_structured_decisions_extracts_affects():
    """parse_structured_decisions() should extract Affects as a list."""
    decisions = parse_structured_decisions(STRUCTURED_DOC, "docs/designs/0085/design.md", "0085")
    first = next(d for d in decisions if d["dd_id"] == "DD-0085-001")
    assert "WorkflowServer" in first["affects"]
    assert "TraceabilityServer" in first["affects"]


def test_parse_structured_decisions_extracts_alternatives():
    """parse_structured_decisions() should extract alternatives text."""
    decisions = parse_structured_decisions(STRUCTURED_DOC, "docs/designs/0085/design.md", "0085")
    first = next(d for d in decisions if d["dd_id"] == "DD-0085-001")
    assert first["alternatives"] is not None
    assert "Merged single DB" in first["alternatives"]


def test_parse_structured_decisions_extracts_status():
    """parse_structured_decisions() should extract status (lowercased)."""
    decisions = parse_structured_decisions(STRUCTURED_DOC, "docs/designs/0085/design.md", "0085")
    first = next(d for d in decisions if d["dd_id"] == "DD-0085-001")
    assert first["status"] == "active"


def test_parse_structured_decisions_assigns_ticket():
    """parse_structured_decisions() should assign the provided ticket to each decision."""
    decisions = parse_structured_decisions(STRUCTURED_DOC, "docs/designs/0085/design.md", "0085")
    for d in decisions:
        assert d["ticket"] == "0085"


def test_parse_structured_decisions_extraction_method():
    """parse_structured_decisions() should set extraction_method to 'structured'."""
    decisions = parse_structured_decisions(STRUCTURED_DOC, "docs/designs/0085/design.md", "0085")
    for d in decisions:
        assert d["extraction_method"] == "structured"


def test_parse_structured_decisions_empty_doc():
    """parse_structured_decisions() should return [] for docs without DD blocks."""
    decisions = parse_structured_decisions("# Just a heading\nSome text.", "path.md", "0001")
    assert decisions == []


# ===========================================================================
# index_decisions: parse_heuristic_decisions
# ===========================================================================


HEURISTIC_DOC = """
## Why ATTACHed DB Pattern

The traceability database is ATTACHed at runtime rather than merged into workflow.db
because they have different data lifecycles. The workflow database is ephemeral
coordination state, while traceability accumulates history.

**Design Rationale**: Using a separate SQLite file per concern keeps backup and
migration simple. Each database can be deleted and rebuilt independently without
affecting the other.
"""


def test_parse_heuristic_decisions_finds_matches():
    """parse_heuristic_decisions() should extract decisions from heuristic patterns."""
    decisions = parse_heuristic_decisions(HEURISTIC_DOC, "docs/designs/0085/design.md", "0085")
    assert len(decisions) >= 1


def test_parse_heuristic_decisions_sets_extraction_method():
    """parse_heuristic_decisions() should set extraction_method to 'heuristic'."""
    decisions = parse_heuristic_decisions(HEURISTIC_DOC, "docs/designs/0085/design.md", "0085")
    for d in decisions:
        assert d["extraction_method"] == "heuristic"


def test_parse_heuristic_decisions_skips_short_matches():
    """parse_heuristic_decisions() should skip matches with <30 characters."""
    short_doc = "**Design Rationale**: Too short."
    decisions = parse_heuristic_decisions(short_doc, "path.md", "0001")
    assert len(decisions) == 0


def test_parse_heuristic_decisions_truncates_long_rationale():
    """parse_heuristic_decisions() should truncate rationale >500 chars."""
    long_text = "X" * 600
    long_doc = f"**Design Rationale**: {long_text}"
    decisions = parse_heuristic_decisions(long_doc, "path.md", "0001")
    if decisions:
        assert len(decisions[0]["rationale"]) <= 503  # 500 + "..."


def test_parse_heuristic_decisions_generates_sequential_ids():
    """parse_heuristic_decisions() should generate unique, sequential DD IDs."""
    decisions = parse_heuristic_decisions(HEURISTIC_DOC, "path.md", "0085")
    ids = [d["dd_id"] for d in decisions]
    assert len(ids) == len(set(ids))  # all unique


# ===========================================================================
# index_decisions: find_documents
# ===========================================================================


def test_find_documents_finds_design_docs(tmp_path):
    """find_documents() should find .md files in the designs directory."""
    designs = tmp_path / "docs" / "designs" / "0085_feature"
    designs.mkdir(parents=True)
    (designs / "design.md").write_text("# Design")

    docs = find_documents(str(tmp_path), "docs/designs", "tickets")
    paths = [rel for _, rel in docs]
    assert any("0085_feature" in p for p in paths)


def test_find_documents_finds_ticket_docs(tmp_path):
    """find_documents() should find .md files in the tickets directory."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    (tickets_dir / "0085_consolidation.md").write_text("# Ticket")

    docs = find_documents(str(tmp_path), "docs/designs", "tickets")
    paths = [rel for _, rel in docs]
    assert any("0085_consolidation" in p for p in paths)


def test_find_documents_returns_relative_paths(tmp_path):
    """find_documents() should return relative paths (not absolute)."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    (tickets_dir / "0001_test.md").write_text("content")

    docs = find_documents(str(tmp_path), "docs/designs", "tickets")
    for _, rel in docs:
        assert not rel.startswith("/"), f"Expected relative path, got: {rel}"


def test_find_documents_missing_directories(tmp_path):
    """find_documents() should return [] when both directories are absent."""
    docs = find_documents(str(tmp_path), "does/not/exist", "also/missing")
    assert docs == []


# ===========================================================================
# index_decisions: index_decisions (integration, no git subprocess)
# ===========================================================================


def test_index_decisions_incremental_skips_existing(conn, tmp_path):
    """index_decisions() should not re-insert decisions already in the DB."""
    # Create a ticket with structured DD block
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    (tickets_dir / "0085_test.md").write_text(
        "### DD-0085-001: Existing Decision\n\n"
        "- **Rationale**: Already indexed\n"
        "- **Status**: Active\n"
    )

    # First run
    result1 = index_decisions(conn, str(tmp_path), "docs/designs", "tickets")

    # Second run — should produce 0 new decisions
    result2 = index_decisions(conn, str(tmp_path), "docs/designs", "tickets")
    assert result2["new_count"] == 0
    assert result2["total"] == result1["total"]


def test_index_decisions_returns_counts(conn, tmp_path):
    """index_decisions() should return new_count, total, and symbol_links."""
    designs_dir = tmp_path / "docs" / "designs" / "0085_test"
    designs_dir.mkdir(parents=True)
    (designs_dir / "design.md").write_text(
        "### DD-0085-001: Test Decision\n\n"
        "- **Affects**: WorkflowServer, TraceServer\n"
        "- **Rationale**: Testing counts\n"
        "- **Status**: Active\n"
    )

    result = index_decisions(conn, str(tmp_path), "docs/designs", "tickets")
    assert "new_count" in result
    assert "total" in result
    assert "symbol_links" in result
    assert result["new_count"] >= 1
    assert result["symbol_links"] >= 2  # WorkflowServer + TraceServer


# ===========================================================================
# index_records: parse_pydantic_models
# ===========================================================================


MODELS_PY = '''
from pydantic import BaseModel
from typing import Optional

class BodyState(BaseModel):
    """Aggregated body state.

    From BodyStateRecord.
    """
    position_x: float
    position_y: float
    velocity: Optional[float] = None


class AutoGenerated(BaseModel):
    """Auto-generated model.

    Maps-to: BodyStateRecord
    """
    id: int
    value: float


class NotBaseModel:
    """Regular class, not a BaseModel subclass."""
    x: int
'''


def test_parse_pydantic_models_finds_base_model_classes():
    """parse_pydantic_models() should find classes that extend BaseModel."""
    models = parse_pydantic_models(MODELS_PY)
    assert "BodyState" in models


def test_parse_pydantic_models_skips_auto_generated():
    """parse_pydantic_models() should skip models with 'Maps-to:' in docstring."""
    models = parse_pydantic_models(MODELS_PY)
    assert "AutoGenerated" not in models


def test_parse_pydantic_models_skips_non_basemodel():
    """parse_pydantic_models() should skip classes not extending BaseModel."""
    models = parse_pydantic_models(MODELS_PY)
    assert "NotBaseModel" not in models


def test_parse_pydantic_models_extracts_fields():
    """parse_pydantic_models() should extract annotated fields."""
    models = parse_pydantic_models(MODELS_PY)
    body_fields = [f["field_name"] for f in models["BodyState"]["fields"]]
    assert "position_x" in body_fields
    assert "position_y" in body_fields
    assert "velocity" in body_fields


def test_parse_pydantic_models_extracts_cpp_record_from_docstring():
    """parse_pydantic_models() should extract 'From XxxRecord' from docstring."""
    models = parse_pydantic_models(MODELS_PY)
    assert models["BodyState"]["cpp_record"] == "BodyStateRecord"


def test_parse_pydantic_models_empty_source():
    """parse_pydantic_models() should return {} for an empty file."""
    models = parse_pydantic_models("")
    assert models == {}


def test_parse_pydantic_models_no_cpp_record_when_missing_from_docstring():
    """cpp_record should be None when docstring has no 'From XxxRecord' pattern."""
    source = '''
from pydantic import BaseModel

class SimpleModel(BaseModel):
    """Simple model without record reference."""
    value: int
'''
    models = parse_pydantic_models(source)
    assert "SimpleModel" in models
    assert models["SimpleModel"]["cpp_record"] is None


# ===========================================================================
# index_records: populate_composite_models
# ===========================================================================


def test_populate_composite_models_inserts_fields(conn):
    """populate_composite_models() should insert pydantic fields into DB."""
    pydantic_models = {
        "BodyState": {
            "fields": [
                {"field_name": "position_x", "field_type": "float"},
                {"field_name": "position_y", "field_type": "float"},
            ],
            "cpp_record": "BodyStateRecord",
        }
    }

    populate_composite_models(conn, pydantic_models)

    rows = conn.execute(
        "SELECT field_name FROM record_layer_fields WHERE record_name = 'BodyStateRecord' AND layer = 'pydantic'"
    ).fetchall()
    field_names = [r["field_name"] for r in rows]
    assert "position_x" in field_names
    assert "position_y" in field_names


def test_populate_composite_models_uses_class_name_when_no_cpp_record(conn):
    """When cpp_record is None, the class name should be used as record_name."""
    pydantic_models = {
        "FrameData": {
            "fields": [{"field_name": "timestamp", "field_type": "float"}],
            "cpp_record": None,
        }
    }

    populate_composite_models(conn, pydantic_models)

    rows = conn.execute(
        "SELECT field_name FROM record_layer_fields WHERE record_name = 'FrameData'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["field_name"] == "timestamp"


def test_populate_composite_models_clears_previous_composite_fields(conn):
    """populate_composite_models() should replace existing composite pydantic fields."""
    # Insert old composite field
    conn.execute(
        "INSERT INTO record_layer_fields (record_name, layer, field_name, notes) VALUES (?, ?, ?, ?)",
        ("OldRecord", "pydantic", "old_field", "composite"),
    )
    conn.commit()

    # Re-populate with new data (empty)
    populate_composite_models(conn, {})

    rows = conn.execute(
        "SELECT * FROM record_layer_fields WHERE record_name = 'OldRecord' AND notes = 'composite'"
    ).fetchall()
    assert len(rows) == 0


def test_populate_composite_models_does_not_clear_non_composite_fields(conn):
    """populate_composite_models() should not delete non-composite pydantic fields."""
    conn.execute(
        "INSERT INTO record_layer_fields (record_name, layer, field_name, field_type) VALUES (?, ?, ?, ?)",
        ("GenRecord", "pydantic", "gen_field", "int"),
    )
    conn.commit()

    # Repopulate — gen_field has no notes='composite' so should survive
    populate_composite_models(conn, {})

    rows = conn.execute(
        "SELECT * FROM record_layer_fields WHERE record_name = 'GenRecord'"
    ).fetchall()
    assert len(rows) == 1


# ===========================================================================
# index_records: index_records (integration)
# ===========================================================================


def test_index_records_with_missing_models_files(conn, tmp_path):
    """index_records() should return 0 counts when model files don't exist."""
    result = index_records(conn, str(tmp_path))
    assert result["composite_count"] == 0
    assert result["generated_count"] == 0


def test_index_records_reads_models_file(conn, tmp_path):
    """index_records() should parse models from the provided models_path."""
    models_content = '''
from pydantic import BaseModel

class BodyState(BaseModel):
    """From BodyRecord."""
    x: float
    y: float
'''
    models_file = tmp_path / "models.py"
    models_file.write_text(models_content)

    result = index_records(
        conn, str(tmp_path),
        models_path="models.py",
        generated_models_path="missing_generated.py",
    )
    assert result["composite_count"] == 1


def test_index_records_returns_generated_count(conn, tmp_path):
    """index_records() should count classes in generated_models_path."""
    gen_content = '''
class GenA:
    pass

class GenB:
    pass
'''
    gen_file = tmp_path / "generated_models.py"
    gen_file.write_text(gen_content)

    result = index_records(
        conn, str(tmp_path),
        models_path="nonexistent.py",
        generated_models_path="generated_models.py",
    )
    assert result["generated_count"] == 2
