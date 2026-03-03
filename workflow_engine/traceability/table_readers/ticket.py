"""Ticket query logic.

Encapsulates all ticket-related queries: search, detail retrieval,
listing, and creation.  Owned by TraceabilityServer and delegates SQL
construction to ticket_queries.py.
"""

import re
import sqlite3
from pathlib import Path

from workflow_engine.traceability.table_readers.ticket_queries import (
    get_ticket_acceptance_criteria,
    get_ticket_artifacts,
    get_ticket_detail,
    get_ticket_files,
    get_ticket_references,
    get_ticket_workflow_log,
    list_tickets_filtered,
    search_tickets_fts,
    search_tickets_like,
)
from workflow_engine.utils.sqlite import rows_to_dicts
from workflow_engine.utils.string_utils import slugify, split_pascal_case


class TicketQuerier:
    """Queries over the tickets family of tables."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def search(
        self,
        query: str,
        phase: str | None = None,
        priority: str | None = None,
        component: str | None = None,
    ) -> list[dict]:
        """Search tickets by keyword, with FTS5 ranked search
        and automatic LIKE fallback.

        If query is empty, delegates to list() for filter-based listing.

        Args:
            query: Free-text search string (supports PascalCase splitting).
            phase: Optional canonical phase filter.
            priority: Optional priority filter.
            component: Optional component substring filter.

        Returns:
            List of matching ticket dicts, ordered by relevance (max 20).
        """
        if not query.strip():
            return self.list(phase=phase, priority=priority, component=component)

        results = []

        try:
            sql, params = search_tickets_fts(
                query, phase=phase, priority=priority, component=component
            )
            cursor = self.conn.execute(sql, params)
            results = rows_to_dicts(cursor.fetchall())
        except sqlite3.OperationalError:
            pass

        if results:
            return results

        words = split_pascal_case(query)
        if not words:
            return []

        sql, params = search_tickets_like(
            words, phase=phase, priority=priority, component=component
        )
        cursor = self.conn.execute(sql, params)
        return rows_to_dicts(cursor.fetchall())

    def get(self, ticket_number: str) -> dict:
        """Get full ticket detail by number.

        Returns a dict with the ticket fields plus:
            acceptance_criteria: list of criterion dicts.
            workflow_log: list of log entry dicts with nested artifacts.
            files: list of file declaration dicts.
            references: list of reference dicts.

        Returns an ``{"error": ...}`` dict if the ticket is not found.
        """
        sql, params = get_ticket_detail(ticket_number)
        row = self.conn.execute(sql, params).fetchone()
        if not row:
            return {"error": f"Ticket '{ticket_number}' not found"}

        result = dict(row)
        result.pop("id", None)

        # Acceptance criteria
        sql, params = get_ticket_acceptance_criteria(ticket_number)
        result["acceptance_criteria"] = rows_to_dicts(
            self.conn.execute(sql, params).fetchall()
        )

        # Workflow log with nested artifacts
        sql, params = get_ticket_workflow_log(ticket_number)
        log_entries = []
        for log_row in self.conn.execute(sql, params).fetchall():
            entry = dict(log_row)
            log_id = entry.pop("id")
            art_sql, art_params = get_ticket_artifacts(log_id)
            entry["artifacts"] = [
                r["artifact_path"]
                for r in self.conn.execute(art_sql, art_params).fetchall()
            ]
            log_entries.append(entry)
        result["workflow_log"] = log_entries

        # File declarations
        sql, params = get_ticket_files(ticket_number)
        result["files"] = rows_to_dicts(self.conn.execute(sql, params).fetchall())

        # References
        sql, params = get_ticket_references(ticket_number)
        result["references"] = rows_to_dicts(self.conn.execute(sql, params).fetchall())

        return result

    def list(
        self,
        phase: str | None = None,
        priority: str | None = None,
        component: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """List tickets filtered by phase, priority, and/or component.

        Returns:
            List of ticket summary dicts, ordered by ticket_number (max ``limit``).
        """
        sql, params = list_tickets_filtered(
            phase=phase, priority=priority, component=component, limit=limit
        )
        cursor = self.conn.execute(sql, params)
        return rows_to_dicts(cursor.fetchall())

    def create(
        self,
        ticket_number: str,
        content: str,
        repo_root: str,
        tickets_dir: str = "tickets",
    ) -> dict:
        """Write a new ticket file to disk and index it into the database.

        Args:
            ticket_number: Numeric ticket ID (e.g. "0050" or "0050a").
            content: Full markdown content for the ticket.
            repo_root: Absolute path to the repository root.
            tickets_dir: Relative path to the tickets directory.

        Returns:
            Dict with ticket_number, file_path, and title on success,
            or a dict with an "error" key on failure.
        """
        if not re.match(r"^\d{4}[a-z]?$", ticket_number):
            return {
                "error": f"Invalid ticket_number format: '{ticket_number}'. "
                "Expected 4 digits optionally followed by a lowercase letter "
                "(e.g. '0050', '0050a')."
            }

        row = self.conn.execute(
            "SELECT ticket_number FROM tickets WHERE ticket_number = ?",
            (ticket_number,),
        ).fetchone()
        if row:
            return {"error": f"Ticket '{ticket_number}' already exists in the database."}

        tickets_path = Path(repo_root) / tickets_dir
        tickets_path.mkdir(parents=True, exist_ok=True)
        for existing_file in tickets_path.glob(f"{ticket_number}_*.md"):
            return {"error": f"Ticket file already exists on disk: {existing_file.name}"}

        from workflow_engine.traceability.index_tickets import (
            _parse_title,
            index_single_ticket,
        )

        title = _parse_title(content)
        slug = slugify(title) if title != "Untitled" else "untitled"
        filename = f"{ticket_number}_{slug}.md"
        file_path = tickets_path / filename

        file_path.write_text(content, encoding="utf-8")

        source_file = f"{tickets_dir}/{filename}"
        result = index_single_ticket(self.conn, ticket_number, content, source_file)

        try:
            self.conn.execute("INSERT INTO tickets_fts(tickets_fts) VALUES('rebuild')")
        except sqlite3.OperationalError:
            pass
        self.conn.commit()

        return {
            "ticket_number": result["ticket_number"],
            "file_path": str(file_path),
            "title": result["title"],
        }
