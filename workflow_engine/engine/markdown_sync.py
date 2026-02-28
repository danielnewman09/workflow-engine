#!/usr/bin/env python3
# Ticket: 0083_database_agent_orchestration
# Design: docs/designs/0083_database_agent_orchestration/design.md
"""
Workflow Engine Markdown Synchronization

Bridges between file-based ticket markdown and the SQLite coordination database.

The markdown ticket is the CONTENT store (requirements, design decisions, feedback).
The database is the STATE/COORDINATION store (phase status, assignments, audit trail).

Import direction (markdown → DB):
- Parse ticket metadata (Priority, Languages, Requires Math Design, etc.)
- Parse checkbox status to determine current workflow position
- Create/update ticket and phase records in DB
- Idempotent — can be run repeatedly

Export direction (DB → markdown):
- Update the status checkboxes in the ticket markdown to match DB state
- Optionally update the Workflow Log section with phase timestamps and artifacts
- Preserve all human-authored content (requirements, design decisions, feedback)

Conflict resolution:
- DB is authoritative for status fields
- Markdown is authoritative for content fields
- If both have changed, log a warning and prefer DB status
"""

import re
import sqlite3
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Checkbox status parsing
# ---------------------------------------------------------------------------


def parse_status_checkboxes(content: str) -> dict[str, bool]:
    """
    Parse all status checkboxes from the ## Status section.

    Returns a dict mapping status label → checked (True/False).
    """
    statuses: dict[str, bool] = {}
    in_status = False

    for line in content.splitlines():
        stripped = line.strip()
        if stripped == "## Status":
            in_status = True
            continue
        if in_status and stripped.startswith("## "):
            break
        if in_status:
            # Checked: "- [x] Label"
            match = re.match(r"-\s+\[[xX]\]\s+(.+)", stripped)
            if match:
                statuses[match.group(1).strip()] = True
                continue
            # Unchecked: "- [ ] Label"
            match = re.match(r"-\s+\[\s\]\s+(.+)", stripped)
            if match:
                statuses[match.group(1).strip()] = False

    return statuses


def get_current_status(content: str) -> str:
    """
    Return the label of the last checked checkbox in the ## Status section.

    Returns "Draft" if no checkbox is checked.
    """
    last_checked = "Draft"
    in_status = False

    for line in content.splitlines():
        stripped = line.strip()
        if stripped == "## Status":
            in_status = True
            continue
        if in_status and stripped.startswith("## "):
            break
        if in_status:
            match = re.match(r"-\s+\[[xX]\]\s+(.+)", stripped)
            if match:
                last_checked = match.group(1).strip()

    return last_checked


# ---------------------------------------------------------------------------
# Metadata parsing
# ---------------------------------------------------------------------------


def parse_metadata(content: str) -> dict[str, Any]:
    """
    Parse the ## Metadata section of a ticket markdown file.

    Returns a dict of key → value for all "- **Key**: Value" lines.
    """
    metadata: dict[str, Any] = {}
    in_metadata = False

    for line in content.splitlines():
        stripped = line.strip()
        if stripped == "## Metadata":
            in_metadata = True
            continue
        if in_metadata and stripped.startswith("## "):
            break
        if in_metadata:
            match = re.match(r"-\s+\*\*(.+?)\*\*:\s*(.*)", stripped)
            if match:
                key = match.group(1).strip()
                value = match.group(2).strip()
                metadata[key] = value if value else None

    return metadata


# ---------------------------------------------------------------------------
# Status update (DB → markdown)
# ---------------------------------------------------------------------------


def update_status_in_markdown(
    content: str,
    new_status: str,
) -> str:
    """
    Update the ## Status section checkboxes to reflect new_status.

    Marks all statuses up to and including new_status as checked [x],
    and all subsequent statuses as unchecked [ ].

    Args:
        content: Full markdown content
        new_status: The new current status label to mark as the last checked item

    Returns:
        Updated markdown content string.
    """
    lines = content.splitlines(keepends=True)
    in_status = False
    found_target = False
    status_lines: list[int] = []  # line indices of status checkboxes

    # First pass: collect status checkbox line indices
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "## Status":
            in_status = True
            continue
        if in_status and stripped.startswith("## "):
            break
        if in_status and (
            re.match(r"-\s+\[[xX ]\]\s+.+", stripped)
        ):
            status_lines.append(i)

    # Second pass: determine which statuses should be checked
    # All statuses up to and including new_status are checked
    target_found = False
    checked_up_to: list[bool] = []

    for idx in status_lines:
        stripped = lines[idx].strip()
        match = re.match(r"-\s+\[[xX ]\]\s+(.+)", stripped)
        if match:
            label = match.group(1).strip()
            checked_up_to.append(not target_found)
            if label == new_status:
                target_found = True

    # Apply updates
    result_lines = list(lines)
    for i, line_idx in enumerate(status_lines):
        original = lines[line_idx]
        # Preserve indentation
        leading = original[: len(original) - len(original.lstrip())]
        stripped = original.strip()
        match = re.match(r"-\s+\[[xX ]\]\s+(.+)", stripped)
        if match:
            label = match.group(1).strip()
            checkbox = "[x]" if (i < len(checked_up_to) and checked_up_to[i]) else "[ ]"
            newline_char = "\n" if original.endswith("\n") else ""
            result_lines[line_idx] = f"{leading}- {checkbox} {label}{newline_char}"

    return "".join(result_lines)


def sync_status_to_file(
    markdown_path: str | Path,
    new_status: str,
) -> bool:
    """
    Update the status checkboxes in a ticket markdown file.

    Args:
        markdown_path: Path to the ticket .md file
        new_status: New status label to set as current

    Returns:
        True if the file was modified, False if no change needed.
    """
    path = Path(markdown_path)
    if not path.exists():
        return False

    original = path.read_text(encoding="utf-8")
    current = get_current_status(original)

    if current == new_status:
        return False

    updated = update_status_in_markdown(original, new_status)
    if updated == original:
        return False

    path.write_text(updated, encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# Workflow Log update (DB → markdown)
# ---------------------------------------------------------------------------


def update_workflow_log(
    content: str,
    phase_name: str,
    started_at: str | None,
    completed_at: str | None,
    artifacts: list[str] | None = None,
    notes: str | None = None,
    branch: str | None = None,
    pr: str | None = None,
) -> str:
    """
    Update the Workflow Log section for a completed phase.

    Finds the existing phase log entry and updates its timestamps and artifacts.
    If the phase entry doesn't exist, inserts a new one.

    Args:
        content: Full markdown content
        phase_name: Phase name (e.g. "Implementation")
        started_at: ISO datetime string
        completed_at: ISO datetime string
        artifacts: List of file paths produced
        notes: Optional notes text
        branch: Git branch name
        pr: PR reference (e.g. "#111 (draft)")

    Returns:
        Updated markdown content string.
    """
    # Build the new log entry
    lines_to_insert = [f"### {phase_name} Phase\n"]
    if started_at:
        lines_to_insert.append(f"- **Started**: {started_at}\n")
    if completed_at:
        lines_to_insert.append(f"- **Completed**: {completed_at}\n")
    if branch:
        lines_to_insert.append(f"- **Branch**: {branch}\n")
    if pr:
        lines_to_insert.append(f"- **PR**: {pr}\n")
    if artifacts:
        lines_to_insert.append("- **Artifacts**:\n")
        for artifact in artifacts:
            lines_to_insert.append(f"  - `{artifact}`\n")
    if notes:
        lines_to_insert.append(f"- **Notes**: {notes}\n")
    lines_to_insert.append("\n")

    new_entry = "".join(lines_to_insert)

    # Find existing entry for this phase in the Workflow Log section
    pattern = rf"(### {re.escape(phase_name)} Phase\n(?:(?!###)[^\n]*\n)*\n?)"
    existing_match = re.search(pattern, content)

    if existing_match:
        # Replace existing entry
        return content[: existing_match.start()] + new_entry + content[existing_match.end():]

    # No existing entry — append before the closing of Workflow Log section
    # Find the ## Human Feedback section (which follows Workflow Log)
    feedback_match = re.search(r"\n## Human Feedback\b", content)
    if feedback_match:
        insert_pos = feedback_match.start()
        return content[:insert_pos] + "\n" + new_entry + content[insert_pos:]

    # Fallback: append at end
    return content + "\n" + new_entry


def sync_workflow_log_to_file(
    conn: sqlite3.Connection,
    markdown_path: str | Path,
    ticket_id: str,
    branch: str | None = None,
    pr: str | None = None,
) -> bool:
    """
    Update the Workflow Log section of a ticket markdown file from the database.

    Args:
        conn: Open database connection
        markdown_path: Path to the ticket .md file
        ticket_id: Ticket ID to fetch phase data for
        branch: Git branch name (for log entries)
        pr: PR reference string

    Returns:
        True if the file was modified.
    """
    path = Path(markdown_path)
    if not path.exists():
        return False

    completed_phases = conn.execute(
        """
        SELECT phase_name, started_at, completed_at, artifacts, result_summary
        FROM phases
        WHERE ticket_id = ? AND status = 'completed'
        ORDER BY phase_order ASC, completed_at ASC
        """,
        (ticket_id,),
    ).fetchall()

    if not completed_phases:
        return False

    content = path.read_text(encoding="utf-8")
    modified = False

    for phase in completed_phases:
        artifacts = None
        if phase["artifacts"]:
            try:
                import json
                artifacts = json.loads(phase["artifacts"])
            except Exception:
                pass

        updated = update_workflow_log(
            content,
            phase_name=phase["phase_name"],
            started_at=phase["started_at"],
            completed_at=phase["completed_at"],
            artifacts=artifacts,
            notes=phase["result_summary"],
            branch=branch,
            pr=pr,
        )

        if updated != content:
            content = updated
            modified = True

    if modified:
        path.write_text(content, encoding="utf-8")

    return modified
