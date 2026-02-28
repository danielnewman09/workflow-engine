"""
Tests for engine/markdown_sync.py

Validates:
- parse_status_checkboxes correctly identifies checked/unchecked items
- get_current_status returns the last checked status
- parse_metadata extracts key-value pairs from ## Metadata section
- update_status_in_markdown updates checkboxes correctly
- sync_status_to_file writes changes to a file
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from workflow_engine.engine.markdown_sync import (
    get_current_status,
    parse_metadata,
    parse_status_checkboxes,
    sync_status_to_file,
    update_status_in_markdown,
    update_workflow_log,
)


# ---------------------------------------------------------------------------
# Sample markdown content
# ---------------------------------------------------------------------------

SAMPLE_TICKET = """\
# Feature Ticket: Test

## Status
- [x] Draft
- [x] Ready for Design
- [ ] Design Complete — Awaiting Review
- [ ] Ready for Implementation
- [ ] Merged / Complete

## Metadata
- **Priority**: High
- **Languages**: C++, Python
- **Estimated Complexity**: Large
- **GitHub Issue**: 42
- **Requires Math Design**: No
- **Generate Tutorial**: Yes

## Summary
Test content.

## Workflow Log

{This section is automatically updated}

## Human Feedback

{Add feedback here}
"""


# ---------------------------------------------------------------------------
# parse_status_checkboxes tests
# ---------------------------------------------------------------------------


def test_parse_checkboxes_detects_checked():
    statuses = parse_status_checkboxes(SAMPLE_TICKET)
    assert statuses["Draft"] is True
    assert statuses["Ready for Design"] is True


def test_parse_checkboxes_detects_unchecked():
    statuses = parse_status_checkboxes(SAMPLE_TICKET)
    assert statuses["Design Complete — Awaiting Review"] is False
    assert statuses["Ready for Implementation"] is False
    assert statuses["Merged / Complete"] is False


def test_parse_checkboxes_returns_all_statuses():
    statuses = parse_status_checkboxes(SAMPLE_TICKET)
    assert len(statuses) == 5


def test_parse_checkboxes_empty_status_section():
    content = "## Status\n\n## Metadata\n"
    statuses = parse_status_checkboxes(content)
    assert statuses == {}


# ---------------------------------------------------------------------------
# get_current_status tests
# ---------------------------------------------------------------------------


def test_get_current_status_returns_last_checked():
    assert get_current_status(SAMPLE_TICKET) == "Ready for Design"


def test_get_current_status_all_unchecked():
    content = """\
## Status
- [ ] Draft
- [ ] Ready for Design
"""
    assert get_current_status(content) == "Draft"  # fallback


def test_get_current_status_all_checked():
    content = """\
## Status
- [x] Draft
- [x] Ready for Design
- [x] Merged / Complete
"""
    assert get_current_status(content) == "Merged / Complete"


# ---------------------------------------------------------------------------
# parse_metadata tests
# ---------------------------------------------------------------------------


def test_parse_metadata_extracts_priority():
    meta = parse_metadata(SAMPLE_TICKET)
    assert meta["Priority"] == "High"


def test_parse_metadata_extracts_languages():
    meta = parse_metadata(SAMPLE_TICKET)
    assert meta["Languages"] == "C++, Python"


def test_parse_metadata_extracts_github_issue():
    meta = parse_metadata(SAMPLE_TICKET)
    assert meta["GitHub Issue"] == "42"


def test_parse_metadata_handles_empty_value():
    content = "## Metadata\n- **GitHub Issue**: \n"
    meta = parse_metadata(content)
    assert meta["GitHub Issue"] is None


def test_parse_metadata_stops_at_next_section():
    # Should not pick up items from ## Summary
    meta = parse_metadata(SAMPLE_TICKET)
    assert "Summary" not in meta
    assert "Test content." not in str(meta)


# ---------------------------------------------------------------------------
# update_status_in_markdown tests
# ---------------------------------------------------------------------------


def test_update_status_checks_up_to_target():
    updated = update_status_in_markdown(SAMPLE_TICKET, "Design Complete — Awaiting Review")

    statuses = parse_status_checkboxes(updated)
    assert statuses["Draft"] is True
    assert statuses["Ready for Design"] is True
    assert statuses["Design Complete — Awaiting Review"] is True
    assert statuses["Ready for Implementation"] is False
    assert statuses["Merged / Complete"] is False


def test_update_status_unchecks_after_target():
    # Start with all checked up to "Ready for Implementation"
    content_all = SAMPLE_TICKET.replace("[ ] Ready for Implementation", "[x] Ready for Implementation")
    updated = update_status_in_markdown(content_all, "Design Complete — Awaiting Review")

    statuses = parse_status_checkboxes(updated)
    assert statuses["Ready for Implementation"] is False


def test_update_status_final_status():
    updated = update_status_in_markdown(SAMPLE_TICKET, "Merged / Complete")

    statuses = parse_status_checkboxes(updated)
    assert all(statuses.values()), "All statuses should be checked"


def test_update_status_preserves_non_status_content():
    updated = update_status_in_markdown(SAMPLE_TICKET, "Ready for Implementation")
    assert "## Metadata" in updated
    assert "## Summary" in updated
    assert "Priority**: High" in updated
    assert "Test content." in updated


# ---------------------------------------------------------------------------
# sync_status_to_file tests
# ---------------------------------------------------------------------------


def test_sync_status_to_file_writes_change(tmp_path):
    ticket_file = tmp_path / "test_ticket.md"
    ticket_file.write_text(SAMPLE_TICKET, encoding="utf-8")

    changed = sync_status_to_file(ticket_file, "Design Complete — Awaiting Review")

    assert changed is True
    updated_content = ticket_file.read_text()
    statuses = parse_status_checkboxes(updated_content)
    assert statuses["Design Complete — Awaiting Review"] is True


def test_sync_status_to_file_no_op_when_same(tmp_path):
    ticket_file = tmp_path / "test_ticket.md"
    ticket_file.write_text(SAMPLE_TICKET, encoding="utf-8")

    # Status is already "Ready for Design"
    changed = sync_status_to_file(ticket_file, "Ready for Design")

    assert changed is False


def test_sync_status_to_file_nonexistent(tmp_path):
    nonexistent = tmp_path / "no_such_file.md"
    changed = sync_status_to_file(nonexistent, "Draft")
    assert changed is False


# ---------------------------------------------------------------------------
# update_workflow_log tests
# ---------------------------------------------------------------------------


def test_update_workflow_log_appends_entry():
    updated = update_workflow_log(
        SAMPLE_TICKET,
        phase_name="Implementation",
        started_at="2026-02-27 10:00",
        completed_at="2026-02-27 12:00",
        artifacts=["src/foo.cpp", "src/foo.h"],
        notes="Implementation complete",
        branch="0001-test-feature",
        pr="#42 (draft)",
    )

    assert "### Implementation Phase" in updated
    assert "2026-02-27 10:00" in updated
    assert "2026-02-27 12:00" in updated
    assert "src/foo.cpp" in updated
    assert "Implementation complete" in updated
    assert "0001-test-feature" in updated


def test_update_workflow_log_preserves_existing_content():
    updated = update_workflow_log(
        SAMPLE_TICKET,
        phase_name="Design",
        started_at="2026-02-27 09:00",
        completed_at="2026-02-27 10:00",
    )

    # Original content should still be present
    assert "## Human Feedback" in updated
    assert "Priority**: High" in updated
