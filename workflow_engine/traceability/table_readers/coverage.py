"""Coverage query logic.

Encapsulates all coverage-related queries: run summaries, per-file detail,
and ticket-level coverage aggregation.  Owned by TraceabilityServer and
delegates SQL construction to coverage_queries.py.
"""

import sqlite3

from workflow_engine.traceability.table_readers.coverage_queries import (
    get_coverage_files_like,
    get_coverage_lines,
    get_coverage_run_by_id,
    get_latest_coverage_run,
    get_latest_coverage_run_id,
    get_ticket_file_paths,
    get_top_coverage_files,
)
from workflow_engine.utils.sqlite import rows_to_dicts


class CoverageQuerier:
    """Queries over the coverage_runs, coverage_files, and coverage_lines tables."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def _resolve_run_id(self, run_id: int | None) -> int | None:
        """Return the given run_id, or the latest run id, or None."""
        if run_id is not None:
            return run_id
        sql, params = get_latest_coverage_run_id()
        row = self.conn.execute(sql, params).fetchone()
        return row["id"] if row else None

    def summary(self, run_id: int | None = None) -> dict:
        """Get coverage summary for a specific run or the latest run.

        Returns a dict with the run metadata plus a ``top_files`` list
        (top 20 files by lines found).

        Returns an ``{"error": ...}`` dict if no coverage runs exist.
        """
        if run_id is not None:
            sql, params = get_coverage_run_by_id(run_id)
        else:
            sql, params = get_latest_coverage_run()

        row = self.conn.execute(sql, params).fetchone()
        if not row:
            return {"error": "No coverage runs found"}

        result = dict(row)

        sql, params = get_top_coverage_files(result["id"])
        result["top_files"] = rows_to_dicts(self.conn.execute(sql, params).fetchall())

        return result

    def file_detail(self, file_path: str, run_id: int | None = None) -> dict:
        """Get per-file coverage detail including line-level data.

        Uses LIKE for partial path matching.  If a single file matches,
        returns its summary plus line-level hit counts.  If multiple files
        match, returns all summaries and line data for the first match.

        Returns an ``{"error": ...}`` dict if no data is found.
        """
        resolved_id = self._resolve_run_id(run_id)
        if resolved_id is None:
            return {"error": "No coverage runs found"}

        sql, params = get_coverage_files_like(resolved_id, file_path)
        files = rows_to_dicts(self.conn.execute(sql, params).fetchall())

        if not files:
            return {"error": f"No coverage data for '{file_path}'"}

        result = files[0] if len(files) == 1 else {"files": files}

        target_path = files[0]["file_path"]
        sql, params = get_coverage_lines(resolved_id, target_path)
        lines = rows_to_dicts(self.conn.execute(sql, params).fetchall())
        if len(files) == 1:
            result["lines"] = lines
        elif lines:
            result["first_file_lines"] = lines

        return result

    def ticket_coverage(self, ticket_number: str) -> dict:
        """Get coverage data for files associated with a ticket.

        Joins ticket file declarations with the latest coverage run
        to produce per-file and aggregate coverage metrics.
        """
        sql, params = get_ticket_file_paths(ticket_number)
        ticket_files = [r["file_path"] for r in self.conn.execute(sql, params).fetchall()]

        if not ticket_files:
            return {
                "ticket": ticket_number,
                "error": "No files declared in ticket",
            }

        resolved_id = self._resolve_run_id(None)
        if resolved_id is None:
            return {
                "ticket": ticket_number,
                "error": "No coverage runs found",
            }

        coverage_data = []
        for tf in ticket_files:
            sql, params = get_coverage_files_like(resolved_id, tf)
            rows = rows_to_dicts(self.conn.execute(sql, params).fetchall())
            coverage_data.extend(rows)

        total_lines = sum(c["lines_found"] for c in coverage_data)
        total_hits = sum(c["lines_hit"] for c in coverage_data)

        return {
            "ticket": ticket_number,
            "run_id": resolved_id,
            "files_declared": len(ticket_files),
            "files_with_coverage": len(coverage_data),
            "total_lines": total_lines,
            "total_hits": total_hits,
            "line_rate": total_hits / total_lines if total_lines > 0 else None,
            "file_details": coverage_data,
        }
