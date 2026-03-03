"""Design decision query logic.

Encapsulates all decision-related queries: search, detail retrieval,
and symbol-to-decision tracing (why_symbol).  Owned by TraceabilityServer
and delegates SQL construction to decision_queries.py.
"""

import sqlite3

from workflow_engine.traceability.table_readers.decision_queries import (
    search_decisions_fts,
    search_decisions_like,
)
from workflow_engine.utils.sqlite import rows_to_dicts
from workflow_engine.utils.string_utils import split_pascal_case


class DecisionQuerier:
    """Queries over the design_decisions family of tables."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def search(
        self, query: str, ticket: str | None = None, status: str | None = None
    ) -> list[dict]:
        """Search design decisions by keyword, with FTS5 ranked search
        and automatic LIKE fallback.

        Attempts an FTS5 MATCH query first (bm25-ranked).  If the FTS
        virtual table does not exist or yields no results, falls back to
        a LIKE search that splits PascalCase tokens and requires every
        word to appear in at least one text field.

        Args:
            query: Free-text search string (supports PascalCase splitting).
            ticket: Optional ticket number to restrict results.
            status: Optional decision status filter (e.g. "active").

        Returns:
            List of matching decision dicts, ordered by relevance (max 20).
        """
        results = []

        try:
            sql, params = search_decisions_fts(query, ticket=ticket, status=status)
            cursor = self.conn.execute(sql, params)
            results = rows_to_dicts(cursor.fetchall())
        except sqlite3.OperationalError:
            pass

        if results:
            return results

        words = split_pascal_case(query)
        if not words:
            return []

        sql, params = search_decisions_like(words, ticket=ticket, status=status)
        cursor = self.conn.execute(sql, params)
        return rows_to_dicts(cursor.fetchall())

    def get(self, dd_id: str) -> dict:
        """Get full details of a design decision with linked symbols and commits.

        Returns a dict with the decision fields plus:
            symbols: list of linked symbol names.
            commits: list of implementing commit dicts (sha, date, message, etc.).

        Returns an ``{"error": ...}`` dict if the decision is not found.
        """
        cursor = self.conn.execute(
            "SELECT * FROM design_decisions WHERE dd_id = ?", (dd_id,)
        )
        row = cursor.fetchone()
        if not row:
            return {"error": f"Decision '{dd_id}' not found"}

        result = dict(row)

        cursor = self.conn.execute(
            "SELECT symbol_name FROM decision_symbols WHERE decision_id = ?",
            (result["id"],),
        )
        result["symbols"] = [r["symbol_name"] for r in cursor.fetchall()]

        cursor = self.conn.execute(
            """SELECT s.sha, s.date, s.message, s.prefix, s.phase
               FROM decision_commits dc
               JOIN snapshots s ON s.id = dc.snapshot_id
               WHERE dc.decision_id = ?
               ORDER BY s.date""",
            (result["id"],),
        )
        result["commits"] = rows_to_dicts(cursor.fetchall())

        result.pop("id", None)
        return result

    def why_symbol(self, qualified_name: str) -> dict:
        """Design decision(s) that created/modified a symbol, with rationale.

        Finds decisions linked directly to the symbol via decision_symbols,
        then augments with indirect decisions discovered through the symbol's
        commit history (via ticket numbers).

        Args:
            qualified_name: Symbol name (supports LIKE wildcards and substring match).

        Returns:
            Dict with keys:
                symbol:         The queried symbol name.
                decisions:      List of decision dicts (direct + indirect).
                change_history: List of symbol change dicts with commit metadata.
        """
        pattern = qualified_name if "%" in qualified_name else f"%{qualified_name}%"

        cursor = self.conn.execute(
            """SELECT dd.dd_id, dd.ticket, dd.title, dd.rationale,
                      dd.alternatives, dd.trade_offs, dd.status,
                      dd.extraction_method, dd.source_file
               FROM decision_symbols ds
               JOIN design_decisions dd ON dd.id = ds.decision_id
               WHERE ds.symbol_name LIKE ?""",
            (pattern,),
        )
        direct_decisions = rows_to_dicts(cursor.fetchall())

        cursor = self.conn.execute(
            """SELECT sc.change_type, sc.qualified_name,
                      s.sha, s.date, s.message, s.ticket_number, s.phase
               FROM symbol_changes sc
               JOIN snapshots s ON s.id = sc.snapshot_id
               WHERE sc.qualified_name LIKE ?
               ORDER BY s.date""",
            (pattern,),
        )
        changes = rows_to_dicts(cursor.fetchall())

        ticket_numbers = {c["ticket_number"] for c in changes if c.get("ticket_number")}
        indirect_decisions = []
        if ticket_numbers:
            placeholders = ",".join("?" * len(ticket_numbers))
            cursor = self.conn.execute(
                f"""SELECT dd_id, ticket, title, rationale, status, extraction_method
                    FROM design_decisions
                    WHERE ticket IN ({placeholders})""",
                list(ticket_numbers),
            )
            indirect_decisions = rows_to_dicts(cursor.fetchall())

        seen_ids = {d["dd_id"] for d in direct_decisions}
        for d in indirect_decisions:
            if d["dd_id"] not in seen_ids:
                d["link_type"] = "indirect (via ticket)"
                direct_decisions.append(d)

        return {
            "symbol": qualified_name,
            "decisions": direct_decisions,
            "change_history": changes,
        }
