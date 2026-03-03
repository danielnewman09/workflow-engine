"""
Traceability MCP tool registrations.

Called from ``create_mcp_server()`` when traceability is configured.
"""

import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from workflow_engine.server.server import WorkflowServer


def register_trace_tools(mcp: "FastMCP", ws: "WorkflowServer") -> None:
    """Register all traceability MCP tools on *mcp* using *ws* state."""
    trace = ws.trace_server
    assert trace is not None

    @mcp.tool()
    def search_decisions(
        query: str, ticket: str | None = None, status: str | None = None
    ) -> str:
        """FTS search across design decision rationale, alternatives, and trade-offs."""
        return json.dumps(
            trace.search_decisions(query, ticket, status),
            indent=2, default=str,
        )

    @mcp.tool()
    def get_decision(dd_id: str) -> str:
        """Get full details of a design decision with linked symbols and commits."""
        return json.dumps(
            trace.get_decision(dd_id), indent=2, default=str,
        )

    @mcp.tool()
    def get_symbol_history(qualified_name: str) -> str:
        """Timeline of changes to a symbol across commits (supports % wildcards)."""
        return json.dumps(
            trace.get_symbol_history(qualified_name),
            indent=2, default=str,
        )

    @mcp.tool()
    def get_ticket_impact(ticket_number: str) -> str:
        """All commits, file changes, symbol changes, and decisions for a ticket."""
        return json.dumps(
            trace.get_ticket_impact(ticket_number),
            indent=2, default=str,
        )

    @mcp.tool()
    def get_commit_context(commit_sha: str) -> str:
        """Context for a commit: ticket, phase, file changes, symbol changes, decisions."""
        return json.dumps(
            trace.get_commit_context(commit_sha),
            indent=2, default=str,
        )

    @mcp.tool()
    def why_symbol(qualified_name: str) -> str:
        """Design decision(s) that created or modified a symbol, with rationale."""
        return json.dumps(
            trace.why_symbol(qualified_name), indent=2, default=str,
        )

    @mcp.tool()
    def get_snapshot_symbols(
        commit_sha: str, file_path: str | None = None
    ) -> str:
        """All symbols at a specific point in time, optionally filtered by file."""
        return json.dumps(
            trace.get_snapshot_symbols(commit_sha, file_path),
            indent=2, default=str,
        )

    @mcp.tool()
    def get_record_mappings(record_name: str) -> str:
        """Return all four layers' field lists for a record with drift analysis."""
        return json.dumps(
            trace.get_record_mappings(record_name),
            indent=2, default=str,
        )

    @mcp.tool()
    def check_record_drift() -> str:
        """Return all records with fields missing from downstream layers."""
        return json.dumps(
            trace.check_record_drift(), indent=2, default=str,
        )

    @mcp.tool()
    def search_tickets(
        query: str,
        phase: str | None = None,
        priority: str | None = None,
        component: str | None = None,
    ) -> str:
        """FTS search across ticket titles and summaries. Filter by phase, priority, or component."""
        return json.dumps(
            trace.search_tickets(query, phase, priority, component),
            indent=2, default=str,
        )

    @mcp.tool()
    def get_ticket(ticket_number: str) -> str:
        """Get full ticket detail by number, including acceptance criteria, workflow log, and files."""
        return json.dumps(
            trace.get_ticket(ticket_number), indent=2, default=str,
        )

    @mcp.tool()
    def list_tickets(
        phase: str | None = None,
        priority: str | None = None,
        component: str | None = None,
        limit: int = 50,
    ) -> str:
        """List tickets filtered by canonical phase, priority, and/or target component."""
        return json.dumps(
            trace.list_tickets(phase, priority, component, limit),
            indent=2, default=str,
        )

    @mcp.tool()
    def get_ticket_coverage(ticket_number: str) -> str:
        """Get coverage data for files associated with a ticket."""
        return json.dumps(
            trace.get_ticket_coverage(ticket_number),
            indent=2, default=str,
        )

    @mcp.tool()
    def get_coverage_summary(run_id: int | None = None) -> str:
        """Get latest coverage run summary with top files by line count."""
        return json.dumps(
            trace.get_coverage_summary(run_id), indent=2, default=str,
        )

    @mcp.tool()
    def get_file_coverage(file_path: str, run_id: int | None = None) -> str:
        """Get per-file coverage detail including line-level hit counts."""
        return json.dumps(
            trace.get_file_coverage(file_path, run_id),
            indent=2, default=str,
        )

    @mcp.tool()
    def create_ticket(ticket_number: str, content: str) -> str:
        """Create a new ticket: write markdown to disk and index into the traceability DB.

        Args:
            ticket_number: 4-digit number optionally followed by a lowercase letter (e.g. '0090', '0090a')
            content: Full markdown content for the ticket
        """
        tickets_dir = (
            str(Path(ws.config.tickets_directory).relative_to(ws.project_root))
            if Path(ws.config.tickets_directory).is_absolute()
            else ws.config.tickets_directory
        )
        return json.dumps(
            trace.create_ticket(
                ticket_number, content, str(ws.project_root), tickets_dir
            ),
            indent=2, default=str,
        )
