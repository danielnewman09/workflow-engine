#!/usr/bin/env python3
# Ticket: 0083_database_agent_orchestration
# Design: docs/designs/0083_database_agent_orchestration/design.md
"""
Workflow Engine CLI

Human-facing command-line interface for the work queue.
Provides human review gate management, queue inspection, and maintenance commands.

Usage:
    # All commands auto-detect .workflow/config.yaml from the current directory
    # or accept --db and --project-root overrides.

    workflow-engine gates              # list pending human review gates
    workflow-engine approve <gate-id>  # approve a gate
    workflow-engine reject <gate-id>   # reject a gate

    workflow-engine status <ticket-id> # show ticket status and phases
    workflow-engine list               # list all tickets
    workflow-engine blocked            # show blocked tickets and gates

    workflow-engine agents             # show active agents
    workflow-engine queue <type>       # show work queue for an agent type

    workflow-engine import-tickets     # import tickets from markdown files
    workflow-engine sync-markdown      # sync DB status back to markdown
    workflow-engine metrics            # show phase duration metrics
    workflow-engine audit              # show audit log

    workflow-engine add-dep --blocked 0084 --blocking 0083
    workflow-engine resolve-dep <dep-id>
    workflow-engine cleanup-stale      # release stale agent claims
"""

import argparse
import json
import sys
from pathlib import Path

from workflow_engine.engine.config import load_workflow_config
from workflow_engine.engine.markdown_sync import sync_status_to_file, sync_workflow_log_to_file
from workflow_engine.engine.schema import create_db
from workflow_engine.engine import scheduler as sched_mod


def _find_project_root(start: Path | None = None) -> Path:
    """Walk up from start directory to find .workflow/ directory."""
    current = start or Path.cwd()
    for candidate in [current] + list(current.parents):
        if (candidate / ".workflow").exists():
            return candidate
    return current  # fallback to cwd


def _open_db(args: argparse.Namespace) -> tuple:
    """Open database and load config from args or auto-discovery."""
    project_root = Path(args.project_root) if args.project_root else _find_project_root()
    config = load_workflow_config(project_root)

    db_path = args.db if args.db else config.db_path
    conn = create_db(db_path)
    return conn, config, project_root


# ---------------------------------------------------------------------------
# Gate commands
# ---------------------------------------------------------------------------


def cmd_gates(args: argparse.Namespace) -> int:
    """List pending human review gates."""
    conn, config, _ = _open_db(args)
    try:
        rows = conn.execute(
            """
            SELECT hg.id AS gate_id, hg.ticket_id, hg.gate_type,
                   hg.requested_at, p.phase_name,
                   t.full_name AS ticket_full_name, t.priority
            FROM human_gates hg
            JOIN phases p ON p.id = hg.phase_id
            JOIN tickets t ON t.id = hg.ticket_id
            WHERE hg.status = 'pending'
            ORDER BY hg.requested_at ASC
            """
        ).fetchall()

        if not rows:
            print("No pending human review gates.")
            return 0

        print(f"{'Gate ID':<8} {'Ticket':<8} {'Priority':<10} {'Gate Type':<25} {'Requested':<20} {'Phase'}")
        print("-" * 90)
        for row in rows:
            print(
                f"{row['gate_id']:<8} {row['ticket_id']:<8} {row['priority'] or 'N/A':<10} "
                f"{row['gate_type']:<25} {row['requested_at'][:19]:<20} {row['phase_name']}"
            )

        print(f"\n{len(rows)} pending gate(s). Run: workflow-engine approve <gate-id>")
        return 0
    finally:
        conn.close()


def cmd_approve(args: argparse.Namespace) -> int:
    """Approve a human review gate."""
    conn, config, _ = _open_db(args)
    try:
        result = sched_mod.resolve_gate(
            conn,
            gate_id=args.gate_id,
            decision="approved",
            decided_by=args.by or "cli-user",
            notes=args.notes,
        )
        print(f"Gate {args.gate_id} APPROVED.")
        print(f"  Ticket: {result['ticket_id']}")
        print(f"  Phase {result['phase_id']} is now available for claiming.")
        return 0
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def cmd_reject(args: argparse.Namespace) -> int:
    """Reject a human review gate."""
    conn, config, _ = _open_db(args)
    try:
        decision = "changes_requested" if args.changes else "rejected"
        result = sched_mod.resolve_gate(
            conn,
            gate_id=args.gate_id,
            decision=decision,
            decided_by=args.by or "cli-user",
            notes=args.notes,
        )
        print(f"Gate {args.gate_id} {decision.upper()}.")
        print(f"  Ticket: {result['ticket_id']}")
        return 0
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Query commands
# ---------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    """Show ticket status with all phases."""
    conn, config, _ = _open_db(args)
    try:
        ticket = conn.execute(
            "SELECT * FROM tickets WHERE id = ?", (args.ticket_id,)
        ).fetchone()

        if ticket is None:
            print(f"Ticket '{args.ticket_id}' not found.", file=sys.stderr)
            return 1

        print(f"\nTicket: {ticket['full_name']}")
        print(f"  Priority  : {ticket['priority'] or 'N/A'}")
        print(f"  Languages : {ticket['languages']}")
        print(f"  Status    : {ticket['current_status']}")
        print(f"  GitHub    : {ticket['github_issue'] or 'N/A'}")

        phases = conn.execute(
            """
            SELECT id, phase_name, status, agent_type, claimed_by,
                   started_at, completed_at
            FROM phases WHERE ticket_id = ?
            ORDER BY phase_order ASC, id ASC
            """,
            (args.ticket_id,),
        ).fetchall()

        print(f"\nPhases ({len(phases)}):")
        print(f"  {'ID':<6} {'Phase':<30} {'Status':<12} {'Agent Type':<20} {'Claimed By'}")
        print("  " + "-" * 85)
        for p in phases:
            claimed = p["claimed_by"] or ""
            if len(claimed) > 20:
                claimed = claimed[:17] + "..."
            agent = p["agent_type"] or "(human gate)"
            print(
                f"  {p['id']:<6} {p['phase_name']:<30} {p['status']:<12} "
                f"{agent:<20} {claimed}"
            )

        # Show pending gates
        gates = conn.execute(
            "SELECT id, gate_type, status, requested_at FROM human_gates WHERE ticket_id = ?",
            (args.ticket_id,),
        ).fetchall()
        if gates:
            print(f"\nHuman Gates ({len(gates)}):")
            for g in gates:
                print(f"  Gate {g['id']}: {g['gate_type']} — {g['status']} (requested: {g['requested_at'][:19]})")

        return 0
    finally:
        conn.close()


def cmd_list(args: argparse.Namespace) -> int:
    """List tickets."""
    conn, config, _ = _open_db(args)
    try:
        conditions: list[str] = []
        params: list = []

        if args.status:
            conditions.append("current_status LIKE ?")
            params.append(f"%{args.status}%")
        if args.priority:
            conditions.append("priority = ?")
            params.append(args.priority)

        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        rows = conn.execute(
            f"""
            SELECT id, full_name, priority, current_status, languages
            FROM tickets {where}
            ORDER BY
                CASE priority WHEN 'Critical' THEN 0 WHEN 'High' THEN 1
                              WHEN 'Medium' THEN 2 WHEN 'Low' THEN 3 ELSE 4 END,
                id ASC
            """,
            params,
        ).fetchall()

        if not rows:
            print("No tickets found.")
            return 0

        print(f"\n{'ID':<8} {'Priority':<10} {'Languages':<15} {'Status'}")
        print("-" * 80)
        for row in rows:
            print(
                f"{row['id']:<8} {row['priority'] or 'N/A':<10} "
                f"{row['languages']:<15} {row['current_status']}"
            )
        print(f"\n{len(rows)} ticket(s).")
        return 0
    finally:
        conn.close()


def cmd_blocked(args: argparse.Namespace) -> int:
    """Show blocked tickets and pending gates."""
    conn, config, _ = _open_db(args)
    try:
        gate_blocked = conn.execute(
            """
            SELECT hg.id AS gate_id, hg.ticket_id, hg.gate_type, hg.requested_at,
                   p.phase_name, t.priority
            FROM human_gates hg
            JOIN phases p ON p.id = hg.phase_id
            JOIN tickets t ON t.id = hg.ticket_id
            WHERE hg.status = 'pending'
            ORDER BY hg.requested_at ASC
            """
        ).fetchall()

        dep_blocked = conn.execute(
            """
            SELECT d.id, d.blocked_ticket_id, d.blocking_ticket_id,
                   d.dependency_type, d.created_at
            FROM dependencies d
            WHERE d.resolved = 0
            ORDER BY d.blocked_ticket_id ASC
            """
        ).fetchall()

        if not gate_blocked and not dep_blocked:
            print("No blocked tickets.")
            return 0

        if gate_blocked:
            print(f"\nBlocked on human review gates ({len(gate_blocked)}):")
            for r in gate_blocked:
                print(f"  Gate {r['gate_id']}: ticket {r['ticket_id']} — {r['gate_type']} (since {r['requested_at'][:19]})")

        if dep_blocked:
            print(f"\nBlocked on ticket dependencies ({len(dep_blocked)}):")
            for r in dep_blocked:
                print(f"  Dep {r['id']}: ticket {r['blocked_ticket_id']} blocked by {r['blocking_ticket_id']} ({r['dependency_type']})")

        return 0
    finally:
        conn.close()


def cmd_agents(args: argparse.Namespace) -> int:
    """Show active agents."""
    conn, config, _ = _open_db(args)
    try:
        rows = conn.execute(
            """
            SELECT a.id, a.agent_type, a.status, a.last_heartbeat,
                   p.phase_name AS current_phase, p.ticket_id AS current_ticket
            FROM agents a
            LEFT JOIN phases p ON p.id = a.current_phase_id
            WHERE a.status != 'terminated'
            ORDER BY a.status DESC, a.last_heartbeat DESC
            """
        ).fetchall()

        if not rows:
            print("No registered agents.")
            return 0

        print(f"\n{'Agent ID':<25} {'Type':<20} {'Status':<10} {'Ticket':<8} {'Phase':<25} {'Last Heartbeat'}")
        print("-" * 110)
        for r in rows:
            agent_id = r["id"]
            if len(agent_id) > 22:
                agent_id = agent_id[:19] + "..."
            print(
                f"{agent_id:<25} {r['agent_type']:<20} {r['status']:<10} "
                f"{r['current_ticket'] or '':<8} {r['current_phase'] or '':<25} "
                f"{r['last_heartbeat'][:19] if r['last_heartbeat'] else 'N/A'}"
            )
        return 0
    finally:
        conn.close()


def cmd_queue(args: argparse.Namespace) -> int:
    """Show work queue for an agent type."""
    conn, config, _ = _open_db(args)
    try:
        from workflow_engine.engine.claim import list_available
        rows = list_available(conn, args.agent_type, limit=50)

        if not rows:
            print(f"No available work for agent type '{args.agent_type}'.")
            return 0

        print(f"\nAvailable work for '{args.agent_type}' ({len(rows)} phases):")
        print(f"{'Phase ID':<10} {'Ticket':<8} {'Priority':<10} {'Phase Name'}")
        print("-" * 60)
        for r in rows:
            print(
                f"{r['phase_id']:<10} {r['ticket_id']:<8} "
                f"{r['priority'] or 'N/A':<10} {r['phase_name']}"
            )
        return 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Maintenance commands
# ---------------------------------------------------------------------------


def cmd_import_tickets(args: argparse.Namespace) -> int:
    """Import tickets from markdown files."""
    conn, config, _ = _open_db(args)
    try:
        results = sched_mod.import_all_tickets(conn, config)
        created = sum(1 for r in results if r.get("action") == "created")
        updated = sum(1 for r in results if r.get("action") == "updated")
        errors = sum(1 for r in results if r.get("action") == "error")

        print(f"Imported {len(results)} tickets: {created} created, {updated} updated, {errors} errors.")
        for r in results:
            if r.get("action") == "error":
                print(f"  ERROR: {r.get('file', '?')}: {r.get('error', '?')}", file=sys.stderr)

        # Auto-run scheduler
        for tid_result in results:
            if tid_result.get("ticket_id"):
                sched_mod.seed_phases(conn, tid_result["ticket_id"], config)

        transitions = sched_mod.resolve_availability(conn)
        print(f"Scheduler: {len(transitions)} phase availability update(s).")
        return 0 if errors == 0 else 1
    finally:
        conn.close()


def cmd_sync_markdown(args: argparse.Namespace) -> int:
    """Sync DB status back to markdown files."""
    conn, config, _ = _open_db(args)
    try:
        tickets = conn.execute("SELECT id, markdown_path, current_status FROM tickets").fetchall()
        synced = 0
        for t in tickets:
            if sync_status_to_file(t["markdown_path"], t["current_status"]):
                synced += 1
                print(f"  Updated: {t['markdown_path']}")

        if synced == 0:
            print("All markdown files are already up to date.")
        else:
            print(f"Synced {synced} ticket(s).")
        return 0
    finally:
        conn.close()


def cmd_metrics(args: argparse.Namespace) -> int:
    """Show phase duration metrics."""
    conn, config, _ = _open_db(args)
    try:
        rows = conn.execute(
            """
            SELECT phase_name,
                   COUNT(*) AS count,
                   ROUND(AVG(
                       (julianday(completed_at) - julianday(started_at)) * 24 * 60
                   ), 1) AS avg_min,
                   ROUND(MIN(
                       (julianday(completed_at) - julianday(started_at)) * 24 * 60
                   ), 1) AS min_min,
                   ROUND(MAX(
                       (julianday(completed_at) - julianday(started_at)) * 24 * 60
                   ), 1) AS max_min
            FROM phases
            WHERE status = 'completed'
              AND started_at IS NOT NULL
              AND completed_at IS NOT NULL
            GROUP BY phase_name
            ORDER BY avg_min DESC NULLS LAST
            """
        ).fetchall()

        if not rows:
            print("No completed phases with timing data.")
            return 0

        print(f"\n{'Phase':<30} {'Count':<8} {'Avg (min)':<12} {'Min (min)':<12} {'Max (min)'}")
        print("-" * 75)
        for r in rows:
            print(
                f"{r['phase_name']:<30} {r['count']:<8} "
                f"{r['avg_min'] or 'N/A':<12} {r['min_min'] or 'N/A':<12} {r['max_min'] or 'N/A'}"
            )
        return 0
    finally:
        conn.close()


def cmd_audit(args: argparse.Namespace) -> int:
    """Show audit log."""
    conn, config, _ = _open_db(args)
    try:
        from workflow_engine.engine.audit import query_audit
        entries = query_audit(
            conn,
            ticket_id=args.ticket,
            limit=args.limit or 50,
        )

        if not entries:
            print("No audit entries found.")
            return 0

        print(f"\n{'Timestamp':<22} {'Actor':<20} {'Action':<20} {'Entity':<15} {'Old':<15} {'New'}")
        print("-" * 110)
        for e in entries:
            ts = e["timestamp"][:19] if e["timestamp"] else ""
            actor = e["actor"]
            if len(actor) > 18:
                actor = actor[:15] + "..."
            entity = f"{e['entity_type']}/{e['entity_id']}"
            if len(entity) > 13:
                entity = entity[:10] + "..."
            print(
                f"{ts:<22} {actor:<20} {e['action']:<20} {entity:<15} "
                f"{e.get('old_state') or '':<15} {e.get('new_state') or ''}"
            )
        return 0
    finally:
        conn.close()


def cmd_add_dep(args: argparse.Namespace) -> int:
    """Add inter-ticket dependency."""
    conn, config, _ = _open_db(args)
    try:
        result = sched_mod.add_dependency(
            conn,
            blocked_ticket_id=args.blocked,
            blocking_ticket_id=args.blocking,
            dependency_type=args.type or "completion",
        )
        print(f"Added dependency: ticket {args.blocked} blocked by ticket {args.blocking}.")
        print(f"  Dependency ID: {result['dep_id']}")
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def cmd_resolve_dep(args: argparse.Namespace) -> int:
    """Resolve a dependency manually."""
    conn, config, _ = _open_db(args)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE dependencies SET resolved = 1, resolved_at = datetime('now') WHERE id = ?",
            (args.dep_id,),
        )
        conn.execute("COMMIT")
        print(f"Dependency {args.dep_id} resolved.")
        # Re-run availability
        transitions = sched_mod.resolve_availability(conn)
        if transitions:
            print(f"  {len(transitions)} downstream phase(s) updated.")
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def cmd_cleanup_stale(args: argparse.Namespace) -> int:
    """Release stale agent claims."""
    conn, config, _ = _open_db(args)
    try:
        released = sched_mod.cleanup_stale_agents(
            conn,
            stale_timeout_minutes=config.stale_timeout_minutes,
        )
        if released:
            print(f"Released {len(released)} stale phase(s):")
            for r in released:
                print(f"  Phase {r['phase_id']} ({r['phase_name']}, ticket {r['ticket_id']}) from agent {r['agent_id']}")
        else:
            print("No stale agents found.")
        return 0
    finally:
        conn.close()


def cmd_supervisor(args: argparse.Namespace) -> int:
    """Run the agent supervisor daemon."""
    import asyncio
    import logging

    from workflow_engine.supervisor.supervisor import Supervisor

    log_level = getattr(logging, args.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    _, config, _ = _open_db(args)
    db_path = args.db if args.db else config.db_path

    supervisor = Supervisor(
        db_path=db_path,
        config=config,
        poll_interval=args.poll_interval,
        dry_run=args.dry_run,
    )

    asyncio.run(supervisor.run())
    return 0


# ---------------------------------------------------------------------------
# Main CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="workflow-engine",
        description="Workflow Engine CLI — human review gate management and queue inspection",
    )
    parser.add_argument(
        "--db",
        metavar="PATH",
        help="Path to workflow.db (default: read from .workflow/config.yaml)",
    )
    parser.add_argument(
        "--project-root",
        metavar="PATH",
        help="Path to consuming repo root (default: auto-detect from .workflow/)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # gates
    p_gates = subparsers.add_parser("gates", help="List pending human review gates")
    p_gates.set_defaults(func=cmd_gates)

    # approve
    p_approve = subparsers.add_parser("approve", help="Approve a human review gate")
    p_approve.add_argument("gate_id", type=int, help="Gate ID to approve")
    p_approve.add_argument("--notes", help="Decision notes")
    p_approve.add_argument("--by", help="Reviewer identifier")
    p_approve.set_defaults(func=cmd_approve)

    # reject
    p_reject = subparsers.add_parser("reject", help="Reject a human review gate")
    p_reject.add_argument("gate_id", type=int, help="Gate ID to reject")
    p_reject.add_argument("--notes", help="Decision notes")
    p_reject.add_argument("--by", help="Reviewer identifier")
    p_reject.add_argument("--changes", action="store_true", help="Request changes (vs. full reject)")
    p_reject.set_defaults(func=cmd_reject)

    # status
    p_status = subparsers.add_parser("status", help="Show ticket status")
    p_status.add_argument("ticket_id", help="Ticket ID (e.g. 0083)")
    p_status.set_defaults(func=cmd_status)

    # list
    p_list = subparsers.add_parser("list", help="List tickets")
    p_list.add_argument("--status", help="Filter by status substring")
    p_list.add_argument("--priority", help="Filter by priority (Low/Medium/High/Critical)")
    p_list.set_defaults(func=cmd_list)

    # blocked
    p_blocked = subparsers.add_parser("blocked", help="Show blocked tickets")
    p_blocked.set_defaults(func=cmd_blocked)

    # agents
    p_agents = subparsers.add_parser("agents", help="Show active agents")
    p_agents.set_defaults(func=cmd_agents)

    # queue
    p_queue = subparsers.add_parser("queue", help="Show work queue for agent type")
    p_queue.add_argument("agent_type", help="Agent type (e.g. cpp-implementer)")
    p_queue.set_defaults(func=cmd_queue)

    # import-tickets
    p_import = subparsers.add_parser("import-tickets", help="Import tickets from markdown")
    p_import.set_defaults(func=cmd_import_tickets)

    # sync-markdown
    p_sync = subparsers.add_parser("sync-markdown", help="Sync DB status to markdown files")
    p_sync.set_defaults(func=cmd_sync_markdown)

    # metrics
    p_metrics = subparsers.add_parser("metrics", help="Show phase duration metrics")
    p_metrics.set_defaults(func=cmd_metrics)

    # audit
    p_audit = subparsers.add_parser("audit", help="Show audit log")
    p_audit.add_argument("--ticket", help="Filter by ticket ID")
    p_audit.add_argument("--limit", type=int, default=50, help="Max entries")
    p_audit.set_defaults(func=cmd_audit)

    # add-dep
    p_adddep = subparsers.add_parser("add-dep", help="Add inter-ticket dependency")
    p_adddep.add_argument("--blocked", required=True, help="Blocked ticket ID")
    p_adddep.add_argument("--blocking", required=True, help="Blocking ticket ID")
    p_adddep.add_argument("--type", default="completion", help="Dependency type")
    p_adddep.set_defaults(func=cmd_add_dep)

    # resolve-dep
    p_resdep = subparsers.add_parser("resolve-dep", help="Resolve a dependency manually")
    p_resdep.add_argument("dep_id", type=int, help="Dependency ID")
    p_resdep.set_defaults(func=cmd_resolve_dep)

    # cleanup-stale
    p_cleanup = subparsers.add_parser("cleanup-stale", help="Release stale agent claims")
    p_cleanup.set_defaults(func=cmd_cleanup_stale)

    # supervisor
    p_supervisor = subparsers.add_parser(
        "supervisor", help="Run the agent supervisor daemon"
    )
    p_supervisor.add_argument(
        "--poll-interval",
        type=float,
        default=30,
        help="Seconds between poll cycles (default: 30)",
    )
    p_supervisor.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would spawn without actually spawning agents",
    )
    p_supervisor.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (default: INFO)",
    )
    p_supervisor.set_defaults(func=cmd_supervisor)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
