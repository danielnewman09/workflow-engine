"""Record layer tables: record_layer_fields, record_layer_mapping, and FTS."""

import sqlite3


def create_record_layer_fields_table(conn: sqlite3.Connection, prefix: str = "") -> None:
    """Per-field metadata for each record across the four architecture layers.

    Tracks field names, types, and layer-specific notes for each record
    (e.g. C++ struct, SQLite table, pybind class, Pydantic model).
    Used by the drift analysis to detect mismatches between layers.

    Columns:
        record_name:  Name of the record (e.g. "ConvexHull").
        layer:        Architecture layer ("cpp", "sql", "pybind", "pydantic").
        field_name:   Name of the field in this layer.
        field_type:   Data type of the field.
        source_field: Corresponding field name in another layer, if mapped.
        notes:        Free-text notes about the field.

    Indexes:
        idx_rlf_record: Lookup by record name.
        idx_rlf_layer:  Filter by layer.
    """
    conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS {prefix}record_layer_fields (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            record_name    TEXT NOT NULL,
            layer          TEXT NOT NULL,
            field_name     TEXT NOT NULL,
            field_type     TEXT,
            source_field   TEXT,
            notes          TEXT
        );
        CREATE INDEX IF NOT EXISTS {prefix}idx_rlf_record ON {prefix}record_layer_fields(record_name);
        CREATE INDEX IF NOT EXISTS {prefix}idx_rlf_layer ON {prefix}record_layer_fields(layer);
    """)


def create_record_layer_mapping_table(conn: sqlite3.Connection, prefix: str = "") -> None:
    """Cross-layer name mapping for records.

    Maps a canonical record name to its concrete names in each
    architecture layer (Pydantic model class, pybind class, SQL table).
    Used to correlate fields across layers during drift analysis.

    Columns:
        record_name:    Canonical record name (unique).
        pydantic_model: Pydantic model class name.
        pybind_class:   pybind11 wrapper class name.
        sql_table:      SQLite table name.

    Indexes:
        idx_rlm_pydantic: Reverse lookup by Pydantic model name.
    """
    conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS {prefix}record_layer_mapping (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            record_name     TEXT NOT NULL UNIQUE,
            pydantic_model  TEXT,
            pybind_class    TEXT,
            sql_table       TEXT
        );
        CREATE INDEX IF NOT EXISTS {prefix}idx_rlm_pydantic ON {prefix}record_layer_mapping(pydantic_model);
    """)


def create_record_fts(conn: sqlite3.Connection) -> None:
    """FTS5 virtual table for full-text search over record layer fields.

    Content-synced with record_layer_fields via content= directive.
    Uses Porter stemming and Unicode tokenization.

    Does not support schema prefixes — only for standalone databases.
    """
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS record_layer_fields_fts USING fts5(
            field_name,
            field_type,
            notes,
            content=record_layer_fields,
            content_rowid=id,
            tokenize='porter unicode61'
        )
    """)


def create_record_tables(conn: sqlite3.Connection, prefix: str = "") -> None:
    """Create all record tables: record_layer_fields and record_layer_mapping."""
    create_record_layer_fields_table(conn, prefix)
    create_record_layer_mapping_table(conn, prefix)
