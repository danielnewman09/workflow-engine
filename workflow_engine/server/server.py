#!/usr/bin/env python3
# Ticket: 0083_database_agent_orchestration
# Design: docs/designs/0083_database_agent_orchestration/design.md
"""
Workflow Engine MCP Server

FastMCP server exposing the work queue to agents via MCP tools.
Supports both stdio (local development) and SSE (Docker) transports.

Usage (stdio mode — used by Claude Code):
    python server.py <db_path> --project-root <path>

Usage (SSE mode — Docker):
    python server.py <db_path> --project-root <path> --transport sse --port 8080

Usage (CLI smoke-test):
    python server.py <db_path> --project-root <path> <command> [args...]

MCP Tools exposed:
    register_agent       — register an agent instance, returns agent_id
    list_available_work  — list phases available for this agent type
    claim_phase          — atomically claim next available phase (or specific)
    heartbeat            — update agent liveness timestamp
    start_phase          — mark claimed phase as running
    complete_phase       — report successful completion
    fail_phase           — report phase failure
    release_phase        — release a claimed phase back to available
    request_human_review — create a human gate blocking the next phase
    approve_gate         — human approves a pending gate, unblocking the next phase
    reject_gate          — human rejects a pending gate with feedback
    get_pr_reviews       — fetch reviews and comments from the current branch's PR
    reply_to_review_comment — reply to an inline review comment (attributed to Claude)
    request_gate_revisions — request revisions on a gate, pulling PR comments as context
    get_ticket_status    — full ticket status with all phases
    list_tickets         — query tickets by criteria
    list_blocked         — list all phases blocked on gates or dependencies
    list_agents          — list registered agents and current assignments
    declare_files        — declare files this phase will modify
    check_conflicts      — check for active file locks on given files
    get_phase_metrics    — phase duration statistics
    get_audit_log        — query audit trail
    update_ticket_metadata — update project-specific metadata fields
    import_tickets       — import/refresh tickets from markdown
    run_scheduler        — seed phases and resolve availability
    cleanup_stale        — release stale agent claims
    log_build_attempt    — log implementer build attempt + circle detection

Traceability tools (registered when traceability.db_path is configured):
    search_decisions     — FTS search across design decision rationale
    get_decision         — full details of a design decision
    get_symbol_history   — timeline of changes to a symbol
    get_ticket_impact    — all impact data for a ticket
    get_commit_context   — context for a commit
    why_symbol           — design decision(s) behind a symbol
    get_snapshot_symbols — symbols at a point in time
    get_record_mappings  — four-layer field lists with drift analysis
    check_record_drift   — records with missing downstream fields
    search_tickets       — FTS search across ticket titles/summaries
    get_ticket           — full ticket detail by number
    list_tickets         — list tickets by phase/priority/component
    get_ticket_coverage  — coverage data for files in a ticket
    get_coverage_summary — latest coverage run summary
    get_file_coverage    — per-file coverage with line detail

MCP Resources:
    workflow://dashboard           — summary dashboard
    workflow://ticket/{id}         — full ticket state
    workflow://queue/{agent_type}  — work queue for agent type
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

try:
    from fastmcp import FastMCP
    HAS_MCP = True
except ImportError:
    try:
        from mcp.server.fastmcp import FastMCP
        HAS_MCP = True
    except ImportError:
        HAS_MCP = False

from workflow_engine.engine import claim as claim_mod
from workflow_engine.engine import scheduler as sched_mod
from workflow_engine.engine.config import load_workflow_config
from workflow_engine.engine.schema import create_db
from workflow_engine.traceability.schema import ensure_schema as ensure_trace_schema
from workflow_engine.traceability.server import TraceabilityServer
from workflow_engine.utils.sqlite import rows_to_dicts


# ---------------------------------------------------------------------------
# WorkflowServer — state holder + CLI methods
# ---------------------------------------------------------------------------


class WorkflowServer:
    """
    Workflow engine server wrapping the SQLite database.

    Owns the database connection, config, and optional traceability server.
    MCP tools access ``ws.conn``, ``ws.config``, etc. directly.
    CLI commands use the convenience methods on this class.
    """

    def __init__(self, db_path: str, project_root: str):
        self.db_path = db_path
        self.project_root = Path(project_root)
        self.conn = create_db(db_path)
        self.config = load_workflow_config(project_root)

        # ATTACH traceability DB if configured
        self.trace_server: TraceabilityServer | None = None
        self.trace_conn: sqlite3.Connection | None = None
        self.trace_db_path: str | None = self.config.traceability_db_path
        if self.trace_db_path:
            trace_path = Path(self.trace_db_path)
            trace_path.parent.mkdir(parents=True, exist_ok=True)
            # check_same_thread=False needed because FastMCP runs tools on worker threads
            self.trace_conn = sqlite3.connect(str(trace_path), check_same_thread=False)
            self.trace_conn.row_factory = sqlite3.Row
            self.trace_conn.execute("PRAGMA journal_mode=WAL")
            self.trace_conn.execute("PRAGMA foreign_keys=ON")
            ensure_trace_schema(self.trace_conn)
            self.trace_server = TraceabilityServer(self.trace_conn)

    def close(self) -> None:
        """Close all database connections."""
        if self.trace_conn is not None:
            self.trace_conn.close()
        self.conn.close()

    def _require_trace(self) -> TraceabilityServer:
        """Return the traceability server or raise an error."""
        if self.trace_server is None:
            raise RuntimeError(
                "Traceability not configured. Add a 'traceability' section "
                "to .workflow/config.yaml with db_path."
            )
        return self.trace_server

    # -----------------------------------------------------------------------
    # CLI-only methods (used by main() CLI commands)
    # -----------------------------------------------------------------------

    def import_tickets(self) -> dict[str, Any]:
        """Import/refresh all tickets from markdown files."""
        results = sched_mod.import_all_tickets(self.conn, self.config)
        created = sum(1 for r in results if r.get("action") == "created")
        updated = sum(1 for r in results if r.get("action") == "updated")
        errors = sum(1 for r in results if r.get("action") == "error")
        return {
            "imported": len(results),
            "created": created,
            "updated": updated,
            "errors": errors,
            "results": results,
        }

    def get_dashboard(self) -> dict[str, Any]:
        """Summary dashboard: agents, gates, phase counts by status."""
        phase_counts = dict(
            self.conn.execute(
                "SELECT status, COUNT(*) AS n FROM phases GROUP BY status"
            ).fetchall()
        )

        active_agents = self.conn.execute(
            "SELECT COUNT(*) AS n FROM agents WHERE status = 'working'"
        ).fetchone()["n"]

        pending_gates = self.conn.execute(
            "SELECT COUNT(*) AS n FROM human_gates WHERE status = 'pending'"
        ).fetchone()["n"]

        total_tickets = self.conn.execute(
            "SELECT COUNT(*) AS n FROM tickets"
        ).fetchone()["n"]

        return {
            "total_tickets": total_tickets,
            "active_agents": active_agents,
            "pending_human_gates": pending_gates,
            "phase_counts": phase_counts,
        }

    def run_scheduler(self, ticket_id: str | None = None) -> dict[str, Any]:
        """Seed phases for all tickets and resolve phase availability."""
        seeded_all = []

        if ticket_id:
            ticket_ids = [ticket_id]
        else:
            ticket_ids = [
                row["id"] for row in self.conn.execute("SELECT id FROM tickets").fetchall()
            ]

        for tid in ticket_ids:
            seeded = sched_mod.seed_phases(self.conn, tid, self.config)
            seeded_all.extend(seeded)

        transitions = sched_mod.resolve_availability(self.conn, ticket_id)

        return {
            "phases_seeded": len(seeded_all),
            "availability_transitions": len(transitions),
            "transitions": transitions,
        }

    def cleanup_stale(self) -> dict[str, Any]:
        """Release stale agent claims."""
        released = sched_mod.cleanup_stale_agents(
            self.conn,
            stale_timeout_minutes=self.config.stale_timeout_minutes,
        )
        return {
            "released": len(released),
            "phases": released,
        }

    def list_blocked(self) -> dict[str, Any]:
        """List all phases blocked on human gates or unresolved dependencies."""
        gate_blocked = self.conn.execute(
            """
            SELECT p.id AS phase_id, p.ticket_id, p.phase_name,
                   hg.id AS gate_id, hg.gate_type, hg.requested_at
            FROM phases p
            JOIN human_gates hg ON hg.phase_id = p.id
            WHERE p.status = 'blocked'
              AND hg.status = 'pending'
            ORDER BY hg.requested_at ASC
            """
        ).fetchall()

        dep_blocked = self.conn.execute(
            """
            SELECT DISTINCT t.id AS ticket_id, t.full_name,
                            d.blocking_ticket_id, d.dependency_type
            FROM dependencies d
            JOIN tickets t ON t.id = d.blocked_ticket_id
            WHERE d.resolved = 0
            ORDER BY t.id ASC
            """
        ).fetchall()

        return {
            "gate_blocked": rows_to_dicts(gate_blocked),
            "dependency_blocked": rows_to_dicts(dep_blocked),
        }


# ---------------------------------------------------------------------------
# FastMCP server factory
# ---------------------------------------------------------------------------


def create_mcp_server(db_path: str, project_root: str) -> "FastMCP":
    """
    Create a FastMCP server with all workflow tools.

    Business logic lives directly in each tool function.
    ``ws`` provides shared state (conn, config, trace_server, project_root).
    """
    ws = WorkflowServer(db_path, project_root)
    mcp = FastMCP("workflow")

    # -----------------------------------------------------------------------
    # Agent lifecycle, phase management, gates, metrics, audit, build log
    # -----------------------------------------------------------------------

    from workflow_engine.server.agent_phase_mcp import register_agent_phase_tools
    register_agent_phase_tools(mcp, ws)

    # -----------------------------------------------------------------------
    # Git/GitHub tools
    # -----------------------------------------------------------------------

    from workflow_engine.server.github_mcp import register_github_tools
    register_github_tools(mcp, ws)

    # -----------------------------------------------------------------------
    # Ticket query + update tools
    # -----------------------------------------------------------------------

    from workflow_engine.server.ticket_mcp import register_ticket_tools
    register_ticket_tools(mcp, ws)

    # ----- Traceability tools -----
    # Only registered if traceability is configured

    if ws.trace_server is not None:
        from workflow_engine.server.trace_server_mcp import register_trace_tools
        register_trace_tools(mcp, ws)

    # MCP Resources
    @mcp.resource("workflow://dashboard")
    def dashboard() -> str:
        """Summary dashboard: active agents, pending gates, phase counts by status."""
        return json.dumps(ws.get_dashboard(), indent=2)

    @mcp.resource("workflow://ticket/{ticket_id}")
    def ticket_resource(ticket_id: str) -> str:
        """Full ticket state with all phases and gates."""
        ticket = ws.conn.execute(
            "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()

        if ticket is None:
            return json.dumps({"error": f"Ticket '{ticket_id}' not found"}, indent=2)

        phases = ws.conn.execute(
            """
            SELECT id, phase_name, phase_order, status, agent_type, claimed_by,
                   claimed_at, started_at, completed_at, result_summary, error_details,
                   artifacts, parallel_group
            FROM phases WHERE ticket_id = ?
            ORDER BY phase_order ASC, id ASC
            """,
            (ticket_id,),
        ).fetchall()

        gates = ws.conn.execute(
            "SELECT * FROM human_gates WHERE ticket_id = ?", (ticket_id,)
        ).fetchall()

        return json.dumps({
            "ticket": dict(ticket),
            "phases": [dict(p) for p in phases],
            "gates": [dict(g) for g in gates],
        }, indent=2, default=str)

    @mcp.resource("workflow://queue/{agent_type}")
    def queue_resource(agent_type: str) -> str:
        """Work queue for a specific agent type."""
        return json.dumps(
            claim_mod.list_available(ws.conn, agent_type), indent=2
        )

    return mcp


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Workflow Engine MCP Server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # MCP server mode (used by Claude Code)
    python server.py build/Debug/docs/workflow.db --project-root .

    # Streamable HTTP mode (Docker, recommended)
    python server.py workflow.db --project-root /app/project --transport streamable-http --port 8080

    # SSE mode (legacy)
    python server.py workflow.db --project-root /app/project --transport sse --port 8080

    # CLI smoke tests
    python server.py build/Debug/docs/workflow.db --project-root . import_tickets
    python server.py build/Debug/docs/workflow.db --project-root . dashboard
        """,
    )
    parser.add_argument("database", help="Path to the workflow SQLite database")
    parser.add_argument("--project-root", default=".", help="Path to consuming repo root")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
    )
    parser.add_argument("--port", type=int, default=8080, help="Port for HTTP/SSE mode")
    parser.add_argument(
        "command",
        nargs="?",
        help="CLI command (omit for MCP server mode)",
    )

    args = parser.parse_args()

    db_path = Path(args.database)
    project_root = Path(args.project_root)

    if not args.command:
        # MCP server mode
        if not HAS_MCP:
            print("Error: mcp package not installed. Run: pip install mcp", file=sys.stderr)
            sys.exit(1)
        mcp_server = create_mcp_server(str(db_path), str(project_root))
        if args.transport in ("sse", "streamable-http"):
            mcp_server.run(
                transport=args.transport, host="0.0.0.0", port=args.port
            )
        else:
            mcp_server.run(transport="stdio")
        return

    # CLI mode — create server and run command
    ws = WorkflowServer(str(db_path), str(project_root))
    try:
        if args.command == "import_tickets":
            result = ws.import_tickets()
        elif args.command == "dashboard":
            result = ws.get_dashboard()
        elif args.command == "run_scheduler":
            result = ws.run_scheduler()
        elif args.command == "cleanup_stale":
            result = ws.cleanup_stale()
        elif args.command == "list_blocked":
            result = ws.list_blocked()
        else:
            print(f"Unknown command: {args.command}", file=sys.stderr)
            sys.exit(1)

        print(json.dumps(result, indent=2, default=str))
    finally:
        ws.close()


if __name__ == "__main__":
    main()
