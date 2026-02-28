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

MCP Resources:
    workflow://dashboard           — summary dashboard
    workflow://ticket/{id}         — full ticket state
    workflow://queue/{agent_type}  — work queue for agent type
"""

import argparse
import json
import sqlite3
import sys
import uuid
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

from workflow_engine.engine import audit as audit_mod
from workflow_engine.engine import claim as claim_mod
from workflow_engine.engine import github as github_mod
from workflow_engine.engine import scheduler as sched_mod
from workflow_engine.engine.config import load_workflow_config
from workflow_engine.engine.schema import create_db
from workflow_engine.traceability.schema import ensure_schema as ensure_trace_schema
from workflow_engine.traceability.server import TraceabilityServer
from workflow_engine.traceability.reindex import reindex_all as trace_reindex_all


class WorkflowServer:
    """
    Workflow engine server wrapping the SQLite database.

    Owns the database connection and exposes all workflow operations.
    The FastMCP tools delegate to this class.
    """

    def __init__(self, db_path: str, project_root: str):
        self.db_path = db_path
        self.project_root = Path(project_root)
        self.conn = create_db(db_path)
        self.config = load_workflow_config(project_root)

        # ATTACH traceability DB if configured
        self.trace_server: TraceabilityServer | None = None
        self.trace_db_path: str | None = self.config.traceability_db_path
        if self.trace_db_path:
            trace_path = Path(self.trace_db_path)
            trace_path.parent.mkdir(parents=True, exist_ok=True)
            # Create a dedicated connection for traceability queries
            self.trace_conn = sqlite3.connect(str(trace_path))
            self.trace_conn.row_factory = sqlite3.Row
            self.trace_conn.execute("PRAGMA journal_mode=WAL")
            self.trace_conn.execute("PRAGMA foreign_keys=ON")
            ensure_trace_schema(self.trace_conn)
            self.trace_server = TraceabilityServer(self.trace_conn)

    def close(self) -> None:
        """Close all database connections."""
        if hasattr(self, 'trace_conn') and self.trace_conn:
            self.trace_conn.close()
        self.conn.close()

    # -----------------------------------------------------------------------
    # Agent registration and lifecycle
    # -----------------------------------------------------------------------

    def register_agent(
        self,
        agent_type: str,
        agent_id: str | None = None,
        metadata: dict | None = None,
    ) -> dict[str, Any]:
        """
        Register an agent instance with the work queue.

        If agent_id is not provided, a new UUID is generated.

        Returns:
            {agent_id, agent_type, status, message}
        """
        aid = agent_id or f"agent-{uuid.uuid4().hex[:12]}"
        result = sched_mod.register_agent(self.conn, aid, agent_type, metadata)
        result["message"] = (
            f"Agent '{aid}' registered as '{agent_type}'. "
            "Call list_available_work to see available phases."
        )
        return result

    def list_available_work(
        self,
        agent_type: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """
        List phases available for claiming by this agent type.

        Read-only — does not claim. Use claim_phase to actually claim.
        """
        return claim_mod.list_available(self.conn, agent_type, limit)

    def claim_phase(
        self,
        agent_id: str,
        phase_id: int | None = None,
    ) -> dict[str, Any]:
        """
        Atomically claim a phase.

        If phase_id is provided, claim that specific phase.
        If phase_id is None, claim the highest-priority available phase
        for this agent's type.

        The agent type is looked up from the agents table.

        Returns:
            {success, phase_id, ticket_id, phase_name, ...} on success
            {success: false, reason: "..."} on failure
        """
        # Look up agent type
        agent_row = self.conn.execute(
            "SELECT agent_type FROM agents WHERE id = ?", (agent_id,)
        ).fetchone()

        if agent_row is None:
            return {
                "success": False,
                "reason": f"Agent '{agent_id}' not registered. Call register_agent first.",
            }

        agent_type = agent_row["agent_type"]

        if phase_id is not None:
            result = claim_mod.claim_specific(self.conn, agent_id, phase_id)
        else:
            result = claim_mod.claim_next(self.conn, agent_id, agent_type)

        if result.success:
            phase = result.phase  # type: ignore[union-attr]
            return {
                "success": True,
                "phase_id": phase.id,
                "ticket_id": phase.ticket_id,
                "phase_name": phase.phase_name,
                "agent_type": agent_type,
                "message": (
                    f"Claimed '{phase.phase_name}' for ticket {phase.ticket_id}. "
                    "Call start_phase when you begin work."
                ),
            }
        else:
            return {
                "success": False,
                "reason": str(result),
            }

    def heartbeat(self, agent_id: str) -> dict[str, Any]:
        """Update agent liveness timestamp."""
        updated = sched_mod.heartbeat(self.conn, agent_id)
        return {
            "agent_id": agent_id,
            "updated": updated,
            "message": "Heartbeat recorded." if updated else f"Agent '{agent_id}' not found or is stale.",
        }

    def start_phase(self, agent_id: str, phase_id: int) -> dict[str, Any]:
        """Mark a claimed phase as running."""
        return sched_mod.start_phase(self.conn, agent_id, phase_id)

    def complete_phase(
        self,
        agent_id: str,
        phase_id: int,
        result_summary: str | None = None,
        artifacts: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Report successful phase completion.

        After completion, automatically resolves availability for downstream phases.
        """
        result = sched_mod.complete_phase(
            self.conn, agent_id, phase_id, result_summary, artifacts
        )
        # Resolve downstream availability
        ticket_id = result["ticket_id"]
        transitions = sched_mod.resolve_availability(self.conn, ticket_id)
        result["downstream_transitions"] = transitions
        result["message"] = (
            f"Phase '{result['phase_name']}' completed for ticket {ticket_id}. "
            f"{len(transitions)} downstream phase(s) updated."
        )
        return result

    def fail_phase(
        self,
        agent_id: str,
        phase_id: int,
        error_details: str | None = None,
    ) -> dict[str, Any]:
        """Report phase failure."""
        return sched_mod.fail_phase(self.conn, agent_id, phase_id, error_details)

    def release_phase(self, agent_id: str, phase_id: int) -> dict[str, Any]:
        """Release a claimed phase back to available."""
        released = claim_mod.release_phase(self.conn, agent_id, phase_id)
        return {
            "released": released,
            "phase_id": phase_id,
            "agent_id": agent_id,
            "message": "Phase released back to available." if released else "Release failed — phase not held by this agent.",
        }

    def request_human_review(
        self,
        phase_id: int,
        gate_type: str,
        context: dict | None = None,
    ) -> dict[str, Any]:
        """
        Create a human gate blocking downstream phases.

        Typically called by an agent after completing a phase that requires
        human review before the next phase can proceed.
        """
        phase = self.conn.execute(
            "SELECT ticket_id, phase_name FROM phases WHERE id = ?", (phase_id,)
        ).fetchone()

        if phase is None:
            return {"error": f"Phase {phase_id} not found"}

        context_json = json.dumps(context) if context else None

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO human_gates
                    (phase_id, ticket_id, gate_type, status, context)
                VALUES (:phase_id, :ticket_id, :gate_type, 'pending', :context)
                """,
                {
                    "phase_id": phase_id,
                    "ticket_id": phase["ticket_id"],
                    "gate_type": gate_type,
                    "context": context_json,
                },
            )

            audit_mod.log(
                self.conn,
                actor="agent",
                action="create_gate",
                entity_type="gate",
                entity_id=f"{phase['ticket_id']}/{phase['phase_name']}",
                details={"gate_type": gate_type, "phase_id": phase_id},
            )

            self.conn.execute("COMMIT")
        except Exception:
            try:
                self.conn.execute("ROLLBACK")
            except Exception:
                pass
            raise

        return {
            "gate_created": True,
            "ticket_id": phase["ticket_id"],
            "phase_id": phase_id,
            "gate_type": gate_type,
            "message": (
                "Human review gate created. "
                "Run: workflow-engine gates  to see pending reviews."
            ),
        }

    def approve_gate(
        self,
        gate_id: int,
        decided_by: str,
        notes: str | None = None,
    ) -> dict[str, Any]:
        """
        Approve a pending human review gate, unblocking the next phase.

        This must be called by a human reviewer — agents should not call
        this tool directly.
        """
        return sched_mod.resolve_gate(
            self.conn, gate_id, "approved", decided_by, notes
        )

    def reject_gate(
        self,
        gate_id: int,
        decided_by: str,
        notes: str | None = None,
    ) -> dict[str, Any]:
        """
        Reject a pending human review gate with feedback.

        The associated phase remains blocked. The upstream phase may need
        to be re-run to address the feedback.
        """
        return sched_mod.resolve_gate(
            self.conn, gate_id, "rejected", decided_by, notes
        )

    def request_gate_revisions(
        self,
        gate_id: int,
        decided_by: str,
        notes: str | None = None,
    ) -> dict[str, Any]:
        """
        Request revisions on a human gate, pulling PR review comments as context.

        Fetches all reviews and comments from the current branch's PR,
        formats them as the gate's decision notes, and marks the gate as
        changes_requested.
        """
        # Fetch PR reviews
        pr_data = github_mod.get_pr_reviews(self.project_root)

        # Build revision notes from PR feedback
        feedback_parts = []
        if notes:
            feedback_parts.append(notes)

        if "error" not in pr_data:
            for review in pr_data.get("reviews", []):
                if review.get("body"):
                    feedback_parts.append(
                        f"[Review by {review['author']} ({review['state']})]: {review['body']}"
                    )
            for comment in pr_data.get("comments", []):
                if comment.get("body"):
                    feedback_parts.append(
                        f"[Comment by {comment['author']}]: {comment['body']}"
                    )
            for rc in pr_data.get("review_comments", []):
                if rc.get("body"):
                    location = f"{rc.get('path', '?')}:{rc.get('line', '?')}"
                    feedback_parts.append(
                        f"[Inline at {location} by {rc['author']}]: {rc['body']}"
                    )

        combined_notes = "\n\n".join(feedback_parts) if feedback_parts else None

        result = sched_mod.resolve_gate(
            self.conn, gate_id, "changes_requested", decided_by, combined_notes
        )
        result["pr_number"] = pr_data.get("pr_number")
        result["feedback_items"] = len(feedback_parts)
        return result

    def get_pr_reviews(self) -> dict[str, Any]:
        """Fetch reviews and comments from the current branch's PR."""
        return github_mod.get_pr_reviews(self.project_root)

    def reply_to_review_comment(
        self,
        comment_id: int,
        body: str,
    ) -> dict[str, Any]:
        """Reply to an inline review comment on the current branch's PR."""
        return github_mod.reply_to_review_comment(self.project_root, comment_id, body)

    # -----------------------------------------------------------------------
    # Query tools
    # -----------------------------------------------------------------------

    def get_ticket_status(self, ticket_id: str) -> dict[str, Any]:
        """Get full ticket status with all phases."""
        ticket = self.conn.execute(
            "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()

        if ticket is None:
            return {"error": f"Ticket '{ticket_id}' not found"}

        phases = self.conn.execute(
            """
            SELECT id, phase_name, phase_order, status, agent_type, claimed_by,
                   claimed_at, started_at, completed_at, result_summary, error_details,
                   artifacts, parallel_group
            FROM phases WHERE ticket_id = ?
            ORDER BY phase_order ASC, id ASC
            """,
            (ticket_id,),
        ).fetchall()

        gates = self.conn.execute(
            "SELECT * FROM human_gates WHERE ticket_id = ?", (ticket_id,)
        ).fetchall()

        return {
            "ticket": dict(ticket),
            "phases": [dict(p) for p in phases],
            "gates": [dict(g) for g in gates],
        }

    def list_tickets(
        self,
        status_filter: str | None = None,
        priority: str | None = None,
        component: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Query tickets by criteria."""
        conditions: list[str] = []
        params: list[Any] = []

        if status_filter:
            conditions.append("current_status LIKE ?")
            params.append(f"%{status_filter}%")
        if priority:
            conditions.append("priority = ?")
            params.append(priority)
        if component:
            conditions.append("components LIKE ?")
            params.append(f"%{component}%")

        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        cursor = self.conn.execute(
            f"""
            SELECT id, full_name, priority, current_status, languages, components
            FROM tickets
            {where}
            ORDER BY
                CASE priority
                    WHEN 'Critical' THEN 0 WHEN 'High' THEN 1
                    WHEN 'Medium' THEN 2 WHEN 'Low' THEN 3 ELSE 4
                END ASC,
                id ASC
            LIMIT ?
            """,
            params + [limit],
        )
        return [dict(row) for row in cursor.fetchall()]

    def list_blocked(self) -> dict[str, Any]:
        """List all phases blocked on human gates or unresolved dependencies."""
        # Phases blocked on human gates
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

        # Phases blocked on ticket dependencies
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
            "gate_blocked": [dict(r) for r in gate_blocked],
            "dependency_blocked": [dict(r) for r in dep_blocked],
        }

    def list_agents(self) -> list[dict[str, Any]]:
        """List registered agents and their current assignments."""
        cursor = self.conn.execute(
            """
            SELECT a.id, a.agent_type, a.status, a.last_heartbeat,
                   p.phase_name AS current_phase, p.ticket_id AS current_ticket
            FROM agents a
            LEFT JOIN phases p ON p.id = a.current_phase_id
            WHERE a.status != 'terminated'
            ORDER BY a.status DESC, a.last_heartbeat DESC
            """
        )
        return [dict(row) for row in cursor.fetchall()]

    def declare_files(
        self,
        agent_id: str,
        phase_id: int,
        file_paths: list[str],
    ) -> list[dict[str, Any]]:
        """Declare files this phase will modify."""
        return sched_mod.declare_files(self.conn, agent_id, phase_id, file_paths)

    def check_conflicts(
        self,
        file_paths: list[str],
        exclude_phase_id: int | None = None,
    ) -> dict[str, Any]:
        """Check for active file locks on given files."""
        conflicts = sched_mod.check_conflicts(self.conn, file_paths, exclude_phase_id)
        return {
            "has_conflicts": len(conflicts) > 0,
            "conflicts": conflicts,
            "message": (
                f"{len(conflicts)} conflict(s) detected." if conflicts
                else "No conflicts detected."
            ),
        }

    def get_phase_metrics(self, ticket_id: str | None = None) -> dict[str, Any]:
        """Get phase duration statistics."""
        conditions = []
        params: list[Any] = []

        if ticket_id:
            conditions.append("ticket_id = ?")
            params.append(ticket_id)

        where = "WHERE status = 'completed'" + (
            " AND " + " AND ".join(conditions) if conditions else ""
        )

        cursor = self.conn.execute(
            f"""
            SELECT phase_name,
                   COUNT(*) AS count,
                   AVG(CAST(
                       (julianday(completed_at) - julianday(started_at)) * 24 * 60
                       AS REAL)) AS avg_minutes,
                   MIN(CAST(
                       (julianday(completed_at) - julianday(started_at)) * 24 * 60
                       AS REAL)) AS min_minutes,
                   MAX(CAST(
                       (julianday(completed_at) - julianday(started_at)) * 24 * 60
                       AS REAL)) AS max_minutes
            FROM phases
            {where}
              AND started_at IS NOT NULL
              AND completed_at IS NOT NULL
            GROUP BY phase_name
            ORDER BY avg_minutes DESC NULLS LAST
            """,
            params,
        )
        return {"metrics": [dict(r) for r in cursor.fetchall()]}

    def get_audit_log(
        self,
        ticket_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Query the audit trail."""
        return audit_mod.query_audit(self.conn, ticket_id=ticket_id, limit=limit)

    def update_ticket_metadata(
        self,
        ticket_id: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Update project-specific custom_metadata for a ticket.

        Merges the provided dict into existing custom_metadata.
        Used by agents to track project-specific state like revision counts.
        """
        existing_row = self.conn.execute(
            "SELECT custom_metadata FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()

        if existing_row is None:
            return {"error": f"Ticket '{ticket_id}' not found"}

        existing: dict = {}
        if existing_row["custom_metadata"]:
            try:
                existing = json.loads(existing_row["custom_metadata"])
            except Exception:
                pass

        existing.update(metadata)

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                """
                UPDATE tickets
                SET custom_metadata = ?, updated_at = datetime('now')
                WHERE id = ?
                """,
                (json.dumps(existing), ticket_id),
            )
            self.conn.execute("COMMIT")
        except Exception:
            try:
                self.conn.execute("ROLLBACK")
            except Exception:
                pass
            raise

        return {"ticket_id": ticket_id, "updated_metadata": existing}

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

    def run_scheduler(self, ticket_id: str | None = None) -> dict[str, Any]:
        """
        Seed phases for all tickets and resolve phase availability.

        Pass ticket_id to limit to a single ticket.
        """
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

    # -----------------------------------------------------------------------
    # Git/GitHub operations
    # -----------------------------------------------------------------------

    def setup_branch(self, ticket_id: str) -> dict[str, Any]:
        """Create or check out a git branch for the given ticket."""
        return github_mod.setup_branch(self.project_root, ticket_id, self.conn)

    def commit_and_push(
        self,
        agent_id: str,
        phase_id: int,
        file_paths: list[str],
        message: str | None = None,
    ) -> dict[str, Any]:
        """Stage files, commit, and push to remote. Triggers traceability reindex."""
        result = github_mod.commit_and_push(
            self.project_root, self.conn, agent_id, phase_id, file_paths, message
        )

        # Trigger incremental traceability reindex after successful commit
        if self.trace_server is not None and result.get("status") != "error":
            try:
                trace_result = self.trace_reindex(skip_symbols=True)
                result["traceability_reindex"] = trace_result
            except Exception as e:
                result["traceability_reindex_error"] = str(e)

        return result

    def create_or_update_pr(
        self,
        agent_id: str,
        phase_id: int,
        title: str | None = None,
        body: str | None = None,
        draft: bool = True,
    ) -> dict[str, Any]:
        """Create a new PR or update an existing one."""
        return github_mod.create_or_update_pr(
            self.project_root, self.conn, agent_id, phase_id, title, body, draft
        )

    def post_pr_comment(self, body: str) -> dict[str, Any]:
        """Post a comment on the PR for the current branch."""
        return github_mod.post_pr_comment(self.project_root, body)

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

    # -------------------------------------------------------------------
    # Traceability tools (delegated to TraceabilityServer)
    # -------------------------------------------------------------------

    def _require_trace(self) -> TraceabilityServer:
        """Return the traceability server or raise an error."""
        if self.trace_server is None:
            raise RuntimeError(
                "Traceability not configured. Add a 'traceability' section "
                "to .workflow/config.yaml with db_path."
            )
        return self.trace_server

    def trace_search_decisions(
        self, query: str, ticket: str | None = None, status: str | None = None
    ) -> list[dict]:
        return self._require_trace().search_decisions(query, ticket, status)

    def trace_get_decision(self, dd_id: str) -> dict:
        return self._require_trace().get_decision(dd_id)

    def trace_get_symbol_history(self, qualified_name: str) -> list[dict]:
        return self._require_trace().get_symbol_history(qualified_name)

    def trace_get_ticket_impact(self, ticket_number: str) -> dict:
        return self._require_trace().get_ticket_impact(ticket_number)

    def trace_get_commit_context(self, commit_sha: str) -> dict:
        return self._require_trace().get_commit_context(commit_sha)

    def trace_why_symbol(self, qualified_name: str) -> dict:
        return self._require_trace().why_symbol(qualified_name)

    def trace_get_snapshot_symbols(
        self, commit_sha: str, file_path: str | None = None
    ) -> list[dict]:
        return self._require_trace().get_snapshot_symbols(commit_sha, file_path)

    def trace_get_record_mappings(self, record_name: str) -> dict:
        return self._require_trace().get_record_mappings(record_name)

    def trace_check_record_drift(self) -> list[dict]:
        return self._require_trace().check_record_drift()

    def trace_reindex(self, skip_symbols: bool = False) -> dict:
        """Run incremental traceability reindexing."""
        self._require_trace()
        return trace_reindex_all(
            self.trace_conn,
            str(self.project_root),
            source_dir=self.config.traceability_source_dir,
            designs_dir=self.config.traceability_designs_dir,
            tickets_dir=str(Path(self.config.tickets_directory).relative_to(self.project_root))
            if Path(self.config.tickets_directory).is_absolute()
            else self.config.tickets_directory,
            models_path=self.config.traceability_models_path,
            generated_models_path=self.config.traceability_generated_models_path,
            skip_symbols=skip_symbols,
        )


# ---------------------------------------------------------------------------
# FastMCP server factory
# ---------------------------------------------------------------------------


def create_mcp_server(db_path: str, project_root: str) -> "FastMCP":
    """
    Create a FastMCP server wrapping the WorkflowServer.

    Follows the same class + factory pattern as guidelines_server.py.
    """
    ws = WorkflowServer(db_path, project_root)
    mcp = FastMCP("workflow")

    @mcp.tool()
    def register_agent(
        agent_type: str,
        agent_id: str | None = None,
        metadata: str | None = None,
    ) -> str:
        """
        Register an agent instance with the work queue.

        Call this before claiming any work. Returns an agent_id you will use
        for all subsequent calls.

        Args:
            agent_type: Type of work this agent handles (e.g. 'cpp-architect',
                        'cpp-implementer', 'design-reviewer')
            agent_id: Optional — provide to re-register a known agent ID.
                      If omitted, a new UUID is generated.
            metadata: Optional JSON string with agent metadata (model, worktree path, etc.)
        """
        meta_dict = json.loads(metadata) if metadata else None
        return json.dumps(
            ws.register_agent(agent_type, agent_id, meta_dict), indent=2
        )

    @mcp.tool()
    def list_available_work(agent_type: str, limit: int = 20) -> str:
        """
        List phases available for claiming by this agent type.

        Returns phases ordered by ticket priority (Critical > High > Medium > Low),
        then by phase ID. Does NOT claim — use claim_phase to actually reserve work.

        Args:
            agent_type: Your agent type (e.g. 'cpp-architect')
            limit: Maximum phases to return (default: 20)
        """
        return json.dumps(ws.list_available_work(agent_type, limit), indent=2)

    @mcp.tool()
    def claim_phase(agent_id: str, phase_id: int | None = None) -> str:
        """
        Atomically claim a phase from the work queue.

        Uses BEGIN IMMEDIATE transaction to prevent double-claiming (validated in P1
        prototype: 40/40 trials with 0 duplicates under concurrent access).

        If phase_id is provided, claims that specific phase.
        If phase_id is omitted, claims the highest-priority available phase for
        your agent type.

        Args:
            agent_id: Your agent ID (from register_agent)
            phase_id: Optional specific phase ID to claim (from list_available_work)
        """
        return json.dumps(ws.claim_phase(agent_id, phase_id), indent=2)

    @mcp.tool()
    def heartbeat(agent_id: str) -> str:
        """
        Update agent liveness timestamp.

        Call periodically to prevent stale agent detection. With heartbeat_implicit=true
        (default), all MCP tool calls implicitly update the heartbeat, so explicit
        calls are rarely needed.

        Args:
            agent_id: Your agent ID
        """
        return json.dumps(ws.heartbeat(agent_id), indent=2)

    @mcp.tool()
    def start_phase(agent_id: str, phase_id: int) -> str:
        """
        Mark a claimed phase as running.

        Call this when you begin the actual phase work (after claim_phase).

        Args:
            agent_id: Your agent ID
            phase_id: The phase ID you claimed
        """
        return json.dumps(ws.start_phase(agent_id, phase_id), indent=2)

    @mcp.tool()
    def complete_phase(
        agent_id: str,
        phase_id: int,
        result_summary: str | None = None,
        artifacts: str | None = None,
    ) -> str:
        """
        Report successful phase completion.

        Automatically resolves availability for downstream phases.

        Args:
            agent_id: Your agent ID
            phase_id: The phase ID you completed
            result_summary: Brief description of what was accomplished
            artifacts: JSON array of file paths produced (e.g. '["docs/designs/foo/design.md"]')
        """
        artifact_list = json.loads(artifacts) if artifacts else None
        return json.dumps(
            ws.complete_phase(agent_id, phase_id, result_summary, artifact_list), indent=2
        )

    @mcp.tool()
    def fail_phase(
        agent_id: str,
        phase_id: int,
        error_details: str | None = None,
    ) -> str:
        """
        Report phase failure.

        Call this if you cannot complete the phase. The phase status will be
        set to 'failed'. Stale recovery or human intervention will be needed
        to proceed.

        Args:
            agent_id: Your agent ID
            phase_id: The phase ID that failed
            error_details: Description of what went wrong
        """
        return json.dumps(ws.fail_phase(agent_id, phase_id, error_details), indent=2)

    @mcp.tool()
    def release_phase(agent_id: str, phase_id: int) -> str:
        """
        Release a claimed phase back to available.

        Use this if you claimed a phase but cannot or should not execute it
        (e.g. missing prerequisites discovered after claiming).

        Args:
            agent_id: Your agent ID
            phase_id: The phase ID to release
        """
        return json.dumps(ws.release_phase(agent_id, phase_id), indent=2)

    @mcp.tool()
    def request_human_review(
        phase_id: int,
        gate_type: str,
        context: str | None = None,
    ) -> str:
        """
        Create a human gate blocking downstream phases.

        Call this when the current phase requires human review before work
        can proceed. Downstream phases will remain 'blocked' until the gate
        is approved via the CLI: workflow-engine approve <gate-id>

        Args:
            phase_id: The phase that needs human review
            gate_type: Type of review (e.g. 'design_review', 'prototype_review')
            context: Optional JSON string with context for the reviewer
        """
        context_dict = json.loads(context) if context else None
        return json.dumps(
            ws.request_human_review(phase_id, gate_type, context_dict), indent=2
        )

    @mcp.tool()
    def approve_gate(
        gate_id: int,
        decided_by: str,
        notes: str | None = None,
    ) -> str:
        """
        Approve a pending human review gate, unblocking the next phase.

        Only a human reviewer should trigger this tool. Use list_blocked to
        find pending gates and their IDs.

        Args:
            gate_id: The gate ID to approve (from list_blocked or get_ticket_status)
            decided_by: Name of the human reviewer approving the gate
            notes: Optional approval notes or comments
        """
        return json.dumps(
            ws.approve_gate(gate_id, decided_by, notes), indent=2
        )

    @mcp.tool()
    def reject_gate(
        gate_id: int,
        decided_by: str,
        notes: str | None = None,
    ) -> str:
        """
        Reject a pending human review gate with feedback.

        The associated phase remains blocked. The upstream work may need to
        be revised to address the feedback.

        Args:
            gate_id: The gate ID to reject (from list_blocked or get_ticket_status)
            decided_by: Name of the human reviewer rejecting the gate
            notes: Feedback explaining why the gate was rejected
        """
        return json.dumps(
            ws.reject_gate(gate_id, decided_by, notes), indent=2
        )

    @mcp.tool()
    def get_pr_reviews() -> str:
        """
        Fetch all reviews and comments from the current branch's PR.

        Returns PR reviews (approve/request changes), conversation comments,
        and inline code review comments. Use this to check feedback before
        approving or requesting revisions on a gate.
        """
        return json.dumps(
            ws.get_pr_reviews(), indent=2, default=str
        )

    @mcp.tool()
    def reply_to_review_comment(
        comment_id: int,
        body: str,
    ) -> str:
        """
        Reply to an inline review comment on the current branch's PR.

        The reply is posted as a threaded response to the specific review
        comment, attributed to Claude. Use get_pr_reviews to find comment IDs.

        Args:
            comment_id: The review comment ID to reply to (from get_pr_reviews review_comments[].id)
            body: The reply text (Claude signature is appended automatically)
        """
        return json.dumps(
            ws.reply_to_review_comment(comment_id, body), indent=2
        )

    @mcp.tool()
    def request_gate_revisions(
        gate_id: int,
        decided_by: str,
        notes: str | None = None,
    ) -> str:
        """
        Request revisions on a human review gate, pulling PR comments as context.

        Fetches all reviews and comments from the current branch's PR,
        combines them with any additional notes, and marks the gate as
        changes_requested. The associated phase remains blocked.

        Args:
            gate_id: The gate ID (from list_blocked or get_ticket_status)
            decided_by: Name of the human reviewer requesting revisions
            notes: Optional additional notes beyond the PR comments
        """
        return json.dumps(
            ws.request_gate_revisions(gate_id, decided_by, notes), indent=2
        )

    @mcp.tool()
    def get_ticket_status(ticket_id: str) -> str:
        """
        Get full ticket status with all phases and gates.

        Args:
            ticket_id: Ticket ID (e.g. '0083')
        """
        return json.dumps(ws.get_ticket_status(ticket_id), indent=2, default=str)

    @mcp.tool()
    def list_tickets(
        status_filter: str | None = None,
        priority: str | None = None,
        component: str | None = None,
        limit: int = 50,
    ) -> str:
        """
        Query tickets by criteria.

        Args:
            status_filter: Filter by current_status substring (e.g. 'Implementation')
            priority: Filter by priority ('Low', 'Medium', 'High', 'Critical')
            component: Filter by component substring
            limit: Maximum results (default: 50)
        """
        return json.dumps(
            ws.list_tickets(status_filter, priority, component, limit), indent=2
        )

    @mcp.tool()
    def list_blocked() -> str:
        """
        List all phases blocked on human gates or unresolved ticket dependencies.

        Shows pending human gates (needing review approval) and tickets blocked
        by inter-ticket dependencies.
        """
        return json.dumps(ws.list_blocked(), indent=2, default=str)

    @mcp.tool()
    def list_agents() -> str:
        """List registered agents and their current assignments."""
        return json.dumps(ws.list_agents(), indent=2, default=str)

    @mcp.tool()
    def declare_files(
        agent_id: str,
        phase_id: int,
        file_paths: str,
    ) -> str:
        """
        Declare files this phase will modify (for conflict detection).

        Call this after claiming a phase to register which files you intend to
        modify. Other agents can then check for conflicts.

        Args:
            agent_id: Your agent ID
            phase_id: Your claimed phase ID
            file_paths: JSON array of file paths (e.g. '["src/foo.cpp", "src/foo.h"]')
        """
        paths = json.loads(file_paths)
        return json.dumps(ws.declare_files(agent_id, phase_id, paths), indent=2)

    @mcp.tool()
    def check_conflicts(
        file_paths: str,
        exclude_phase_id: int | None = None,
    ) -> str:
        """
        Check if any active phase holds locks on the given files.

        Returns conflicts if found — empty list means no conflicts.

        Args:
            file_paths: JSON array of file paths to check
            exclude_phase_id: Optional phase ID to exclude from conflict check
                              (your own phase)
        """
        paths = json.loads(file_paths)
        return json.dumps(ws.check_conflicts(paths, exclude_phase_id), indent=2)

    @mcp.tool()
    def get_phase_metrics(ticket_id: str | None = None) -> str:
        """
        Get phase duration statistics.

        Returns average, min, and max duration in minutes per phase type.
        Useful for identifying workflow bottlenecks.

        Args:
            ticket_id: Optional — limit to a specific ticket
        """
        return json.dumps(ws.get_phase_metrics(ticket_id), indent=2, default=str)

    @mcp.tool()
    def get_audit_log(ticket_id: str | None = None, limit: int = 50) -> str:
        """
        Query the audit trail.

        Returns all state transitions in reverse chronological order.

        Args:
            ticket_id: Optional — filter to a specific ticket
            limit: Maximum entries (default: 50)
        """
        return json.dumps(ws.get_audit_log(ticket_id, limit), indent=2, default=str)

    @mcp.tool()
    def update_ticket_metadata(ticket_id: str, metadata: str) -> str:
        """
        Update project-specific metadata fields for a ticket.

        Merges the provided dict into existing custom_metadata. Used by agents
        to track project-specific state (e.g. design revision counts, previous
        design approaches).

        Args:
            ticket_id: Ticket ID (e.g. '0083')
            metadata: JSON object with fields to update/add
        """
        meta_dict = json.loads(metadata)
        return json.dumps(ws.update_ticket_metadata(ticket_id, meta_dict), indent=2)

    @mcp.tool()
    def import_tickets() -> str:
        """
        Import/refresh all tickets from markdown files in the tickets/ directory.

        Reads each ticket .md file, parses metadata and status checkboxes,
        and creates/updates records in the database. Idempotent — safe to run
        multiple times.
        """
        return json.dumps(ws.import_tickets(), indent=2)

    @mcp.tool()
    def run_scheduler(ticket_id: str | None = None) -> str:
        """
        Seed phases for tickets and resolve phase availability.

        For each ticket (or the specified ticket):
        1. Creates phase rows per phases.yaml (if not already seeded)
        2. Evaluates conditions (e.g. 'Requires Math Design: Yes')
        3. Marks phases as 'available' when prerequisites are met
        4. Creates human_gates records for human gate phases

        Run after import_tickets to make work available for agents.

        Args:
            ticket_id: Optional — limit to a single ticket
        """
        return json.dumps(ws.run_scheduler(ticket_id), indent=2)

    @mcp.tool()
    def cleanup_stale() -> str:
        """
        Release stale agent claims.

        Finds agents whose last_heartbeat exceeds the configured stale timeout
        (default: 30 minutes) and releases their claimed phases back to available.
        """
        return json.dumps(ws.cleanup_stale(), indent=2)

    # -----------------------------------------------------------------------
    # Git/GitHub tools
    # -----------------------------------------------------------------------

    @mcp.tool()
    def setup_branch(ticket_id: str) -> str:
        """
        Create or check out a git branch for a ticket.

        Derives the branch name from the ticket's full_name (underscores become
        hyphens). If the branch already exists, checks it out. Otherwise creates
        it from main.

        Args:
            ticket_id: Ticket ID (e.g. '0041')
        """
        return json.dumps(ws.setup_branch(ticket_id), indent=2)

    @mcp.tool()
    def commit_and_push(
        agent_id: str,
        phase_id: int,
        file_paths: str,
        message: str | None = None,
    ) -> str:
        """
        Stage files, commit, and push to remote.

        If no message is provided, auto-generates one from the phase and ticket
        info (e.g. 'impl: feature name').

        Args:
            agent_id: Your agent ID
            phase_id: Your claimed phase ID
            file_paths: JSON array of file paths to stage (e.g. '["src/foo.cpp"]')
            message: Optional commit message (auto-generated if omitted)
        """
        paths = json.loads(file_paths)
        return json.dumps(
            ws.commit_and_push(agent_id, phase_id, paths, message), indent=2
        )

    @mcp.tool()
    def create_or_update_pr(
        agent_id: str,
        phase_id: int,
        title: str | None = None,
        body: str | None = None,
        draft: bool = True,
    ) -> str:
        """
        Create a new PR or update an existing one for the current branch.

        If a PR already exists, returns its info. If draft=False and a draft PR
        exists, marks it as ready for review. Auto-generates title and body from
        ticket/phase info if not provided.

        Args:
            agent_id: Your agent ID
            phase_id: Your claimed phase ID
            title: Optional PR title (auto-generated if omitted)
            body: Optional PR body (auto-generated if omitted)
            draft: Whether to create as draft PR (default: True)
        """
        return json.dumps(
            ws.create_or_update_pr(agent_id, phase_id, title, body, draft),
            indent=2,
        )

    @mcp.tool()
    def post_pr_comment(body: str) -> str:
        """
        Post a comment on the PR associated with the current branch.

        Finds the PR for the current branch and posts the given comment body.

        Args:
            body: The comment text to post
        """
        return json.dumps(ws.post_pr_comment(body), indent=2)

    # ----- Traceability tools -----
    # Only registered if traceability is configured

    if ws.trace_server is not None:
        @mcp.tool()
        def search_decisions(
            query: str, ticket: str | None = None, status: str | None = None
        ) -> str:
            """FTS search across design decision rationale, alternatives, and trade-offs."""
            return json.dumps(
                ws.trace_search_decisions(query, ticket, status), indent=2, default=str
            )

        @mcp.tool()
        def get_decision(dd_id: str) -> str:
            """Get full details of a design decision with linked symbols and commits."""
            return json.dumps(ws.trace_get_decision(dd_id), indent=2, default=str)

        @mcp.tool()
        def get_symbol_history(qualified_name: str) -> str:
            """Timeline of changes to a symbol across commits (supports % wildcards)."""
            return json.dumps(
                ws.trace_get_symbol_history(qualified_name), indent=2, default=str
            )

        @mcp.tool()
        def get_ticket_impact(ticket_number: str) -> str:
            """All commits, file changes, symbol changes, and decisions for a ticket."""
            return json.dumps(
                ws.trace_get_ticket_impact(ticket_number), indent=2, default=str
            )

        @mcp.tool()
        def get_commit_context(commit_sha: str) -> str:
            """Context for a commit: ticket, phase, file changes, symbol changes, decisions."""
            return json.dumps(
                ws.trace_get_commit_context(commit_sha), indent=2, default=str
            )

        @mcp.tool()
        def why_symbol(qualified_name: str) -> str:
            """Design decision(s) that created or modified a symbol, with rationale."""
            return json.dumps(ws.trace_why_symbol(qualified_name), indent=2, default=str)

        @mcp.tool()
        def get_snapshot_symbols(
            commit_sha: str, file_path: str | None = None
        ) -> str:
            """All symbols at a specific point in time, optionally filtered by file."""
            return json.dumps(
                ws.trace_get_snapshot_symbols(commit_sha, file_path), indent=2, default=str
            )

        @mcp.tool()
        def get_record_mappings(record_name: str) -> str:
            """Return all four layers' field lists for a record with drift analysis."""
            return json.dumps(
                ws.trace_get_record_mappings(record_name), indent=2, default=str
            )

        @mcp.tool()
        def check_record_drift() -> str:
            """Return all records with fields missing from downstream layers."""
            return json.dumps(ws.trace_check_record_drift(), indent=2, default=str)

    # MCP Resources
    @mcp.resource("workflow://dashboard")
    def dashboard() -> str:
        """Summary dashboard: active agents, pending gates, phase counts by status."""
        return json.dumps(ws.get_dashboard(), indent=2)

    @mcp.resource("workflow://ticket/{ticket_id}")
    def ticket_resource(ticket_id: str) -> str:
        """Full ticket state with all phases and gates."""
        return json.dumps(ws.get_ticket_status(ticket_id), indent=2, default=str)

    @mcp.resource("workflow://queue/{agent_type}")
    def queue_resource(agent_type: str) -> str:
        """Work queue for a specific agent type."""
        return json.dumps(ws.list_available_work(agent_type), indent=2)

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
