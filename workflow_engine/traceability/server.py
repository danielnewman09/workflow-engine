"""
Traceability Query Server

Provides design decision traceability, symbol history, and ticket impact
analysis. Operates on an already-open SQLite connection (typically the
traceability DB ATTACHed to the workflow DB).

Tools provided:
    - search_decisions: FTS search across design decision rationale
    - get_decision: Full details of a decision with linked symbols and commits
    - get_symbol_history: Timeline of changes to a symbol across commits
    - get_ticket_impact: All commits, file changes, symbol changes, and decisions for a ticket
    - get_commit_context: Context for a commit: ticket, phase, decisions, symbol changes
    - why_symbol: Design decision(s) that created/modified a symbol, with rationale
    - get_snapshot_symbols: All symbols at a specific point in time
    - get_record_mappings: All four layers' field lists for a record with drift analysis
    - check_record_drift: All records with fields missing from downstream layers
"""

import sqlite3

from workflow_engine.traceability.table_readers.core import CoreQuerier
from workflow_engine.traceability.table_readers.coverage import CoverageQuerier
from workflow_engine.traceability.table_readers.decision import DecisionQuerier
from workflow_engine.traceability.table_readers.record import RecordQuerier
from workflow_engine.traceability.table_readers.symbol import SymbolQuerier
from workflow_engine.traceability.table_readers.ticket import TicketQuerier


class TraceabilityServer:
    """Server for design decision traceability queries."""

    def __init__(self, conn: sqlite3.Connection):
        """Initialize with an already-open connection.

        Args:
            conn: SQLite connection (standalone or with ATTACHed traceability DB).
        """
        self.conn = conn
        self.core = CoreQuerier(conn)
        self.coverage = CoverageQuerier(conn)
        self.decisions = DecisionQuerier(conn)
        self.records = RecordQuerier(conn)
        self.symbols = SymbolQuerier(conn)
        self.tickets = TicketQuerier(conn)

    # =========================================================================
    # Design Decision Tools
    # =========================================================================

    def search_decisions(
        self, query: str, ticket: str | None = None, status: str | None = None
    ) -> list[dict]:
        """Search design decisions by keyword. See DecisionQuerier.search."""
        return self.decisions.search(query, ticket=ticket, status=status)

    def get_decision(self, dd_id: str) -> dict:
        """Get full details of a design decision. See DecisionQuerier.get."""
        return self.decisions.get(dd_id)

    # =========================================================================
    # Symbol History Tools
    # =========================================================================

    def get_symbol_history(self, qualified_name: str) -> list[dict]:
        """Timeline of changes to a symbol. See SymbolQuerier.history."""
        return self.symbols.history(qualified_name)

    # =========================================================================
    # Ticket Impact Tools
    # =========================================================================

    def get_ticket_impact(self, ticket_number: str) -> dict:
        """All commits, file changes, symbol changes, and decisions. See CoreQuerier.ticket_impact."""
        return self.core.ticket_impact(ticket_number)

    # =========================================================================
    # Commit Context Tools
    # =========================================================================

    def get_commit_context(self, commit_sha: str) -> dict:
        """Context for a commit: ticket, phase, decisions. See CoreQuerier.commit_context."""
        return self.core.commit_context(commit_sha)

    # =========================================================================
    # Why-Symbol Tool
    # =========================================================================

    def why_symbol(self, qualified_name: str) -> dict:
        """Design decision(s) that created/modified a symbol. See DecisionQuerier.why_symbol."""
        return self.decisions.why_symbol(qualified_name)

    # =========================================================================
    # Snapshot Symbols Tool
    # =========================================================================

    def get_snapshot_symbols(
        self, commit_sha: str, file_path: str | None = None
    ) -> list[dict]:
        """All symbols at a specific point in time. See SymbolQuerier.snapshot."""
        return self.symbols.snapshot(commit_sha, file_path=file_path)

    # =========================================================================
    # Record Layer Mapping Tools
    # =========================================================================

    def get_record_mappings(self, record_name: str) -> dict:
        """Return all four layers' field lists with drift analysis. See RecordQuerier.mappings."""
        return self.records.mappings(record_name)

    def check_record_drift(self) -> list[dict]:
        """All records with fields missing from downstream layers. See RecordQuerier.check_drift."""
        return self.records.check_drift()

    # =========================================================================
    # Ticket Query Tools
    # =========================================================================

    def search_tickets(
        self,
        query: str,
        phase: str | None = None,
        priority: str | None = None,
        component: str | None = None,
    ) -> list[dict]:
        """Search tickets by keyword. See TicketQuerier.search."""
        return self.tickets.search(query, phase=phase, priority=priority, component=component)

    def get_ticket(self, ticket_number: str) -> dict:
        """Get full ticket detail by number. See TicketQuerier.get."""
        return self.tickets.get(ticket_number)

    def list_tickets(
        self,
        phase: str | None = None,
        priority: str | None = None,
        component: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """List tickets filtered by phase, priority, and/or component. See TicketQuerier.list."""
        return self.tickets.list(phase=phase, priority=priority, component=component, limit=limit)

    def create_ticket(
        self,
        ticket_number: str,
        content: str,
        repo_root: str,
        tickets_dir: str = "tickets",
    ) -> dict:
        """Write a new ticket file to disk and index it. See TicketQuerier.create."""
        return self.tickets.create(ticket_number, content, repo_root, tickets_dir=tickets_dir)

    # =========================================================================
    # Coverage Query Tools
    # =========================================================================

    def get_coverage_summary(self, run_id: int | None = None) -> dict:
        """Get coverage summary for a run or the latest. See CoverageQuerier.summary."""
        return self.coverage.summary(run_id=run_id)

    def get_file_coverage(self, file_path: str, run_id: int | None = None) -> dict:
        """Get per-file coverage detail. See CoverageQuerier.file_detail."""
        return self.coverage.file_detail(file_path, run_id=run_id)

    def get_ticket_coverage(self, ticket_number: str) -> dict:
        """Get coverage for files associated with a ticket. See CoverageQuerier.ticket_coverage."""
        return self.coverage.ticket_coverage(ticket_number)
