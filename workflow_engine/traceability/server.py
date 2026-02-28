"""
Traceability Query Server

Provides design decision traceability, symbol history, and ticket impact
analysis. Operates on an already-open SQLite connection (typically the
traceability DB ATTACHed to the workflow DB).

Tools provided:
    - search_decisions: FTS search across design decision rationale
    - get_decision: Full details of a decision with linked symbols and commits
    - get_symbol_history: Timeline of changes to a symbol across commits
    - get_ticket_impact: All commits, file changes, symbol changes, and decisions for a ticket
    - get_commit_context: Context for a commit: ticket, phase, decisions, symbol changes
    - why_symbol: Design decision(s) that created/modified a symbol, with rationale
    - get_snapshot_symbols: All symbols at a specific point in time
    - get_record_mappings: All four layers' field lists for a record with drift analysis
    - check_record_drift: All records with fields missing from downstream layers
"""

import re
import sqlite3
from typing import Any


class TraceabilityServer:
    """Server for design decision traceability queries."""

    def __init__(self, conn: sqlite3.Connection):
        """Initialize with an already-open connection.

        Args:
            conn: SQLite connection (standalone or with ATTACHed traceability DB).
        """
        self.conn = conn

    def _rows_to_dicts(self, rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        return [dict(row) for row in rows]

    @staticmethod
    def _split_pascal_case(text: str) -> list[str]:
        tokens = text.split()
        words = []
        for token in tokens:
            parts = re.sub(r'([a-z])([A-Z])', r'\1 \2', token)
            parts = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', parts)
            words.extend(parts.split())
        return words

    # =========================================================================
    # Design Decision Tools
    # =========================================================================

    def search_decisions(
        self, query: str, ticket: str | None = None, status: str | None = None
    ) -> list[dict]:
        """FTS search across design decision rationale."""
        results = []

        try:
            sql = """
                SELECT dd.dd_id, dd.ticket, dd.title, dd.rationale,
                       dd.status, dd.extraction_method, dd.source_file,
                       bm25(design_decisions_fts) as rank
                FROM design_decisions_fts fts
                JOIN design_decisions dd ON dd.id = fts.rowid
                WHERE design_decisions_fts MATCH ?
            """
            params: list = [query]

            if ticket:
                sql += " AND dd.ticket = ?"
                params.append(ticket)
            if status:
                sql += " AND dd.status = ?"
                params.append(status)

            sql += " ORDER BY rank LIMIT 20"
            cursor = self.conn.execute(sql, params)
            results = self._rows_to_dicts(cursor.fetchall())
        except sqlite3.OperationalError:
            pass

        if results:
            return results

        words = self._split_pascal_case(query)
        if not words:
            return []

        word_clauses = []
        params = []
        for word in words:
            pattern = f"%{word}%"
            word_clauses.append(
                "(title LIKE ? OR rationale LIKE ? OR alternatives LIKE ? OR trade_offs LIKE ?)"
            )
            params.extend([pattern] * 4)

        sql = f"""
            SELECT dd_id, ticket, title, rationale, status,
                   extraction_method, source_file
            FROM design_decisions
            WHERE {' AND '.join(word_clauses)}
        """

        if ticket:
            sql += " AND ticket = ?"
            params.append(ticket)
        if status:
            sql += " AND status = ?"
            params.append(status)

        sql += " LIMIT 20"
        cursor = self.conn.execute(sql, params)
        return self._rows_to_dicts(cursor.fetchall())

    def get_decision(self, dd_id: str) -> dict:
        """Get full details of a design decision with linked symbols and commits."""
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
        result["commits"] = self._rows_to_dicts(cursor.fetchall())

        result.pop("id", None)
        return result

    # =========================================================================
    # Symbol History Tools
    # =========================================================================

    def get_symbol_history(self, qualified_name: str) -> list[dict]:
        """Timeline of changes to a symbol across commits."""
        pattern = qualified_name if "%" in qualified_name else f"%{qualified_name}%"

        cursor = self.conn.execute(
            """SELECT sc.change_type, sc.qualified_name,
                      sc.old_file_path, sc.new_file_path,
                      sc.old_line, sc.new_line,
                      sc.old_signature, sc.new_signature,
                      s.sha, s.date, s.message, s.ticket_number, s.phase
               FROM symbol_changes sc
               JOIN snapshots s ON s.id = sc.snapshot_id
               WHERE sc.qualified_name LIKE ?
               ORDER BY s.date""",
            (pattern,),
        )
        return self._rows_to_dicts(cursor.fetchall())

    # =========================================================================
    # Ticket Impact Tools
    # =========================================================================

    def get_ticket_impact(self, ticket_number: str) -> dict:
        """All commits, file changes, symbol changes, and decisions for a ticket."""
        cursor = self.conn.execute(
            """SELECT id, sha, date, author, message, prefix, phase
               FROM snapshots
               WHERE ticket_number = ?
               ORDER BY date""",
            (ticket_number,),
        )
        commits = self._rows_to_dicts(cursor.fetchall())

        snapshot_ids = [c["id"] for c in commits]
        file_changes = []
        symbol_changes = []

        if snapshot_ids:
            placeholders = ",".join("?" * len(snapshot_ids))

            cursor = self.conn.execute(
                f"""SELECT file_path, change_type,
                           SUM(insertions) as total_insertions,
                           SUM(deletions) as total_deletions,
                           COUNT(*) as touch_count
                    FROM file_changes
                    WHERE snapshot_id IN ({placeholders})
                    GROUP BY file_path
                    ORDER BY touch_count DESC""",
                snapshot_ids,
            )
            file_changes = self._rows_to_dicts(cursor.fetchall())

            cursor = self.conn.execute(
                f"""SELECT sc.qualified_name, sc.change_type,
                           sc.old_file_path, sc.new_file_path,
                           s.sha, s.date, s.message
                    FROM symbol_changes sc
                    JOIN snapshots s ON s.id = sc.snapshot_id
                    WHERE sc.snapshot_id IN ({placeholders})
                    ORDER BY s.date""",
                snapshot_ids,
            )
            symbol_changes = self._rows_to_dicts(cursor.fetchall())

        cursor = self.conn.execute(
            """SELECT dd_id, title, rationale, status, extraction_method
               FROM design_decisions
               WHERE ticket = ?""",
            (ticket_number,),
        )
        decisions = self._rows_to_dicts(cursor.fetchall())

        if not commits and not decisions:
            return {"error": f"No data found for ticket '{ticket_number}'"}

        for c in commits:
            c.pop("id", None)

        return {
            "ticket": ticket_number,
            "commits": commits,
            "file_changes": file_changes,
            "symbol_changes": symbol_changes,
            "decisions": decisions,
        }

    # =========================================================================
    # Commit Context Tools
    # =========================================================================

    def get_commit_context(self, commit_sha: str) -> dict:
        """Context for a commit: ticket, phase, decisions, symbol changes."""
        cursor = self.conn.execute(
            "SELECT * FROM snapshots WHERE sha LIKE ?",
            (f"{commit_sha}%",),
        )
        row = cursor.fetchone()
        if not row:
            return {"error": f"Commit '{commit_sha}' not found"}

        result = dict(row)
        snapshot_id = result["id"]

        cursor = self.conn.execute(
            """SELECT file_path, change_type, insertions, deletions, old_path
               FROM file_changes WHERE snapshot_id = ?""",
            (snapshot_id,),
        )
        result["file_changes"] = self._rows_to_dicts(cursor.fetchall())

        cursor = self.conn.execute(
            """SELECT qualified_name, change_type,
                      old_file_path, new_file_path,
                      old_line, new_line,
                      old_signature, new_signature
               FROM symbol_changes WHERE snapshot_id = ?""",
            (snapshot_id,),
        )
        result["symbol_changes"] = self._rows_to_dicts(cursor.fetchall())

        decisions = []
        if result.get("ticket_number"):
            cursor = self.conn.execute(
                """SELECT dd_id, title, rationale, status
                   FROM design_decisions WHERE ticket = ?""",
                (result["ticket_number"],),
            )
            decisions = self._rows_to_dicts(cursor.fetchall())

        cursor = self.conn.execute(
            """SELECT dd.dd_id, dd.title, dd.rationale, dd.status
               FROM decision_commits dc
               JOIN design_decisions dd ON dd.id = dc.decision_id
               WHERE dc.snapshot_id = ?""",
            (snapshot_id,),
        )
        direct_links = self._rows_to_dicts(cursor.fetchall())
        seen_ids = {d["dd_id"] for d in decisions}
        for d in direct_links:
            if d["dd_id"] not in seen_ids:
                decisions.append(d)

        result["decisions"] = decisions
        result.pop("id", None)
        return result

    # =========================================================================
    # Why-Symbol Tool
    # =========================================================================

    def why_symbol(self, qualified_name: str) -> dict:
        """Design decision(s) that created/modified a symbol, with rationale."""
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
        direct_decisions = self._rows_to_dicts(cursor.fetchall())

        cursor = self.conn.execute(
            """SELECT sc.change_type, sc.qualified_name,
                      s.sha, s.date, s.message, s.ticket_number, s.phase
               FROM symbol_changes sc
               JOIN snapshots s ON s.id = sc.snapshot_id
               WHERE sc.qualified_name LIKE ?
               ORDER BY s.date""",
            (pattern,),
        )
        changes = self._rows_to_dicts(cursor.fetchall())

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
            indirect_decisions = self._rows_to_dicts(cursor.fetchall())

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

    # =========================================================================
    # Snapshot Symbols Tool
    # =========================================================================

    def get_snapshot_symbols(
        self, commit_sha: str, file_path: str | None = None
    ) -> list[dict]:
        """All symbols at a specific point in time."""
        cursor = self.conn.execute(
            "SELECT id FROM snapshots WHERE sha LIKE ?",
            (f"{commit_sha}%",),
        )
        row = cursor.fetchone()
        if not row:
            return [{"error": f"Commit '{commit_sha}' not found"}]

        snapshot_id = row["id"]

        if file_path:
            cursor = self.conn.execute(
                """SELECT qualified_name, kind, file_path, line_number, signature, class_scope
                   FROM symbol_snapshots
                   WHERE snapshot_id = ? AND file_path LIKE ?
                   ORDER BY file_path, line_number""",
                (snapshot_id, f"%{file_path}%"),
            )
        else:
            cursor = self.conn.execute(
                """SELECT qualified_name, kind, file_path, line_number, signature, class_scope
                   FROM symbol_snapshots
                   WHERE snapshot_id = ?
                   ORDER BY file_path, line_number""",
                (snapshot_id,),
            )

        return self._rows_to_dicts(cursor.fetchall())

    # =========================================================================
    # Record Layer Mapping Tools
    # =========================================================================

    def get_record_mappings(self, record_name: str) -> dict:
        """Return all four layers' field lists for a record with drift analysis."""
        cursor = self.conn.execute(
            "SELECT * FROM record_layer_mapping WHERE record_name = ?",
            (record_name,),
        )
        mapping_row = cursor.fetchone()
        if not mapping_row:
            return {"error": f"Record '{record_name}' not found"}

        layers = {}
        for layer in ["cpp", "sql", "pybind", "pydantic"]:
            cursor = self.conn.execute(
                """SELECT field_name, field_type, source_field, notes
                   FROM record_layer_fields
                   WHERE record_name = ? AND layer = ?
                   ORDER BY field_name""",
                (record_name, layer),
            )
            layers[layer] = self._rows_to_dicts(cursor.fetchall())

        cpp_fields = {f["field_name"] for f in layers["cpp"]}
        pybind_fields = {f["field_name"] for f in layers["pybind"]}
        pydantic_fields = {f["field_name"] for f in layers["pydantic"]}

        missing_in_pybind = []
        for f in layers["cpp"]:
            field_name = f["field_name"]
            fk_name = f"{field_name}_id"
            if field_name not in pybind_fields and fk_name not in pybind_fields:
                missing_in_pybind.append(field_name)

        missing_in_pydantic = []
        for f in layers["cpp"]:
            field_name = f["field_name"]
            fk_name = f"{field_name}_id"
            if field_name not in pydantic_fields and fk_name not in pydantic_fields:
                missing_in_pydantic.append(field_name)

        return {
            "record": record_name,
            "pydantic_model": dict(mapping_row).get("pydantic_model"),
            "layers": layers,
            "drift": {
                "missing_in_pybind": missing_in_pybind,
                "missing_in_pydantic": missing_in_pydantic,
                "naming_mismatches": [],
            },
        }

    def check_record_drift(self) -> list[dict]:
        """Return all records with fields missing from downstream layers."""
        cursor = self.conn.execute("SELECT record_name FROM record_layer_mapping")
        all_records = [row["record_name"] for row in cursor.fetchall()]

        drift_records = []
        for record_name in all_records:
            result = self.get_record_mappings(record_name)
            if "error" in result:
                continue

            drift = result["drift"]
            if drift["missing_in_pybind"] or drift["missing_in_pydantic"]:
                drift_records.append(
                    {
                        "record": record_name,
                        "missing_in_pybind": drift["missing_in_pybind"],
                        "missing_in_pydantic": drift["missing_in_pydantic"],
                    }
                )

        return drift_records
