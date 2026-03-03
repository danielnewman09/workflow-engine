"""SQL query builders for SymbolQuerier.

Each function returns a (sql, params) tuple, keeping query construction
separate from execution.
"""

from __future__ import annotations


def symbol_history(qualified_name: str) -> tuple[str, list]:
    """Chronological timeline of changes to a symbol.

    Joins symbol_changes with snapshots to return change_type,
    old/new file paths, line numbers, signatures, and commit metadata.
    Results ordered by commit date.

    Args:
        qualified_name: Symbol name; if it doesn't contain '%', wrapped
                        in '%…%' for substring matching.
    """
    pattern = qualified_name if "%" in qualified_name else f"%{qualified_name}%"
    sql = """
        SELECT sc.change_type, sc.qualified_name,
               sc.old_file_path, sc.new_file_path,
               sc.old_line, sc.new_line,
               sc.old_signature, sc.new_signature,
               s.sha, s.date, s.message, s.ticket_number, s.phase
        FROM symbol_changes sc
        JOIN snapshots s ON s.id = sc.snapshot_id
        WHERE sc.qualified_name LIKE ?
        ORDER BY s.date
    """
    return sql, [pattern]


def snapshot_symbols(
    snapshot_id: int, *, file_path: str | None = None
) -> tuple[str, list]:
    """All symbols recorded at a given snapshot.

    Returns qualified_name, kind, file_path, line_number, signature,
    and class_scope for every symbol in the snapshot.  Optionally
    filtered by a file_path substring.

    Args:
        snapshot_id: Primary key of the target snapshot row.
        file_path: Optional file path substring to filter results.
    """
    if file_path:
        sql = """
            SELECT qualified_name, kind, file_path, line_number, signature, class_scope
            FROM symbol_snapshots
            WHERE snapshot_id = ? AND file_path LIKE ?
            ORDER BY file_path, line_number
        """
        return sql, [snapshot_id, f"%{file_path}%"]

    sql = """
        SELECT qualified_name, kind, file_path, line_number, signature, class_scope
        FROM symbol_snapshots
        WHERE snapshot_id = ?
        ORDER BY file_path, line_number
    """
    return sql, [snapshot_id]
