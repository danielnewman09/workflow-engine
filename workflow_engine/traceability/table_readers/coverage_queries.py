"""SQL query builders for coverage queries.

Each function returns a (sql, params) tuple, keeping query construction
separate from execution.  This makes queries independently testable and
self-documenting via their docstrings.
"""

from __future__ import annotations


def get_coverage_run_by_id(run_id: int) -> tuple[str, list]:
    """Fetch a single coverage run by id."""
    return "SELECT * FROM coverage_runs WHERE id = ?", [run_id]


def get_latest_coverage_run() -> tuple[str, list]:
    """Fetch the most recent coverage run."""
    return "SELECT * FROM coverage_runs ORDER BY id DESC LIMIT 1", []


def get_latest_coverage_run_id() -> tuple[str, list]:
    """Fetch just the id of the most recent coverage run."""
    return "SELECT id FROM coverage_runs ORDER BY id DESC LIMIT 1", []


def get_top_coverage_files(run_id: int) -> tuple[str, list]:
    """Top 20 files by lines found for a coverage run."""
    sql = """SELECT file_path, lines_found, lines_hit,
                    functions_found, functions_hit,
                    branches_found, branches_hit
             FROM coverage_files
             WHERE run_id = ?
             ORDER BY lines_found DESC
             LIMIT 20"""
    return sql, [run_id]


def get_coverage_files_like(run_id: int, file_path: str) -> tuple[str, list]:
    """Coverage files matching a partial path for a given run."""
    sql = """SELECT file_path, lines_found, lines_hit,
                    functions_found, functions_hit,
                    branches_found, branches_hit
             FROM coverage_files
             WHERE run_id = ? AND file_path LIKE ?"""
    return sql, [run_id, f"%{file_path}%"]


def get_coverage_lines(run_id: int, file_path: str) -> tuple[str, list]:
    """Line-level hit counts for a specific file in a coverage run."""
    sql = """SELECT line_number, hit_count
             FROM coverage_lines
             WHERE run_id = ? AND file_path = ?
             ORDER BY line_number"""
    return sql, [run_id, file_path]


def get_ticket_file_paths(ticket_number: str) -> tuple[str, list]:
    """File paths declared in a ticket."""
    return "SELECT file_path FROM ticket_files WHERE ticket_number = ?", [ticket_number]
