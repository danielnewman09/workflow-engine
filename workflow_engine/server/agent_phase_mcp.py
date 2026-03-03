"""
Agent lifecycle and phase management MCP tool registrations.

Called from ``create_mcp_server()`` to register agent registration,
phase claiming/completion, human gates, conflict detection, metrics,
audit log, and build log tools.
"""

import json
import uuid
from typing import Any, TYPE_CHECKING

from workflow_engine.engine import audit as audit_mod
from workflow_engine.engine import claim as claim_mod
from workflow_engine.engine import scheduler as sched_mod
from workflow_engine.utils.sqlite import rows_to_dicts

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from workflow_engine.server.server import WorkflowServer


def register_agent_phase_tools(mcp: "FastMCP", ws: "WorkflowServer") -> None:
    """Register agent lifecycle and phase management MCP tools."""

    # -------------------------------------------------------------------
    # Agent registration and lifecycle
    # -------------------------------------------------------------------

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
        aid = agent_id or f"agent-{uuid.uuid4().hex[:12]}"
        result = sched_mod.register_agent(ws.conn, aid, agent_type, meta_dict)
        result["message"] = (
            f"Agent '{aid}' registered as '{agent_type}'. "
            "Call list_available_work to see available phases."
        )
        return json.dumps(result, indent=2)

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
        updated = sched_mod.heartbeat(ws.conn, agent_id)
        return json.dumps({
            "agent_id": agent_id,
            "updated": updated,
            "message": "Heartbeat recorded." if updated else f"Agent '{agent_id}' not found or is stale.",
        }, indent=2)

    @mcp.tool()
    def list_agents() -> str:
        """List registered agents and their current assignments."""
        cursor = ws.conn.execute(
            """
            SELECT a.id, a.agent_type, a.status, a.last_heartbeat,
                   p.phase_name AS current_phase, p.ticket_id AS current_ticket
            FROM agents a
            LEFT JOIN phases p ON p.id = a.current_phase_id
            WHERE a.status != 'terminated'
            ORDER BY a.status DESC, a.last_heartbeat DESC
            """
        )
        return json.dumps(rows_to_dicts(cursor.fetchall()), indent=2, default=str)

    # -------------------------------------------------------------------
    # Phase lifecycle
    # -------------------------------------------------------------------

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
        return json.dumps(
            claim_mod.list_available(ws.conn, agent_type, limit), indent=2
        )

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
        # Look up agent type
        agent_row = ws.conn.execute(
            "SELECT agent_type FROM agents WHERE id = ?", (agent_id,)
        ).fetchone()

        if agent_row is None:
            return json.dumps({
                "success": False,
                "reason": f"Agent '{agent_id}' not registered. Call register_agent first.",
            }, indent=2)

        agent_type = agent_row["agent_type"]

        if phase_id is not None:
            result = claim_mod.claim_specific(ws.conn, agent_id, phase_id)
        else:
            result = claim_mod.claim_next(ws.conn, agent_id, agent_type)

        if result.success:
            phase = result.phase  # type: ignore[union-attr]
            return json.dumps({
                "success": True,
                "phase_id": phase.id,
                "ticket_id": phase.ticket_id,
                "phase_name": phase.phase_name,
                "agent_type": agent_type,
                "message": (
                    f"Claimed '{phase.phase_name}' for ticket {phase.ticket_id}. "
                    "Call start_phase when you begin work."
                ),
            }, indent=2)
        else:
            return json.dumps({
                "success": False,
                "reason": str(result),
            }, indent=2)

    @mcp.tool()
    def start_phase(agent_id: str, phase_id: int) -> str:
        """
        Mark a claimed phase as running.

        Call this when you begin the actual phase work (after claim_phase).

        Args:
            agent_id: Your agent ID
            phase_id: The phase ID you claimed
        """
        return json.dumps(
            sched_mod.start_phase(ws.conn, agent_id, phase_id), indent=2
        )

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
        result = sched_mod.complete_phase(
            ws.conn, agent_id, phase_id, result_summary, artifact_list
        )
        # Resolve downstream availability
        ticket_id = result["ticket_id"]
        transitions = sched_mod.resolve_availability(ws.conn, ticket_id)
        result["downstream_transitions"] = transitions
        result["message"] = (
            f"Phase '{result['phase_name']}' completed for ticket {ticket_id}. "
            f"{len(transitions)} downstream phase(s) updated."
        )
        return json.dumps(result, indent=2)

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
        return json.dumps(
            sched_mod.fail_phase(ws.conn, agent_id, phase_id, error_details), indent=2
        )

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
        released = claim_mod.release_phase(ws.conn, agent_id, phase_id)
        return json.dumps({
            "released": released,
            "phase_id": phase_id,
            "agent_id": agent_id,
            "message": "Phase released back to available." if released else "Release failed — phase not held by this agent.",
        }, indent=2)

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
        return json.dumps(
            sched_mod.declare_files(ws.conn, agent_id, phase_id, paths), indent=2
        )

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
        conflicts = sched_mod.check_conflicts(ws.conn, paths, exclude_phase_id)
        return json.dumps({
            "has_conflicts": len(conflicts) > 0,
            "conflicts": conflicts,
            "message": (
                f"{len(conflicts)} conflict(s) detected." if conflicts
                else "No conflicts detected."
            ),
        }, indent=2)

    # -------------------------------------------------------------------
    # Human gates
    # -------------------------------------------------------------------

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

        phase = ws.conn.execute(
            "SELECT ticket_id, phase_name FROM phases WHERE id = ?", (phase_id,)
        ).fetchone()

        if phase is None:
            return json.dumps({"error": f"Phase {phase_id} not found"}, indent=2)

        context_json = json.dumps(context_dict) if context_dict else None

        ws.conn.execute("BEGIN IMMEDIATE")
        try:
            ws.conn.execute(
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
                ws.conn,
                actor="agent",
                action="create_gate",
                entity_type="gate",
                entity_id=f"{phase['ticket_id']}/{phase['phase_name']}",
                details={"gate_type": gate_type, "phase_id": phase_id},
            )

            ws.conn.execute("COMMIT")
        except Exception:
            try:
                ws.conn.execute("ROLLBACK")
            except Exception:
                pass
            raise

        return json.dumps({
            "gate_created": True,
            "ticket_id": phase["ticket_id"],
            "phase_id": phase_id,
            "gate_type": gate_type,
            "message": (
                "Human review gate created. "
                "Run: workflow-engine gates  to see pending reviews."
            ),
        }, indent=2)

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
            sched_mod.resolve_gate(ws.conn, gate_id, "approved", decided_by, notes),
            indent=2,
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
            sched_mod.resolve_gate(ws.conn, gate_id, "rejected", decided_by, notes),
            indent=2,
        )

    # -------------------------------------------------------------------
    # Metrics, audit, and build log
    # -------------------------------------------------------------------

    @mcp.tool()
    def get_phase_metrics(ticket_id: str | None = None) -> str:
        """
        Get phase duration statistics.

        Returns average, min, and max duration in minutes per phase type.
        Useful for identifying workflow bottlenecks.

        Args:
            ticket_id: Optional — limit to a specific ticket
        """
        conditions = []
        params: list[Any] = []

        if ticket_id:
            conditions.append("ticket_id = ?")
            params.append(ticket_id)

        where = "WHERE status = 'completed'" + (
            " AND " + " AND ".join(conditions) if conditions else ""
        )

        cursor = ws.conn.execute(
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
        return json.dumps({"metrics": rows_to_dicts(cursor.fetchall())}, indent=2, default=str)

    @mcp.tool()
    def get_audit_log(ticket_id: str | None = None, limit: int = 50) -> str:
        """
        Query the audit trail.

        Returns all state transitions in reverse chronological order.

        Args:
            ticket_id: Optional — filter to a specific ticket
            limit: Maximum entries (default: 50)
        """
        return json.dumps(
            audit_mod.query_audit(ws.conn, ticket_id=ticket_id, limit=limit),
            indent=2,
            default=str,
        )

    @mcp.tool()
    def log_build_attempt(
        phase_id: int,
        agent_id: str,
        hypothesis: str | None = None,
        files_changed: str | None = None,
        build_result: str = "fail",
        compiler_output: str | None = None,
    ) -> str:
        """
        Log an implementation build attempt to the impl_build_log table.

        Call this after each `cmake --build` attempt during implementation.
        Returns the attempt number and whether circle detection triggered.

        If circle_detected is true, the implementer MUST stop and produce
        implementation-findings.md for human escalation.

        Args:
            phase_id: Your claimed phase ID
            agent_id: Your agent ID
            hypothesis: What you intended to fix/implement in this attempt
            files_changed: JSON array of file paths you modified (e.g. '["src/foo.cpp", "src/bar.hpp"]')
            build_result: 'pass' or 'fail'
            compiler_output: First ~4000 chars of compiler stdout/stderr
        """
        from workflow_engine.engine.schema import insert_build_attempt

        files_list = json.loads(files_changed) if files_changed else None

        # Look up ticket_id from the phase
        phase = ws.conn.execute(
            "SELECT ticket_id FROM phases WHERE id = ?", (phase_id,)
        ).fetchone()
        if phase is None:
            return json.dumps({"error": f"Phase {phase_id} not found"}, indent=2)

        ticket_id = phase["ticket_id"]
        return json.dumps(
            insert_build_attempt(
                ws.conn,
                phase_id=phase_id,
                agent_id=agent_id,
                ticket_id=ticket_id,
                hypothesis=hypothesis,
                files_changed=files_list,
                build_result=build_result,
                compiler_output=compiler_output,
            ),
            indent=2,
        )
