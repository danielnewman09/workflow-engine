#!/usr/bin/env python3
# Ticket: 0083_database_agent_orchestration
# Design: docs/designs/0083_database_agent_orchestration/design.md
"""
Workflow Engine Atomic Claiming Logic

Implements the atomic claim pattern validated by prototype P1:
  BEGIN IMMEDIATE + UPDATE ... WHERE claimed_by IS NULL RETURNING *

This is the central correctness guarantee of the workflow engine: no two
agents can claim the same phase simultaneously, even under concurrent access.

Key prototype findings (P1, 40/40 trials passed):
1. BEGIN IMMEDIATE acquires a reserved lock at transaction start, serializing
   writers without OperationalError.
2. PRAGMA busy_timeout=5000 (set on connection open) enables retry without
   application-level retry loops.
3. WHERE claimed_by IS NULL is required (status alone is insufficient guard).
4. UPDATE ... RETURNING * reflects committed values — correct for all checked criteria.

The claim query orders available phases by:
1. Ticket priority (Critical > High > Medium > Low)
2. Phase ID (stable ordering for ties)

Inter-ticket dependency check is embedded in the query: a ticket with an
unresolved 'completion' dependency is never eligible for claiming.
"""

import sqlite3
from typing import Any

from . import audit
from .models import Phase, PhaseStatus


# ---------------------------------------------------------------------------
# Claim result types
# ---------------------------------------------------------------------------


class ClaimSuccess:
    """Agent successfully claimed a phase."""

    def __init__(self, phase: Phase):
        self.phase = phase
        self.success = True


class ClaimEmpty:
    """No available phases matching the agent's type."""

    def __init__(self, agent_type: str):
        self.agent_type = agent_type
        self.success = False

    def __str__(self) -> str:
        return f"No available phases for agent type '{self.agent_type}'"


class ClaimConflict:
    """The specified phase was already claimed by another agent."""

    def __init__(self, phase_id: int):
        self.phase_id = phase_id
        self.success = False

    def __str__(self) -> str:
        return f"Phase {self.phase_id} was already claimed by another agent"


# ---------------------------------------------------------------------------
# Atomic claim query
# ---------------------------------------------------------------------------

_CLAIM_NEXT_SQL = """
UPDATE phases
SET status      = 'claimed',
    claimed_by  = :agent_id,
    claimed_at  = datetime('now'),
    heartbeat_at = datetime('now')
WHERE id = (
    SELECT p.id
    FROM phases p
    JOIN tickets t ON p.ticket_id = t.id
    WHERE p.status = 'available'
      AND p.claimed_by IS NULL
      AND p.agent_type = :agent_type
      AND NOT EXISTS (
          SELECT 1 FROM dependencies d
          WHERE d.blocked_ticket_id = t.id
            AND d.resolved = 0
            AND d.dependency_type = 'completion'
      )
    ORDER BY
        CASE t.priority
            WHEN 'Critical' THEN 0
            WHEN 'High'     THEN 1
            WHEN 'Medium'   THEN 2
            WHEN 'Low'      THEN 3
            ELSE                 4
        END ASC,
        p.id ASC
    LIMIT 1
)
RETURNING *
"""

_CLAIM_SPECIFIC_SQL = """
UPDATE phases
SET status      = 'claimed',
    claimed_by  = :agent_id,
    claimed_at  = datetime('now'),
    heartbeat_at = datetime('now')
WHERE id = :phase_id
  AND status = 'available'
  AND claimed_by IS NULL
RETURNING *
"""


def claim_next(
    conn: sqlite3.Connection,
    agent_id: str,
    agent_type: str,
) -> ClaimSuccess | ClaimEmpty:
    """
    Atomically claim the next available phase matching agent_type.

    Uses BEGIN IMMEDIATE to serialize concurrent claimers (P1 validated).
    The caller must NOT already be in a transaction.

    Args:
        conn: Open database connection (with busy_timeout=5000 set)
        agent_id: Unique identifier for this agent instance
        agent_type: Type of work this agent can perform

    Returns:
        ClaimSuccess with the claimed Phase, or ClaimEmpty if nothing available.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            _CLAIM_NEXT_SQL,
            {"agent_id": agent_id, "agent_type": agent_type},
        ).fetchone()

        if row is None:
            conn.execute("ROLLBACK")
            return ClaimEmpty(agent_type)

        phase = Phase.from_row(row)

        # Update agent current_phase_id and last_heartbeat
        conn.execute(
            """
            UPDATE agents
            SET status = 'working',
                current_phase_id = :phase_id,
                last_heartbeat = datetime('now')
            WHERE id = :agent_id
            """,
            {"phase_id": phase.id, "agent_id": agent_id},
        )

        audit.log(
            conn,
            actor=agent_id,
            action="claim_phase",
            entity_type="phase",
            entity_id=phase.id,
            old_state=PhaseStatus.AVAILABLE,
            new_state=PhaseStatus.CLAIMED,
            details={"ticket_id": phase.ticket_id, "phase_name": phase.phase_name},
        )

        conn.execute("COMMIT")
        return ClaimSuccess(phase)

    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise


def claim_specific(
    conn: sqlite3.Connection,
    agent_id: str,
    phase_id: int,
) -> ClaimSuccess | ClaimConflict:
    """
    Atomically claim a specific phase by ID.

    Useful when an agent has already listed available work and selected
    a specific phase to claim.

    Args:
        conn: Open database connection (with busy_timeout=5000 set)
        agent_id: Unique identifier for this agent instance
        phase_id: ID of the phase to claim

    Returns:
        ClaimSuccess if claimed, or ClaimConflict if already taken.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            _CLAIM_SPECIFIC_SQL,
            {"agent_id": agent_id, "phase_id": phase_id},
        ).fetchone()

        if row is None:
            conn.execute("ROLLBACK")
            return ClaimConflict(phase_id)

        phase = Phase.from_row(row)

        conn.execute(
            """
            UPDATE agents
            SET status = 'working',
                current_phase_id = :phase_id,
                last_heartbeat = datetime('now')
            WHERE id = :agent_id
            """,
            {"phase_id": phase.id, "agent_id": agent_id},
        )

        audit.log(
            conn,
            actor=agent_id,
            action="claim_phase",
            entity_type="phase",
            entity_id=phase.id,
            old_state=PhaseStatus.AVAILABLE,
            new_state=PhaseStatus.CLAIMED,
            details={"ticket_id": phase.ticket_id, "phase_name": phase.phase_name},
        )

        conn.execute("COMMIT")
        return ClaimSuccess(phase)

    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise


def release_phase(
    conn: sqlite3.Connection,
    agent_id: str,
    phase_id: int,
    actor: str | None = None,
) -> bool:
    """
    Release a claimed phase back to available.

    Used for:
    - Explicit agent release (agent decides not to proceed)
    - Stale recovery (scheduler releases timed-out agent's phase)

    Args:
        conn: Open database connection
        agent_id: Agent that holds the claim (used for validation)
        phase_id: Phase to release
        actor: Override audit actor (default: agent_id, use "scheduler" for stale recovery)

    Returns:
        True if released, False if phase was not claimed by this agent.
    """
    actor = actor or agent_id

    conn.execute("BEGIN IMMEDIATE")
    try:
        # Get current phase state for validation
        row = conn.execute(
            "SELECT id, status, claimed_by FROM phases WHERE id = ?",
            (phase_id,),
        ).fetchone()

        if row is None or row["claimed_by"] != agent_id:
            conn.execute("ROLLBACK")
            return False

        old_status = row["status"]

        conn.execute(
            """
            UPDATE phases
            SET status = 'available',
                claimed_by = NULL,
                claimed_at = NULL,
                heartbeat_at = NULL
            WHERE id = ?
            """,
            (phase_id,),
        )

        conn.execute(
            """
            UPDATE agents
            SET status = 'idle',
                current_phase_id = NULL
            WHERE id = ?
            """,
            (agent_id,),
        )

        audit.log(
            conn,
            actor=actor,
            action="release_phase",
            entity_type="phase",
            entity_id=phase_id,
            old_state=old_status,
            new_state=PhaseStatus.AVAILABLE,
            details={"agent_id": agent_id},
        )

        conn.execute("COMMIT")
        return True

    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise


def list_available(
    conn: sqlite3.Connection,
    agent_type: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """
    List available phases for an agent type, ordered by priority.

    Does NOT claim — read-only query. Used by agents to preview work
    before calling claim_phase.

    Returns:
        List of dicts with phase and ticket info.
    """
    cursor = conn.execute(
        """
        SELECT p.id AS phase_id, p.ticket_id, p.phase_name, p.phase_order,
               p.parallel_group, t.priority, t.full_name AS ticket_full_name
        FROM phases p
        JOIN tickets t ON p.ticket_id = t.id
        WHERE p.status = 'available'
          AND p.agent_type = :agent_type
          AND NOT EXISTS (
              SELECT 1 FROM dependencies d
              WHERE d.blocked_ticket_id = t.id
                AND d.resolved = 0
                AND d.dependency_type = 'completion'
          )
        ORDER BY
            CASE t.priority
                WHEN 'Critical' THEN 0
                WHEN 'High'     THEN 1
                WHEN 'Medium'   THEN 2
                WHEN 'Low'      THEN 3
                ELSE                 4
            END ASC,
            p.id ASC
        LIMIT :limit
        """,
        {"agent_type": agent_type, "limit": limit},
    )
    return [dict(row) for row in cursor.fetchall()]
