#!/usr/bin/env python3
# Ticket: 0083_database_agent_orchestration
# Design: docs/designs/0083_database_agent_orchestration/design.md
"""
Workflow Engine Scheduler

The scheduler is invoked on-demand (not a daemon). It performs:

1. Import tickets — reads tickets/*.md, parses metadata, creates/updates DB records
2. Seed phases — for each ticket, creates phase rows per phases.yaml (respecting conditions)
3. Resolve availability — marks phases as 'available' when all prerequisites are met
4. Handle stale agents — releases claimed phases from agents past their heartbeat timeout
5. Monitor completion — when all phases complete, updates ticket markdown status

The scheduler is idempotent — safe to run multiple times. It uses BEGIN IMMEDIATE
for all write transactions per the P1 prototype findings.

Phase availability rules:
- First phase in a ticket: available when ticket is imported
- Subsequent phases: available when the prior sequential phase is 'completed'
- Parallel group phases: available when the 'after' phase is 'completed' AND all
  other group members that are applicable are also seeded
- Human gate phases (agent_type = null): create a human_gates record, mark as blocked
- Conditional phases that don't apply: skipped immediately
"""

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from . import audit
from .models import PhaseDefinition, PhaseStatus, WorkflowConfig
from .state_machine import validate_transition


# ---------------------------------------------------------------------------
# Ticket import
# ---------------------------------------------------------------------------


def import_ticket(
    conn: sqlite3.Connection,
    markdown_path: str | Path,
    config: WorkflowConfig,
) -> dict[str, Any]:
    """
    Parse a ticket markdown file and upsert its record into the database.

    Extracts metadata fields defined in config.phase_definitions' associated
    ticket_metadata spec. Parses the Status section to determine current_status.

    Args:
        conn: Open database connection
        markdown_path: Path to the ticket markdown file
        config: WorkflowConfig with id_regex and phase definitions

    Returns:
        Dict with ticket_id, action ("created" or "updated"), and extracted metadata.
    """
    path = Path(markdown_path)
    if not path.exists():
        raise FileNotFoundError(f"Ticket file not found: {path}")

    content = path.read_text(encoding="utf-8")
    filename = path.name

    # Extract ticket ID from filename
    ticket_id = _extract_ticket_id(filename, config.id_regex)
    if not ticket_id:
        raise ValueError(
            f"Cannot extract ticket ID from '{filename}' using regex '{config.id_regex}'"
        )

    # Derive name and full_name from filename (strip .md)
    stem = path.stem  # e.g. "0083_database_agent_orchestration"
    name = stem[len(ticket_id) + 1:] if stem.startswith(ticket_id) else stem
    full_name = stem

    # Parse metadata section
    metadata = _parse_metadata(content)

    # Parse current status from checkboxes
    current_status = _parse_current_status(content)

    # Map metadata fields to DB columns
    priority = metadata.get("Priority")
    complexity = metadata.get("Estimated Complexity")
    components = metadata.get("Target Component(s)")
    languages_raw = metadata.get("Languages", "C++") or "C++"
    github_issue_raw = metadata.get("GitHub Issue")
    github_issue: int | None = None
    if github_issue_raw:
        try:
            github_issue = int(str(github_issue_raw).strip().lstrip("#"))
        except (ValueError, AttributeError):
            pass

    # Store all metadata as custom_metadata JSON for project-specific fields
    custom_metadata = json.dumps(metadata)

    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT id FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()

        action = "updated" if existing else "created"

        conn.execute(
            """
            INSERT INTO tickets (id, name, full_name, priority, complexity, components,
                                  languages, github_issue, current_status, markdown_path,
                                  custom_metadata, updated_at)
            VALUES (:id, :name, :full_name, :priority, :complexity, :components,
                    :languages, :github_issue, :current_status, :markdown_path,
                    :custom_metadata, datetime('now'))
            ON CONFLICT(id) DO UPDATE SET
                name            = excluded.name,
                full_name       = excluded.full_name,
                priority        = excluded.priority,
                complexity      = excluded.complexity,
                components      = excluded.components,
                languages       = excluded.languages,
                github_issue    = excluded.github_issue,
                current_status  = excluded.current_status,
                markdown_path   = excluded.markdown_path,
                custom_metadata = excluded.custom_metadata,
                updated_at      = datetime('now')
            """,
            {
                "id": ticket_id,
                "name": name,
                "full_name": full_name,
                "priority": priority,
                "complexity": complexity,
                "components": components,
                "languages": languages_raw,
                "github_issue": github_issue,
                "current_status": current_status,
                "markdown_path": str(path),
                "custom_metadata": custom_metadata,
            },
        )

        audit.log(
            conn,
            actor="scheduler",
            action="import_ticket",
            entity_type="ticket",
            entity_id=ticket_id,
            new_state=current_status,
            details={"action": action, "markdown_path": str(path)},
        )

        conn.execute("COMMIT")

    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise

    return {
        "ticket_id": ticket_id,
        "action": action,
        "full_name": full_name,
        "current_status": current_status,
        "priority": priority,
        "languages": languages_raw,
    }


def import_all_tickets(
    conn: sqlite3.Connection,
    config: WorkflowConfig,
) -> list[dict[str, Any]]:
    """
    Import all ticket markdown files from config.tickets_directory.

    Returns:
        List of import result dicts, one per ticket file found.
    """
    tickets_dir = Path(config.tickets_directory)
    if not tickets_dir.exists():
        return []

    results = []
    for ticket_file in sorted(tickets_dir.glob(config.tickets_pattern)):
        try:
            result = import_ticket(conn, ticket_file, config)
            results.append(result)
        except Exception as exc:
            results.append({
                "ticket_id": None,
                "action": "error",
                "error": str(exc),
                "file": str(ticket_file),
            })

    return results


# ---------------------------------------------------------------------------
# Phase seeding
# ---------------------------------------------------------------------------


def seed_phases(
    conn: sqlite3.Connection,
    ticket_id: str,
    config: WorkflowConfig,
) -> list[dict[str, Any]]:
    """
    Create phase rows for a ticket based on phases.yaml definitions.

    Only creates phases that don't already exist (idempotent).
    Evaluates conditions against the ticket's metadata to determine
    which phases apply.

    Args:
        conn: Open database connection
        ticket_id: Ticket ID to seed phases for
        config: WorkflowConfig with phase_definitions

    Returns:
        List of seeded phase dicts (new phases only).
    """
    # Fetch ticket metadata for condition evaluation
    ticket_row = conn.execute(
        "SELECT languages, custom_metadata, github_issue FROM tickets WHERE id = ?",
        (ticket_id,),
    ).fetchone()

    if ticket_row is None:
        raise ValueError(f"Ticket '{ticket_id}' not found in database")

    ticket_meta = _build_ticket_metadata(ticket_row)

    # Get existing phases for this ticket (to avoid duplicates)
    existing_phases = {
        row["phase_name"]
        for row in conn.execute(
            "SELECT phase_name FROM phases WHERE ticket_id = ?", (ticket_id,)
        ).fetchall()
    }

    seeded: list[dict[str, Any]] = []

    conn.execute("BEGIN IMMEDIATE")
    try:
        for phase_def in config.phase_definitions:
            if phase_def.name in existing_phases:
                continue  # Already seeded

            applicable = phase_def.is_applicable(ticket_meta)
            initial_status = PhaseStatus.PENDING if applicable else PhaseStatus.SKIPPED

            conn.execute(
                """
                INSERT INTO phases (ticket_id, phase_name, phase_order, status,
                                     agent_type, parallel_group)
                VALUES (:ticket_id, :phase_name, :phase_order, :status,
                        :agent_type, :parallel_group)
                """,
                {
                    "ticket_id": ticket_id,
                    "phase_name": phase_def.name,
                    "phase_order": phase_def.order,
                    "status": initial_status,
                    "agent_type": phase_def.agent_type,
                    "parallel_group": phase_def.parallel_group,
                },
            )

            audit.log(
                conn,
                actor="scheduler",
                action="seed_phases",
                entity_type="phase",
                entity_id=f"{ticket_id}/{phase_def.name}",
                new_state=initial_status,
                details={
                    "phase_order": phase_def.order,
                    "agent_type": phase_def.agent_type,
                    "applicable": applicable,
                },
            )

            seeded.append({
                "phase_name": phase_def.name,
                "status": initial_status,
                "agent_type": phase_def.agent_type,
            })

        conn.execute("COMMIT")

    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise

    return seeded


# ---------------------------------------------------------------------------
# Availability resolution
# ---------------------------------------------------------------------------


def resolve_availability(
    conn: sqlite3.Connection,
    ticket_id: str | None = None,
) -> list[dict[str, Any]]:
    """
    Resolve which phases should become 'available' and update their status.

    Rules:
    1. First phase (phase_order=0, or lowest non-skipped): available immediately
    2. Sequential phases: available when prior phase is 'completed'
    3. Parallel group phases: available when the phase with order < group
       is 'completed' AND no blocking dependency exists
    4. Phases with agent_type=None (human gates): create a human_gates record,
       mark phase as 'blocked'; gate approval will make next phase available

    Args:
        conn: Open database connection
        ticket_id: If provided, only resolve for this ticket. Otherwise resolve all.

    Returns:
        List of phase transitions applied.
    """
    if ticket_id:
        ticket_ids = [ticket_id]
    else:
        ticket_ids = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM tickets ORDER BY id"
            ).fetchall()
        ]

    transitions = []

    for tid in ticket_ids:
        ticket_transitions = _resolve_ticket_availability(conn, tid)
        transitions.extend(ticket_transitions)

    return transitions


def _resolve_ticket_availability(
    conn: sqlite3.Connection,
    ticket_id: str,
) -> list[dict[str, Any]]:
    """Resolve availability for a single ticket. Returns applied transitions."""
    phases = conn.execute(
        """
        SELECT id, phase_name, phase_order, status, agent_type, parallel_group
        FROM phases
        WHERE ticket_id = ?
        ORDER BY phase_order ASC, id ASC
        """,
        (ticket_id,),
    ).fetchall()

    if not phases:
        return []

    # Check for unresolved ticket dependencies
    has_blocking_dep = conn.execute(
        """
        SELECT 1 FROM dependencies
        WHERE blocked_ticket_id = ? AND resolved = 0 AND dependency_type = 'completion'
        LIMIT 1
        """,
        (ticket_id,),
    ).fetchone() is not None

    transitions = []
    phases_by_order: dict[int, list] = {}
    for p in phases:
        order = p["phase_order"]
        phases_by_order.setdefault(order, []).append(p)

    # Find the minimum order among non-skipped phases
    active_orders = sorted(
        order for order, plist in phases_by_order.items()
        if any(p["status"] != PhaseStatus.SKIPPED for p in plist)
    )
    if not active_orders:
        return []

    for i, order in enumerate(active_orders):
        plist = phases_by_order[order]
        active_phases = [p for p in plist if p["status"] != PhaseStatus.SKIPPED]

        if not active_phases:
            continue

        # Determine if this order level is "unlocked"
        if i == 0:
            # First active order — always unlocked (unless blocked by ticket dependency)
            prior_completed = not has_blocking_dep
        else:
            # Unlocked when all phases at previous active order are completed
            prior_order = active_orders[i - 1]
            prior_phases = phases_by_order[prior_order]
            prior_active = [p for p in prior_phases if p["status"] != PhaseStatus.SKIPPED]
            prior_completed = all(
                p["status"] == PhaseStatus.COMPLETED for p in prior_active
            ) and not has_blocking_dep

        if not prior_completed:
            # Check if any phase at this level should be blocked (not already blocked)
            for p in active_phases:
                if p["status"] == PhaseStatus.PENDING:
                    t = _apply_transition(conn, p, ticket_id, PhaseStatus.BLOCKED)
                    if t:
                        transitions.append(t)
            continue

        # Prior order completed — make phases at this order available
        for p in active_phases:
            if p["status"] == PhaseStatus.PENDING or p["status"] == PhaseStatus.BLOCKED:
                if p["agent_type"] is None:
                    # Human gate: create gate record, mark phase as blocked
                    t = _handle_human_gate_phase(conn, p, ticket_id)
                    if t:
                        transitions.append(t)
                else:
                    t = _apply_transition(conn, p, ticket_id, PhaseStatus.AVAILABLE)
                    if t:
                        transitions.append(t)

        # Check if any phase at this level is now in a terminal state — if so,
        # blocking a gate's downstream phases might need to be checked
        # (gate approval path: see release_gate below)

    return transitions


def _apply_transition(
    conn: sqlite3.Connection,
    phase_row: Any,
    ticket_id: str,
    new_status: str,
) -> dict[str, Any] | None:
    """Apply a status transition to a phase. Returns transition dict or None if no-op."""
    old_status = phase_row["status"]
    if old_status == new_status:
        return None

    try:
        validate_transition(old_status, new_status, phase_row["id"])
    except Exception:
        return None

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE phases SET status = ? WHERE id = ?",
            (new_status, phase_row["id"]),
        )
        audit.log(
            conn,
            actor="scheduler",
            action="resolve_availability",
            entity_type="phase",
            entity_id=phase_row["id"],
            old_state=old_status,
            new_state=new_status,
            details={"ticket_id": ticket_id, "phase_name": phase_row["phase_name"]},
        )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise

    return {
        "phase_id": phase_row["id"],
        "phase_name": phase_row["phase_name"],
        "ticket_id": ticket_id,
        "old_status": old_status,
        "new_status": new_status,
    }


def _handle_human_gate_phase(
    conn: sqlite3.Connection,
    phase_row: Any,
    ticket_id: str,
) -> dict[str, Any] | None:
    """
    Create a human_gates record for a phase with agent_type=None.
    Mark the phase as 'blocked' pending human approval.
    """
    old_status = phase_row["status"]

    # Check if gate already exists
    existing_gate = conn.execute(
        "SELECT id, status FROM human_gates WHERE phase_id = ?",
        (phase_row["id"],),
    ).fetchone()

    if existing_gate:
        # Gate already exists — check if it was approved
        if existing_gate["status"] == "approved":
            # Gate approved: make phase available
            return _apply_transition(conn, phase_row, ticket_id, PhaseStatus.AVAILABLE)
        # Gate pending/rejected: keep blocked
        return None

    # Create new gate
    gate_type = f"{phase_row['phase_name'].lower().replace(' ', '_')}_review"

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            INSERT INTO human_gates (phase_id, ticket_id, gate_type, status)
            VALUES (:phase_id, :ticket_id, :gate_type, 'pending')
            """,
            {
                "phase_id": phase_row["id"],
                "ticket_id": ticket_id,
                "gate_type": gate_type,
            },
        )

        conn.execute(
            "UPDATE phases SET status = 'blocked' WHERE id = ?",
            (phase_row["id"],),
        )

        audit.log(
            conn,
            actor="scheduler",
            action="create_gate",
            entity_type="gate",
            entity_id=f"{ticket_id}/{phase_row['phase_name']}",
            old_state=old_status,
            new_state="blocked",
            details={
                "ticket_id": ticket_id,
                "phase_name": phase_row["phase_name"],
                "gate_type": gate_type,
            },
        )

        conn.execute("COMMIT")

    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise

    return {
        "phase_id": phase_row["id"],
        "phase_name": phase_row["phase_name"],
        "ticket_id": ticket_id,
        "old_status": old_status,
        "new_status": "blocked",
        "gate_created": True,
        "gate_type": gate_type,
    }


# ---------------------------------------------------------------------------
# Stale agent cleanup
# ---------------------------------------------------------------------------


def cleanup_stale_agents(
    conn: sqlite3.Connection,
    stale_timeout_minutes: int = 30,
) -> list[dict[str, Any]]:
    """
    Find agents past their heartbeat timeout and release their claimed phases.

    Per design: stale_timeout_minutes default is 30 minutes (not 10 as originally
    proposed) because agents may have long reasoning passes without MCP tool calls.

    Returns:
        List of released phase dicts.
    """
    stale_agents = conn.execute(
        """
        SELECT id, agent_type, current_phase_id, last_heartbeat
        FROM agents
        WHERE status = 'working'
          AND last_heartbeat < datetime('now', :delta)
        """,
        {"delta": f"-{stale_timeout_minutes} minutes"},
    ).fetchall()

    released = []

    for agent in stale_agents:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Mark agent as stale
            conn.execute(
                "UPDATE agents SET status = 'stale', current_phase_id = NULL WHERE id = ?",
                (agent["id"],),
            )

            # Release claimed phase(s) for this agent
            claimed_phases = conn.execute(
                """
                SELECT id, status, phase_name, ticket_id FROM phases
                WHERE claimed_by = ?
                  AND status IN ('claimed', 'running')
                """,
                (agent["id"],),
            ).fetchall()

            for phase in claimed_phases:
                conn.execute(
                    """
                    UPDATE phases
                    SET status = 'available',
                        claimed_by = NULL,
                        claimed_at = NULL,
                        heartbeat_at = NULL
                    WHERE id = ?
                    """,
                    (phase["id"],),
                )

                # Release file locks held by this phase
                conn.execute(
                    """
                    UPDATE file_locks
                    SET released_at = datetime('now')
                    WHERE phase_id = ? AND released_at IS NULL
                    """,
                    (phase["id"],),
                )

                audit.log(
                    conn,
                    actor="scheduler",
                    action="stale_agent",
                    entity_type="phase",
                    entity_id=phase["id"],
                    old_state=phase["status"],
                    new_state=PhaseStatus.AVAILABLE,
                    details={
                        "agent_id": agent["id"],
                        "agent_type": agent["agent_type"],
                        "last_heartbeat": agent["last_heartbeat"],
                    },
                )

                released.append({
                    "phase_id": phase["id"],
                    "phase_name": phase["phase_name"],
                    "ticket_id": phase["ticket_id"],
                    "agent_id": agent["id"],
                })

            conn.execute("COMMIT")

        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise

    return released


# ---------------------------------------------------------------------------
# Gate management
# ---------------------------------------------------------------------------


def resolve_gate(
    conn: sqlite3.Connection,
    gate_id: int,
    decision: str,
    decided_by: str,
    notes: str | None = None,
) -> dict[str, Any]:
    """
    Resolve a human gate with approve/reject/changes_requested.

    On approval: mark gate as approved, make next phase available.
    On reject/changes_requested: mark gate, keep next phase blocked.

    Args:
        conn: Open database connection
        gate_id: ID of the human_gates record
        decision: "approved", "rejected", or "changes_requested"
        decided_by: Human reviewer identifier
        notes: Optional decision notes

    Returns:
        Dict with gate_id, decision, and affected phase info.
    """
    valid_decisions = {"approved", "rejected", "changes_requested"}
    if decision not in valid_decisions:
        raise ValueError(f"Invalid gate decision '{decision}'. Must be one of: {valid_decisions}")

    gate = conn.execute(
        "SELECT * FROM human_gates WHERE id = ?", (gate_id,)
    ).fetchone()

    if gate is None:
        raise ValueError(f"Gate {gate_id} not found")

    if gate["status"] != "pending":
        raise ValueError(
            f"Gate {gate_id} is already '{gate['status']}' — cannot re-decide"
        )

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            UPDATE human_gates
            SET status = :decision,
                decided_at = datetime('now'),
                decided_by = :decided_by,
                decision_notes = :notes
            WHERE id = :gate_id
            """,
            {
                "decision": decision,
                "decided_by": decided_by,
                "notes": notes,
                "gate_id": gate_id,
            },
        )

        audit.log(
            conn,
            actor=f"human:{decided_by}",
            action=f"{decision}_gate" if decision != "changes_requested" else "request_changes",
            entity_type="gate",
            entity_id=gate_id,
            old_state="pending",
            new_state=decision,
            details={
                "ticket_id": gate["ticket_id"],
                "notes": notes,
            },
        )

        # On approval: make the associated phase available
        if decision == "approved":
            conn.execute(
                """
                UPDATE phases
                SET status = 'available'
                WHERE id = ? AND status = 'blocked'
                """,
                (gate["phase_id"],),
            )

        conn.execute("COMMIT")

    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise

    return {
        "gate_id": gate_id,
        "decision": decision,
        "ticket_id": gate["ticket_id"],
        "phase_id": gate["phase_id"],
    }


# ---------------------------------------------------------------------------
# Phase completion
# ---------------------------------------------------------------------------


def complete_phase(
    conn: sqlite3.Connection,
    agent_id: str,
    phase_id: int,
    result_summary: str | None = None,
    artifacts: list[str] | None = None,
) -> dict[str, Any]:
    """
    Mark a phase as completed and release file locks.

    After completion, resolve_availability should be called to update
    downstream phases.
    """
    phase = conn.execute(
        "SELECT * FROM phases WHERE id = ?", (phase_id,)
    ).fetchone()

    if phase is None:
        raise ValueError(f"Phase {phase_id} not found")

    if phase["claimed_by"] != agent_id:
        raise ValueError(
            f"Phase {phase_id} is claimed by '{phase['claimed_by']}', not '{agent_id}'"
        )

    artifacts_json = json.dumps(artifacts) if artifacts else None

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            UPDATE phases
            SET status = 'completed',
                completed_at = datetime('now'),
                result_summary = :result_summary,
                artifacts = :artifacts
            WHERE id = :phase_id
            """,
            {
                "result_summary": result_summary,
                "artifacts": artifacts_json,
                "phase_id": phase_id,
            },
        )

        # Release file locks
        conn.execute(
            """
            UPDATE file_locks
            SET released_at = datetime('now')
            WHERE phase_id = ? AND released_at IS NULL
            """,
            (phase_id,),
        )

        # Update agent status
        conn.execute(
            """
            UPDATE agents
            SET status = 'idle',
                current_phase_id = NULL,
                last_heartbeat = datetime('now')
            WHERE id = ?
            """,
            (agent_id,),
        )

        audit.log(
            conn,
            actor=agent_id,
            action="complete_phase",
            entity_type="phase",
            entity_id=phase_id,
            old_state=phase["status"],
            new_state=PhaseStatus.COMPLETED,
            details={
                "ticket_id": phase["ticket_id"],
                "phase_name": phase["phase_name"],
                "result_summary": result_summary,
                "artifacts": artifacts,
            },
        )

        conn.execute("COMMIT")

    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise

    return {
        "phase_id": phase_id,
        "ticket_id": phase["ticket_id"],
        "phase_name": phase["phase_name"],
        "status": PhaseStatus.COMPLETED,
    }


def fail_phase(
    conn: sqlite3.Connection,
    agent_id: str,
    phase_id: int,
    error_details: str | None = None,
) -> dict[str, Any]:
    """Mark a phase as failed and release file locks."""
    phase = conn.execute(
        "SELECT * FROM phases WHERE id = ?", (phase_id,)
    ).fetchone()

    if phase is None:
        raise ValueError(f"Phase {phase_id} not found")

    if phase["claimed_by"] != agent_id:
        raise ValueError(
            f"Phase {phase_id} is claimed by '{phase['claimed_by']}', not '{agent_id}'"
        )

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            UPDATE phases
            SET status = 'failed',
                completed_at = datetime('now'),
                error_details = :error_details
            WHERE id = :phase_id
            """,
            {"error_details": error_details, "phase_id": phase_id},
        )

        conn.execute(
            """
            UPDATE file_locks SET released_at = datetime('now')
            WHERE phase_id = ? AND released_at IS NULL
            """,
            (phase_id,),
        )

        conn.execute(
            """
            UPDATE agents SET status = 'idle', current_phase_id = NULL
            WHERE id = ?
            """,
            (agent_id,),
        )

        audit.log(
            conn,
            actor=agent_id,
            action="fail_phase",
            entity_type="phase",
            entity_id=phase_id,
            old_state=phase["status"],
            new_state=PhaseStatus.FAILED,
            details={
                "ticket_id": phase["ticket_id"],
                "error_details": error_details,
            },
        )

        conn.execute("COMMIT")

    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise

    return {
        "phase_id": phase_id,
        "ticket_id": phase["ticket_id"],
        "phase_name": phase["phase_name"],
        "status": PhaseStatus.FAILED,
    }


# ---------------------------------------------------------------------------
# Agent management
# ---------------------------------------------------------------------------


def register_agent(
    conn: sqlite3.Connection,
    agent_id: str,
    agent_type: str,
    metadata: dict | None = None,
) -> dict[str, Any]:
    """Register or refresh an agent instance."""
    metadata_json = json.dumps(metadata) if metadata else None

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            INSERT INTO agents (id, agent_type, status, metadata)
            VALUES (:id, :agent_type, 'idle', :metadata)
            ON CONFLICT(id) DO UPDATE SET
                agent_type = excluded.agent_type,
                status = 'idle',
                last_heartbeat = datetime('now'),
                metadata = excluded.metadata
            """,
            {"id": agent_id, "agent_type": agent_type, "metadata": metadata_json},
        )

        audit.log(
            conn,
            actor=agent_id,
            action="register_agent",
            entity_type="agent",
            entity_id=agent_id,
            new_state="idle",
            details={"agent_type": agent_type},
        )

        conn.execute("COMMIT")

    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise

    return {"agent_id": agent_id, "agent_type": agent_type, "status": "idle"}


def heartbeat(conn: sqlite3.Connection, agent_id: str) -> bool:
    """Update agent last_heartbeat. Returns True if agent exists."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = conn.execute(
            """
            UPDATE agents SET last_heartbeat = datetime('now')
            WHERE id = ? AND status NOT IN ('stale', 'terminated')
            """,
            (agent_id,),
        )
        conn.execute("COMMIT")
        return result.rowcount > 0
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise


def start_phase(
    conn: sqlite3.Connection,
    agent_id: str,
    phase_id: int,
) -> dict[str, Any]:
    """Mark a claimed phase as running."""
    phase = conn.execute(
        "SELECT * FROM phases WHERE id = ?", (phase_id,)
    ).fetchone()

    if phase is None:
        raise ValueError(f"Phase {phase_id} not found")

    if phase["claimed_by"] != agent_id:
        raise ValueError(
            f"Phase {phase_id} is claimed by '{phase['claimed_by']}', not '{agent_id}'"
        )

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE phases SET status = 'running', started_at = datetime('now') WHERE id = ?",
            (phase_id,),
        )

        conn.execute(
            "UPDATE agents SET last_heartbeat = datetime('now') WHERE id = ?",
            (agent_id,),
        )

        audit.log(
            conn,
            actor=agent_id,
            action="start_phase",
            entity_type="phase",
            entity_id=phase_id,
            old_state=PhaseStatus.CLAIMED,
            new_state=PhaseStatus.RUNNING,
            details={"ticket_id": phase["ticket_id"], "phase_name": phase["phase_name"]},
        )

        conn.execute("COMMIT")

    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise

    return {
        "phase_id": phase_id,
        "ticket_id": phase["ticket_id"],
        "phase_name": phase["phase_name"],
        "status": PhaseStatus.RUNNING,
    }


# ---------------------------------------------------------------------------
# File lock management
# ---------------------------------------------------------------------------


def declare_files(
    conn: sqlite3.Connection,
    agent_id: str,
    phase_id: int,
    file_paths: list[str],
) -> list[dict[str, Any]]:
    """
    Declare files this phase will modify (for conflict detection).

    Acquires file locks in the database. Does not block — advisory only.
    """
    acquired = []

    conn.execute("BEGIN IMMEDIATE")
    try:
        for file_path in file_paths:
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO file_locks (file_path, phase_id, agent_id)
                    VALUES (?, ?, ?)
                    """,
                    (file_path, phase_id, agent_id),
                )
                acquired.append({"file_path": file_path, "acquired": True})
            except Exception as exc:
                acquired.append({"file_path": file_path, "acquired": False, "error": str(exc)})

        conn.execute("COMMIT")

    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise

    return acquired


def check_conflicts(
    conn: sqlite3.Connection,
    file_paths: list[str],
    exclude_phase_id: int | None = None,
) -> list[dict[str, Any]]:
    """
    Check if any active phase holds locks on the given files.

    Returns a list of conflicts (file_path, conflicting phase_id, agent_id).
    Empty list means no conflicts.
    """
    conflicts = []

    for file_path in file_paths:
        if exclude_phase_id is not None:
            rows = conn.execute(
                """
                SELECT fl.file_path, fl.phase_id, fl.agent_id,
                       p.phase_name, p.ticket_id
                FROM file_locks fl
                JOIN phases p ON p.id = fl.phase_id
                WHERE fl.file_path = ?
                  AND fl.released_at IS NULL
                  AND fl.phase_id != ?
                """,
                (file_path, exclude_phase_id),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT fl.file_path, fl.phase_id, fl.agent_id,
                       p.phase_name, p.ticket_id
                FROM file_locks fl
                JOIN phases p ON p.id = fl.phase_id
                WHERE fl.file_path = ? AND fl.released_at IS NULL
                """,
                (file_path,),
            ).fetchall()

        for row in rows:
            conflicts.append(dict(row))

    return conflicts


def add_dependency(
    conn: sqlite3.Connection,
    blocked_ticket_id: str,
    blocking_ticket_id: str,
    dependency_type: str = "completion",
) -> dict[str, Any]:
    """Add an inter-ticket dependency."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO dependencies
                (blocked_ticket_id, blocking_ticket_id, dependency_type)
            VALUES (?, ?, ?)
            """,
            (blocked_ticket_id, blocking_ticket_id, dependency_type),
        )

        row = conn.execute(
            """
            SELECT id FROM dependencies
            WHERE blocked_ticket_id = ? AND blocking_ticket_id = ?
            """,
            (blocked_ticket_id, blocking_ticket_id),
        ).fetchone()

        audit.log(
            conn,
            actor="scheduler",
            action="add_dependency",
            entity_type="ticket",
            entity_id=blocked_ticket_id,
            details={
                "blocking_ticket_id": blocking_ticket_id,
                "dependency_type": dependency_type,
            },
        )

        conn.execute("COMMIT")

    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise

    return {
        "dep_id": row["id"] if row else None,
        "blocked_ticket_id": blocked_ticket_id,
        "blocking_ticket_id": blocking_ticket_id,
        "dependency_type": dependency_type,
    }


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _extract_ticket_id(filename: str, id_regex: str) -> str | None:
    """Extract ticket ID from filename using the configured regex."""
    match = re.match(id_regex, filename)
    if match:
        return match.group(1)
    return None


def _parse_metadata(content: str) -> dict[str, Any]:
    """
    Parse the ## Metadata section of a ticket markdown file.

    Extracts key-value pairs from lines like:
      - **Priority**: High
      - **Languages**: Python, C++
      - **GitHub Issue**: 83
    """
    metadata: dict[str, Any] = {}

    in_metadata = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped == "## Metadata":
            in_metadata = True
            continue
        if in_metadata and stripped.startswith("## "):
            break
        if in_metadata and stripped.startswith("- **"):
            # Parse "- **Key**: Value"
            match = re.match(r"-\s+\*\*(.+?)\*\*:\s*(.*)", stripped)
            if match:
                key = match.group(1).strip()
                value = match.group(2).strip()
                metadata[key] = value if value else None

    return metadata


def _parse_current_status(content: str) -> str:
    """
    Parse the ## Status section to find the last checked checkbox.

    Returns the label of the last [x] item found.
    """
    last_checked = "Draft"
    in_status = False

    for line in content.splitlines():
        stripped = line.strip()
        if stripped == "## Status":
            in_status = True
            continue
        if in_status and stripped.startswith("## "):
            break
        if in_status:
            # Match checked: "- [x] Status Label" (case-insensitive x)
            match = re.match(r"-\s+\[[xX]\]\s+(.+)", stripped)
            if match:
                last_checked = match.group(1).strip()

    return last_checked


def _build_ticket_metadata(ticket_row: Any) -> dict[str, Any]:
    """
    Build a metadata dict from a ticket DB row for condition evaluation.

    Parses languages into a list, and merges custom_metadata JSON.
    """
    meta: dict[str, Any] = {}

    # Parse languages as list
    languages_raw = ticket_row["languages"] or "C++"
    meta["languages"] = [lang.strip() for lang in languages_raw.split(",") if lang.strip()]

    # Parse custom_metadata JSON
    if ticket_row["custom_metadata"]:
        try:
            custom = json.loads(ticket_row["custom_metadata"])
            # Map common markdown keys to canonical field names
            field_map = {
                "Requires Math Design": "requires_math_design",
                "Generate Tutorial": "generate_tutorial",
                "Priority": "priority",
                "Estimated Complexity": "complexity",
                "Target Component(s)": "components",
                "Languages": "languages_raw",
                "GitHub Issue": "github_issue",
            }
            for md_key, field_name in field_map.items():
                if md_key in custom:
                    val = custom[md_key]
                    # Normalize boolean strings
                    if isinstance(val, str) and val.lower() in ("yes", "true"):
                        val = True
                    elif isinstance(val, str) and val.lower() in ("no", "false", ""):
                        val = False
                    meta[field_name] = val
        except (json.JSONDecodeError, TypeError):
            pass

    return meta
