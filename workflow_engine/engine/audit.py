#!/usr/bin/env python3
# Ticket: 0083_database_agent_orchestration
# Design: docs/designs/0083_database_agent_orchestration/design.md
"""
Workflow Engine Audit Log Helpers

All state transitions in the workflow engine are recorded in the audit_log table.
The audit trail is append-only and never modified after insertion.

Actors:
- Agent IDs (e.g. "agent-uuid-1234") for agent-initiated actions
- "human:{name}" for human CLI actions
- "scheduler" for automated scheduler actions

Actions:
- register_agent     — agent registers with the server
- claim_phase        — agent atomically claims an available phase
- start_phase        — agent begins execution
- complete_phase     — agent reports successful completion
- fail_phase         — agent reports failure
- release_phase      — agent or stale recovery releases a claimed phase
- skip_phase         — scheduler marks a conditional phase as skipped
- create_gate        — human gate created for a phase
- approve_gate       — human approves a gate
- reject_gate        — human rejects a gate
- request_changes    — human requests changes on a gate
- seed_phases        — scheduler seeds phases for a ticket
- resolve_dependency — inter-ticket dependency resolved
- stale_agent        — scheduler marks agent as stale
- import_ticket      — ticket imported from markdown
- sync_ticket        — ticket status synced to markdown
"""

import json
import sqlite3
from typing import Any


def log(
    conn: sqlite3.Connection,
    actor: str,
    action: str,
    entity_type: str,
    entity_id: str | int,
    old_state: str | None = None,
    new_state: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """
    Insert an audit log entry.

    This function does NOT commit — the caller must commit as part of the
    enclosing transaction. This ensures audit entries are atomic with the
    state change they record.

    Args:
        conn: Open database connection (must be inside a transaction)
        actor: Who initiated the action (agent ID, "human:name", "scheduler")
        action: What happened (e.g. "claim_phase", "complete_phase")
        entity_type: Type of entity affected ("ticket", "phase", "gate", "agent")
        entity_id: ID of the affected entity
        old_state: Status/state before the action (optional)
        new_state: Status/state after the action (optional)
        details: Additional context as a dict (will be JSON-serialized)
    """
    details_json = json.dumps(details) if details is not None else None
    conn.execute(
        """
        INSERT INTO audit_log (actor, action, entity_type, entity_id,
                               old_state, new_state, details)
        VALUES (:actor, :action, :entity_type, :entity_id,
                :old_state, :new_state, :details)
        """,
        {
            "actor": actor,
            "action": action,
            "entity_type": entity_type,
            "entity_id": str(entity_id),
            "old_state": old_state,
            "new_state": new_state,
            "details": details_json,
        },
    )


def query_audit(
    conn: sqlite3.Connection,
    ticket_id: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    actor: str | None = None,
    action: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """
    Query the audit log with optional filters.

    All filters are combined with AND. Results are ordered newest-first.

    Args:
        conn: Open database connection
        ticket_id: Filter by ticket (matches audit entries for that ticket's
                   phases and gates, plus direct ticket entries)
        entity_type: Filter by entity type ("ticket", "phase", "gate", "agent")
        entity_id: Filter by entity ID
        actor: Filter by actor
        action: Filter by action
        limit: Maximum results (default: 100)

    Returns:
        List of audit entry dicts, newest first.
    """
    conditions: list[str] = []
    params: list[Any] = []

    if ticket_id is not None:
        # Match direct ticket entries OR entries for phases/gates belonging to the ticket
        conditions.append("""
            (
                (entity_type = 'ticket' AND entity_id = ?)
                OR entity_id IN (
                    SELECT CAST(id AS TEXT) FROM phases WHERE ticket_id = ?
                )
                OR entity_id IN (
                    SELECT CAST(id AS TEXT) FROM human_gates WHERE ticket_id = ?
                )
            )
        """)
        params.extend([ticket_id, ticket_id, ticket_id])

    if entity_type is not None:
        conditions.append("entity_type = ?")
        params.append(entity_type)

    if entity_id is not None:
        conditions.append("entity_id = ?")
        params.append(str(entity_id))

    if actor is not None:
        conditions.append("actor = ?")
        params.append(actor)

    if action is not None:
        conditions.append("action = ?")
        params.append(action)

    where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""

    cursor = conn.execute(
        f"""
        SELECT id, timestamp, actor, action, entity_type, entity_id,
               old_state, new_state, details
        FROM audit_log
        {where_clause}
        ORDER BY id DESC
        LIMIT ?
        """,
        params + [limit],
    )
    rows = cursor.fetchall()
    result = []
    for row in rows:
        entry = dict(row)
        if entry.get("details"):
            try:
                entry["details"] = json.loads(entry["details"])
            except (json.JSONDecodeError, TypeError):
                pass
        result.append(entry)
    return result
