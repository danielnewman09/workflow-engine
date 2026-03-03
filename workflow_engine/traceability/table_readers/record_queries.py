"""SQL query builders for record layer mapping queries.

Each function returns a (sql, params) tuple, keeping query construction
separate from execution.  This makes queries independently testable and
self-documenting via their docstrings.
"""

from __future__ import annotations


def get_record_mapping(record_name: str) -> tuple[str, list]:
    """Fetch a single record layer mapping row."""
    return "SELECT * FROM record_layer_mapping WHERE record_name = ?", [record_name]


def get_record_layer_fields(record_name: str, layer: str) -> tuple[str, list]:
    """Fields for a specific record and layer, ordered by name."""
    sql = """SELECT field_name, field_type, source_field, notes
             FROM record_layer_fields
             WHERE record_name = ? AND layer = ?
             ORDER BY field_name"""
    return sql, [record_name, layer]


def list_all_record_names() -> tuple[str, list]:
    """All record names from the mapping table."""
    return "SELECT record_name FROM record_layer_mapping", []
