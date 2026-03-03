"""Core traceability query logic.

Encapsulates ticket-impact and commit-context queries that span
snapshots, file_changes, symbol_changes, and design_decisions.
Owned by TraceabilityServer and delegates SQL construction to
core_queries.py.
"""

import sqlite3

from workflow_engine.traceability.table_readers.core_queries import (
    get_decisions_for_snapshot,
    get_decisions_for_ticket,
    get_decisions_for_ticket_short,
    get_file_changes_for_snapshot,
    get_file_changes_for_snapshots,
    get_snapshot_by_sha,
    get_snapshots_for_ticket,
    get_symbol_changes_for_snapshot,
    get_symbol_changes_for_snapshots,
)
from workflow_engine.utils.sqlite import rows_to_dicts


class CoreQuerier:
    """Cross-table queries over snapshots, file_changes, symbol_changes, and decisions."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def ticket_impact(self, ticket_number: str) -> dict:
        """All commits, file changes, symbol changes, and decisions for a ticket.

        Aggregates file changes across all commits (grouped by file_path)
        and collects symbol changes with commit metadata.

        Returns an ``{"error": ...}`` dict if no data exists for the ticket.
        """
        sql, params = get_snapshots_for_ticket(ticket_number)
        commits = rows_to_dicts(self.conn.execute(sql, params).fetchall())

        snapshot_ids = [c["id"] for c in commits]
        file_changes = []
        symbol_changes = []

        if snapshot_ids:
            sql, params = get_file_changes_for_snapshots(snapshot_ids)
            file_changes = rows_to_dicts(self.conn.execute(sql, params).fetchall())

            sql, params = get_symbol_changes_for_snapshots(snapshot_ids)
            symbol_changes = rows_to_dicts(self.conn.execute(sql, params).fetchall())

        sql, params = get_decisions_for_ticket(ticket_number)
        decisions = rows_to_dicts(self.conn.execute(sql, params).fetchall())

        if not commits and not decisions:
            return {"error": f"No data found for ticket '{ticket_number}'"}

        for c in commits:
            c.pop("id", None)

        return {
            "ticket": ticket_number,
            "commits": commits,
            "file_changes": file_changes,
            "symbol_changes": symbol_changes,
            "decisions": decisions,
        }

    def commit_context(self, commit_sha: str) -> dict:
        """Context for a commit: ticket, phase, decisions, symbol changes.

        Looks up the snapshot by SHA prefix, then gathers file changes,
        symbol changes, and decisions linked both via ticket number and
        directly via decision_commits.

        Returns an ``{"error": ...}`` dict if the commit is not found.
        """
        sql, params = get_snapshot_by_sha(commit_sha)
        row = self.conn.execute(sql, params).fetchone()
        if not row:
            return {"error": f"Commit '{commit_sha}' not found"}

        result = dict(row)
        snapshot_id = result["id"]

        sql, params = get_file_changes_for_snapshot(snapshot_id)
        result["file_changes"] = rows_to_dicts(
            self.conn.execute(sql, params).fetchall()
        )

        sql, params = get_symbol_changes_for_snapshot(snapshot_id)
        result["symbol_changes"] = rows_to_dicts(
            self.conn.execute(sql, params).fetchall()
        )

        decisions = []
        if result.get("ticket_number"):
            sql, params = get_decisions_for_ticket_short(result["ticket_number"])
            decisions = rows_to_dicts(self.conn.execute(sql, params).fetchall())

        sql, params = get_decisions_for_snapshot(snapshot_id)
        direct_links = rows_to_dicts(self.conn.execute(sql, params).fetchall())
        seen_ids = {d["dd_id"] for d in decisions}
        for d in direct_links:
            if d["dd_id"] not in seen_ids:
                decisions.append(d)

        result["decisions"] = decisions
        result.pop("id", None)
        return result
