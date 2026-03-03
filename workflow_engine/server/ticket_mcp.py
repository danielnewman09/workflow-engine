"""
Ticket query and update MCP tool registrations.

Called from ``create_mcp_server()`` to register ticket management tools.
"""

import json
from typing import Any, TYPE_CHECKING

from workflow_engine.engine import scheduler as sched_mod
from workflow_engine.utils.sqlite import rows_to_dicts

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from workflow_engine.server.server import WorkflowServer


def register_ticket_tools(mcp: "FastMCP", ws: "WorkflowServer") -> None:
    """Register ticket query and update MCP tools on *mcp* using *ws* state."""

    @mcp.tool()
    def get_ticket_status(ticket_id: str) -> str:
        """
        Get full ticket status with all phases and gates.

        Args:
            ticket_id: Ticket ID (e.g. '0083')
        """
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
        cursor = ws.conn.execute(
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
        return json.dumps(rows_to_dicts(cursor.fetchall()), indent=2)

    @mcp.tool()
    def list_blocked() -> str:
        """
        List all phases blocked on human gates or unresolved ticket dependencies.

        Shows pending human gates (needing review approval) and tickets blocked
        by inter-ticket dependencies.
        """
        gate_blocked = ws.conn.execute(
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

        dep_blocked = ws.conn.execute(
            """
            SELECT DISTINCT t.id AS ticket_id, t.full_name,
                            d.blocking_ticket_id, d.dependency_type
            FROM dependencies d
            JOIN tickets t ON t.id = d.blocked_ticket_id
            WHERE d.resolved = 0
            ORDER BY t.id ASC
            """
        ).fetchall()

        return json.dumps({
            "gate_blocked": rows_to_dicts(gate_blocked),
            "dependency_blocked": rows_to_dicts(dep_blocked),
        }, indent=2, default=str)

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

        existing_row = ws.conn.execute(
            "SELECT custom_metadata FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()

        if existing_row is None:
            return json.dumps({"error": f"Ticket '{ticket_id}' not found"}, indent=2)

        existing: dict = {}
        if existing_row["custom_metadata"]:
            try:
                existing = json.loads(existing_row["custom_metadata"])
            except Exception:
                pass

        existing.update(meta_dict)

        ws.conn.execute("BEGIN IMMEDIATE")
        try:
            ws.conn.execute(
                """
                UPDATE tickets
                SET custom_metadata = ?, updated_at = datetime('now')
                WHERE id = ?
                """,
                (json.dumps(existing), ticket_id),
            )
            ws.conn.execute("COMMIT")
        except Exception:
            try:
                ws.conn.execute("ROLLBACK")
            except Exception:
                pass
            raise

        return json.dumps({"ticket_id": ticket_id, "updated_metadata": existing}, indent=2)

    @mcp.tool()
    def import_tickets() -> str:
        """
        Import/refresh all tickets from markdown files in the tickets/ directory.

        Reads each ticket .md file, parses metadata and status checkboxes,
        and creates/updates records in the database. Idempotent — safe to run
        multiple times.
        """
        results = sched_mod.import_all_tickets(ws.conn, ws.config)
        created = sum(1 for r in results if r.get("action") == "created")
        updated = sum(1 for r in results if r.get("action") == "updated")
        errors = sum(1 for r in results if r.get("action") == "error")
        return json.dumps({
            "imported": len(results),
            "created": created,
            "updated": updated,
            "errors": errors,
            "results": results,
        }, indent=2)

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
        seeded_all = []

        if ticket_id:
            ticket_ids = [ticket_id]
        else:
            ticket_ids = [
                row["id"] for row in ws.conn.execute("SELECT id FROM tickets").fetchall()
            ]

        for tid in ticket_ids:
            seeded = sched_mod.seed_phases(ws.conn, tid, ws.config)
            seeded_all.extend(seeded)

        transitions = sched_mod.resolve_availability(ws.conn, ticket_id)

        return json.dumps({
            "phases_seeded": len(seeded_all),
            "availability_transitions": len(transitions),
            "transitions": transitions,
        }, indent=2)

    @mcp.tool()
    def cleanup_stale() -> str:
        """
        Release stale agent claims.

        Finds agents whose last_heartbeat exceeds the configured stale timeout
        (default: 30 minutes) and releases their claimed phases back to available.
        """
        released = sched_mod.cleanup_stale_agents(
            ws.conn,
            stale_timeout_minutes=ws.config.stale_timeout_minutes,
        )
        return json.dumps({
            "released": len(released),
            "phases": released,
        }, indent=2)
