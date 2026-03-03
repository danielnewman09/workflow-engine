"""Symbol query logic.

Encapsulates queries over symbol_snapshots and symbol_changes:
timeline of changes to a symbol, and point-in-time symbol listings.
Owned by TraceabilityServer and delegates SQL construction to
symbol_queries.py.
"""

import sqlite3

from workflow_engine.traceability.table_readers.symbol_queries import (
    snapshot_symbols,
    symbol_history,
)
from workflow_engine.utils.sqlite import rows_to_dicts


class SymbolQuerier:
    """Queries over the symbol_snapshots and symbol_changes tables."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def history(self, qualified_name: str) -> list[dict]:
        """Timeline of changes to a symbol across commits.

        Joins symbol_changes with snapshots to produce a chronological
        list of every addition, removal, move, or signature change for
        the given symbol.

        Args:
            qualified_name: Symbol name (supports LIKE wildcards and substring match).

        Returns:
            List of change dicts ordered by commit date, each containing
            change_type, old/new file paths, line numbers, signatures,
            and the associated commit metadata (sha, date, message, etc.).
        """
        sql, params = symbol_history(qualified_name)
        cursor = self.conn.execute(sql, params)
        return rows_to_dicts(cursor.fetchall())

    def snapshot(
        self, commit_sha: str, file_path: str | None = None
    ) -> list[dict]:
        """All symbols at a specific point in time.

        Looks up the snapshot by commit SHA prefix, then returns every
        symbol recorded at that snapshot.  Optionally filters by file path.

        Args:
            commit_sha: Full or prefix SHA of the target commit.
            file_path: Optional file path substring to filter results.

        Returns:
            List of symbol dicts (qualified_name, kind, file_path,
            line_number, signature, class_scope), or a single-element
            list with an error dict if the commit is not found.
        """
        cursor = self.conn.execute(
            "SELECT id FROM snapshots WHERE sha LIKE ?",
            (f"{commit_sha}%",),
        )
        row = cursor.fetchone()
        if not row:
            return [{"error": f"Commit '{commit_sha}' not found"}]

        sql, params = snapshot_symbols(row["id"], file_path=file_path)
        cursor = self.conn.execute(sql, params)
        return rows_to_dicts(cursor.fetchall())
