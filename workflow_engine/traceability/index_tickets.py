"""
Ticket Content Indexer

Parses ticket markdown files and stores their content, metadata,
acceptance criteria, workflow log, file declarations, and cross-references
into the traceability database.

Reuses parse_status_checkboxes, get_current_status, and parse_metadata
from workflow_engine.engine.markdown_sync.
"""

import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from workflow_engine.engine.markdown_sync import (
    get_current_status,
    parse_metadata,
    parse_status_checkboxes,
)

# ---------------------------------------------------------------------------
# Canonical phase mapping
# ---------------------------------------------------------------------------

# Maps substring patterns (lowercased) in status checkbox labels to canonical phases.
# Order matters: first match wins.
_PHASE_MAP: list[tuple[str, str]] = [
    ("merged", "merged"),
    ("documentation complete", "documentation"),
    ("test writing complete", "testing"),
    ("quality gate", "quality_gate"),
    ("implementation complete", "implementation"),
    ("ready for implementation", "prototype"),
    ("prototype complete", "prototype"),
    ("design approved", "design_review"),
    ("design complete", "design"),
    ("math review", "math"),
    ("math formulation", "math"),
    ("ready for design", "draft"),
    ("ready for math", "draft"),
    ("approved", "review"),
    ("draft", "draft"),
]


def _map_canonical_phase(raw_status: str) -> str:
    """Map a raw status label to a canonical phase."""
    lower = raw_status.lower()
    for pattern, phase in _PHASE_MAP:
        if pattern in lower:
            return phase
    return "draft"


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_title(content: str) -> str:
    """Extract ticket title from the first heading."""
    for line in content.splitlines():
        stripped = line.strip()
        # "# Feature Ticket: <title>" or "# Ticket NNNN: <title>" or just "# <title>"
        m = re.match(r"^#\s+(?:Feature\s+)?(?:Ticket:?\s*(?:\d{4}[a-z]?:?\s*)?)?(.+)", stripped, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return "Untitled"


def _extract_section(content: str, heading: str) -> str | None:
    """Extract text under a ## heading, stopping at the next ## heading."""
    pattern = rf"^## {re.escape(heading)}\s*$"
    lines = content.splitlines()
    collecting = False
    section_lines: list[str] = []

    for line in lines:
        if re.match(pattern, line.strip()):
            collecting = True
            continue
        if collecting and line.strip().startswith("## "):
            break
        if collecting:
            section_lines.append(line)

    if not section_lines:
        return None

    text = "\n".join(section_lines).strip()
    return text if text else None


def _parse_summary(content: str) -> str | None:
    """Extract ## Summary section text."""
    return _extract_section(content, "Summary")


def _parse_acceptance_criteria(content: str) -> list[dict[str, Any]]:
    """Extract acceptance criteria checkboxes with optional IDs and categories."""
    section = _extract_section(content, "Acceptance Criteria")
    if not section:
        return []

    criteria: list[dict[str, Any]] = []
    current_category: str | None = None

    for line in section.splitlines():
        stripped = line.strip()

        # Detect category sub-headings like "### Math Formulation" or "### Implementation"
        cat_match = re.match(r"^###\s+(.+)", stripped)
        if cat_match:
            current_category = cat_match.group(1).strip()
            continue

        # Checked: "- [x] AC1: Description" or "- [x] Description"
        m = re.match(r"-\s+\[([xX ])\]\s+(?:([A-Z]+\d+):\s+)?(.+)", stripped)
        if m:
            is_met = m.group(1).lower() == "x"
            criterion_id = m.group(2)
            description = m.group(3).strip()
            criteria.append({
                "criterion_id": criterion_id,
                "description": description,
                "is_met": is_met,
                "category": current_category,
            })

    return criteria


def _parse_workflow_log(content: str) -> list[dict[str, Any]]:
    """Extract workflow log entries from ## Workflow Log section."""
    section = _extract_section(content, "Workflow Log")
    if not section:
        return []

    entries: list[dict[str, Any]] = []
    current_entry: dict[str, Any] | None = None

    for line in section.splitlines():
        stripped = line.strip()

        # Phase heading: "### Implementation Phase"
        phase_match = re.match(r"^###\s+(.+?)\s+Phase\s*$", stripped)
        if phase_match:
            if current_entry:
                entries.append(current_entry)
            current_entry = {
                "phase_name": phase_match.group(1).strip(),
                "started_at": None,
                "completed_at": None,
                "branch": None,
                "pr_url": None,
                "status": None,
                "notes": None,
                "artifacts": [],
            }
            continue

        if current_entry is None:
            continue

        # Key-value: "- **Key**: Value"
        kv_match = re.match(r"-\s+\*\*(.+?)\*\*:\s*(.*)", stripped)
        if kv_match:
            key = kv_match.group(1).strip().lower()
            value = kv_match.group(2).strip()
            if key == "started":
                current_entry["started_at"] = value or None
            elif key == "completed":
                current_entry["completed_at"] = value or None
            elif key == "branch":
                current_entry["branch"] = value or None
            elif key == "pr":
                current_entry["pr_url"] = value or None
            elif key == "status" or key == "result":
                current_entry["status"] = value or None
            elif key == "notes":
                current_entry["notes"] = value or None
            elif key == "artifacts":
                pass  # artifacts follow on indented lines
            continue

        # Artifact list item: "  - `path/to/file`"
        artifact_match = re.match(r"-\s+`(.+?)`", stripped)
        if artifact_match and current_entry:
            current_entry["artifacts"].append(artifact_match.group(1))

    if current_entry:
        entries.append(current_entry)

    return entries


def _parse_files(content: str) -> list[dict[str, Any]]:
    """Extract file declarations from ## Files section and sub-sections."""
    files: list[dict[str, Any]] = []

    # Map of subsection heading patterns to change types
    heading_to_type = {
        "new files": "new",
        "new": "new",
        "modified files": "modified",
        "modified": "modified",
        "removed files": "removed",
        "removed": "removed",
        "deleted files": "removed",
    }

    # Extract the ## Files section first
    files_section = _extract_section(content, "Files")
    if files_section is None:
        # Try ## New Files, ## Modified Files as top-level sections
        for change_type, headings in [
            ("new", ["New Files"]),
            ("modified", ["Modified Files"]),
            ("removed", ["Removed Files"]),
        ]:
            section = _extract_section(content, headings[0])
            if section:
                files.extend(_parse_file_lines(section, change_type))
        return files

    # Parse ### sub-headings within the Files section
    current_type = "new"  # default if no sub-heading
    section_lines: list[str] = []

    for line in files_section.splitlines():
        stripped = line.strip()
        heading_match = re.match(r"^###\s+(.+)", stripped)
        if heading_match:
            # Process accumulated lines
            if section_lines:
                files.extend(_parse_file_lines("\n".join(section_lines), current_type))
                section_lines = []
            heading_text = heading_match.group(1).strip().lower()
            current_type = heading_to_type.get(heading_text, "new")
            continue
        section_lines.append(line)

    # Process remaining lines
    if section_lines:
        files.extend(_parse_file_lines("\n".join(section_lines), current_type))

    return files


def _parse_file_lines(section: str, change_type: str) -> list[dict[str, Any]]:
    """Parse file entries from a section of text."""
    files: list[dict[str, Any]] = []

    for line in section.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        # Table row: "| path | description |"
        table_match = re.match(r"\|\s*`?(.+?)`?\s*\|(?:\s*(.+?)\s*\|)?", stripped)
        if table_match and not stripped.startswith("|---") and not stripped.startswith("| File"):
            path = table_match.group(1).strip().strip("`")
            desc = (table_match.group(2) or "").strip() if table_match.group(2) else None
            if path and not path.startswith("-"):
                files.append({
                    "file_path": path,
                    "change_type": change_type,
                    "description": desc,
                })
                continue

        # List item: "- `path` — description" or "- path"
        list_match = re.match(r"-\s+`(.+?)`(?:\s*[—–-]\s*(.+))?$", stripped)
        if list_match:
            path = list_match.group(1).strip()
            desc = list_match.group(2).strip() if list_match.group(2) else None
            files.append({
                "file_path": path,
                "change_type": change_type,
                "description": desc,
            })

    return files


def _parse_references(content: str) -> list[dict[str, Any]]:
    """Extract cross-references from ## References, ## Dependencies, ## Related Code."""
    refs: list[dict[str, Any]] = []

    for section_name in ["References", "Dependencies", "Related Code", "Related Tickets"]:
        section = _extract_section(content, section_name)
        if not section:
            continue

        for line in section.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue

            # Ticket reference: "0085", "ticket 0085", "#0085"
            ticket_match = re.findall(r"\b(\d{4}[a-z]?)\b", stripped)
            for t in ticket_match:
                ref_type = "ticket"
                # Determine more specific ref_type from context
                lower = stripped.lower()
                if "parent" in lower:
                    ref_type = "parent"
                elif "subtask" in lower or "sub-ticket" in lower:
                    ref_type = "subtask"
                elif "block" in lower:
                    ref_type = "blocks"
                elif "supersede" in lower:
                    ref_type = "supersedes"
                refs.append({"ref_type": ref_type, "ref_target": t})

            # Code/doc references: backtick paths
            code_refs = re.findall(r"`([^`]+\.\w+)`", stripped)
            for c in code_refs:
                refs.append({"ref_type": "code", "ref_target": c})

    return refs


def _detect_ticket_type(content: str) -> str:
    """Detect whether this is a feature ticket or debug ticket."""
    first_line = content.splitlines()[0] if content.splitlines() else ""
    if "debug" in first_line.lower():
        return "debug"
    return "feature"


# ---------------------------------------------------------------------------
# Main indexer
# ---------------------------------------------------------------------------


def index_single_ticket(
    conn: sqlite3.Connection,
    ticket_number: str,
    content: str,
    source_file: str,
) -> dict:
    """Parse and index a single ticket from its markdown content.

    Deletes any existing rows for the ticket (full replace), then inserts
    the parsed fields into the database. Does NOT commit or rebuild FTS —
    callers are responsible for that.

    Args:
        conn: Open SQLite connection to the traceability database.
        ticket_number: The ticket number (e.g. "0050" or "0050a").
        content: The full markdown content of the ticket.
        source_file: Relative path to the ticket file (for provenance).

    Returns:
        Dict with parsed ticket metadata: ticket_number, title, canonical_phase.
    """
    now = datetime.now(timezone.utc).isoformat()

    # Parse all fields
    title = _parse_title(content)
    raw_status = get_current_status(content)
    canonical_phase = _map_canonical_phase(raw_status)
    metadata = parse_metadata(content)
    summary = _parse_summary(content)
    criteria = _parse_acceptance_criteria(content)
    workflow_log = _parse_workflow_log(content)
    files = _parse_files(content)
    references = _parse_references(content)
    ticket_type = _detect_ticket_type(content)

    priority = metadata.get("Priority")
    complexity = metadata.get("Estimated Complexity")
    created_date = metadata.get("Created")
    author = metadata.get("Author")
    target_components = metadata.get("Target Component(s)") or metadata.get("Target Components")
    languages = metadata.get("Languages", "C++")
    requires_math = 1 if (metadata.get("Requires Math Design") or "").lower() in ("yes", "true") else 0
    generate_tutorial = 1 if (metadata.get("Generate Tutorial") or "").lower() in ("yes", "true") else 0
    parent_ticket = metadata.get("Parent Ticket")

    # Delete existing rows for this ticket (full replace)
    _delete_ticket(conn, ticket_number)

    # Insert core ticket
    conn.execute(
        """INSERT INTO tickets (
            ticket_number, title, canonical_phase, raw_status,
            priority, complexity, created_date, author, summary,
            ticket_type, parent_ticket, target_components, languages,
            requires_math, generate_tutorial, source_file, indexed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            ticket_number, title, canonical_phase, raw_status,
            priority, complexity, created_date, author, summary,
            ticket_type, parent_ticket, target_components, languages,
            requires_math, generate_tutorial, source_file, now,
        ),
    )

    # Insert acceptance criteria
    for ac in criteria:
        conn.execute(
            """INSERT INTO ticket_acceptance_criteria
               (ticket_number, criterion_id, description, is_met, category)
               VALUES (?, ?, ?, ?, ?)""",
            (ticket_number, ac["criterion_id"], ac["description"],
             1 if ac["is_met"] else 0, ac["category"]),
        )

    # Insert workflow log entries and artifacts
    for entry in workflow_log:
        cursor = conn.execute(
            """INSERT INTO ticket_workflow_log
               (ticket_number, phase_name, started_at, completed_at,
                branch, pr_url, status, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (ticket_number, entry["phase_name"], entry["started_at"],
             entry["completed_at"], entry["branch"], entry["pr_url"],
             entry["status"], entry["notes"]),
        )
        log_id = cursor.lastrowid
        for artifact in entry.get("artifacts", []):
            conn.execute(
                "INSERT INTO ticket_artifacts (workflow_log_id, artifact_path) VALUES (?, ?)",
                (log_id, artifact),
            )

    # Insert file declarations
    for f in files:
        conn.execute(
            """INSERT INTO ticket_files
               (ticket_number, file_path, change_type, description)
               VALUES (?, ?, ?, ?)""",
            (ticket_number, f["file_path"], f["change_type"], f["description"]),
        )

    # Insert references
    for ref in references:
        conn.execute(
            """INSERT INTO ticket_references
               (ticket_number, ref_type, ref_target)
               VALUES (?, ?, ?)""",
            (ticket_number, ref["ref_type"], ref["ref_target"]),
        )

    return {
        "ticket_number": ticket_number,
        "title": title,
        "canonical_phase": canonical_phase,
    }


def index_tickets(
    conn: sqlite3.Connection,
    repo_root: str,
    *,
    tickets_dir: str = "tickets",
) -> dict:
    """Index ticket markdown files into the traceability database.

    Args:
        conn: Open SQLite connection to the traceability database.
        repo_root: Path to the git repository root.
        tickets_dir: Relative path to tickets directory.

    Returns:
        Dict with indexing results: new_count, updated_count, total.
    """
    tickets_path = Path(repo_root) / tickets_dir
    if not tickets_path.is_dir():
        return {"new_count": 0, "updated_count": 0, "total": 0}

    # Get existing indexed timestamps
    existing: dict[str, str] = {}
    try:
        rows = conn.execute(
            "SELECT ticket_number, indexed_at FROM tickets"
        ).fetchall()
        existing = {row["ticket_number"]: row["indexed_at"] for row in rows}
    except sqlite3.OperationalError:
        pass

    id_regex = r"^(\d{4}[a-z]?)_"
    new_count = 0
    updated_count = 0

    for md_file in sorted(tickets_path.glob("*.md")):
        # Extract ticket number from filename
        match = re.match(id_regex, md_file.name)
        if not match:
            continue
        ticket_number = match.group(1)

        # Check if file is newer than indexed version
        file_mtime = datetime.fromtimestamp(md_file.stat().st_mtime, tz=timezone.utc).isoformat()
        if ticket_number in existing:
            if file_mtime <= existing[ticket_number]:
                continue

        content = md_file.read_text(encoding="utf-8")
        source_file = str(md_file.relative_to(repo_root))

        is_update = ticket_number in existing
        index_single_ticket(conn, ticket_number, content, source_file)

        if is_update:
            updated_count += 1
        else:
            new_count += 1

    # Rebuild FTS
    try:
        conn.execute("INSERT INTO tickets_fts(tickets_fts) VALUES('rebuild')")
    except sqlite3.OperationalError:
        pass

    conn.commit()

    total = conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]

    return {"new_count": new_count, "updated_count": updated_count, "total": total}


def _delete_ticket(conn: sqlite3.Connection, ticket_number: str) -> None:
    """Delete all rows for a ticket (for re-indexing)."""
    # Delete artifacts via workflow log
    conn.execute(
        """DELETE FROM ticket_artifacts WHERE workflow_log_id IN
           (SELECT id FROM ticket_workflow_log WHERE ticket_number = ?)""",
        (ticket_number,),
    )
    conn.execute("DELETE FROM ticket_workflow_log WHERE ticket_number = ?", (ticket_number,))
    conn.execute("DELETE FROM ticket_acceptance_criteria WHERE ticket_number = ?", (ticket_number,))
    conn.execute("DELETE FROM ticket_files WHERE ticket_number = ?", (ticket_number,))
    conn.execute("DELETE FROM ticket_references WHERE ticket_number = ?", (ticket_number,))
    conn.execute("DELETE FROM tickets WHERE ticket_number = ?", (ticket_number,))
