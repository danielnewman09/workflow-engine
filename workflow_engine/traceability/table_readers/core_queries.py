"""SQL query builders for core traceability queries.

Covers ticket-impact and commit-context queries that span the snapshots,
file_changes, symbol_changes, and design_decisions tables.

Each function returns a (sql, params) tuple, keeping query construction
separate from execution.
"""

from __future__ import annotations


def get_snapshots_for_ticket(ticket_number: str) -> tuple[str, list]:
    """Snapshots (commits) for a ticket, ordered by date."""
    sql = """SELECT id, sha, date, author, message, prefix, phase
             FROM snapshots
             WHERE ticket_number = ?
             ORDER BY date"""
    return sql, [ticket_number]


def get_file_changes_for_snapshots(snapshot_ids: list[int]) -> tuple[str, list]:
    """Aggregated file changes across multiple snapshots."""
    placeholders = ",".join("?" * len(snapshot_ids))
    sql = f"""SELECT file_path, change_type,
                     SUM(insertions) as total_insertions,
                     SUM(deletions) as total_deletions,
                     COUNT(*) as touch_count
              FROM file_changes
              WHERE snapshot_id IN ({placeholders})
              GROUP BY file_path
              ORDER BY touch_count DESC"""
    return sql, snapshot_ids


def get_symbol_changes_for_snapshots(snapshot_ids: list[int]) -> tuple[str, list]:
    """Symbol changes across multiple snapshots with commit metadata."""
    placeholders = ",".join("?" * len(snapshot_ids))
    sql = f"""SELECT sc.qualified_name, sc.change_type,
                     sc.old_file_path, sc.new_file_path,
                     s.sha, s.date, s.message
              FROM symbol_changes sc
              JOIN snapshots s ON s.id = sc.snapshot_id
              WHERE sc.snapshot_id IN ({placeholders})
              ORDER BY s.date"""
    return sql, snapshot_ids


def get_decisions_for_ticket(ticket_number: str) -> tuple[str, list]:
    """Design decisions linked to a ticket number."""
    sql = """SELECT dd_id, title, rationale, status, extraction_method
             FROM design_decisions
             WHERE ticket = ?"""
    return sql, [ticket_number]


def get_snapshot_by_sha(commit_sha: str) -> tuple[str, list]:
    """Fetch a snapshot row by SHA prefix."""
    return "SELECT * FROM snapshots WHERE sha LIKE ?", [f"{commit_sha}%"]


def get_file_changes_for_snapshot(snapshot_id: int) -> tuple[str, list]:
    """File changes for a single snapshot."""
    sql = """SELECT file_path, change_type, insertions, deletions, old_path
             FROM file_changes WHERE snapshot_id = ?"""
    return sql, [snapshot_id]


def get_symbol_changes_for_snapshot(snapshot_id: int) -> tuple[str, list]:
    """Symbol changes for a single snapshot."""
    sql = """SELECT qualified_name, change_type,
                    old_file_path, new_file_path,
                    old_line, new_line,
                    old_signature, new_signature
             FROM symbol_changes WHERE snapshot_id = ?"""
    return sql, [snapshot_id]


def get_decisions_for_ticket_short(ticket_number: str) -> tuple[str, list]:
    """Compact decision list for commit-context (fewer columns)."""
    sql = """SELECT dd_id, title, rationale, status
             FROM design_decisions WHERE ticket = ?"""
    return sql, [ticket_number]


def get_decisions_for_snapshot(snapshot_id: int) -> tuple[str, list]:
    """Decisions directly linked to a snapshot via decision_commits."""
    sql = """SELECT dd.dd_id, dd.title, dd.rationale, dd.status
             FROM decision_commits dc
             JOIN design_decisions dd ON dd.id = dc.decision_id
             WHERE dc.snapshot_id = ?"""
    return sql, [snapshot_id]
