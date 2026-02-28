"""
Traceability Module — Design decision and symbol history tracking.

Provides indexers for git history, C++ symbols, design decisions, and record
layer mappings, plus a query server for MCP tool integration.
"""

from workflow_engine.traceability.schema import (
    SCHEMA_VERSION,
    create_schema,
    get_schema_version,
    rebuild_fts,
)

__all__ = [
    "SCHEMA_VERSION",
    "create_schema",
    "get_schema_version",
    "rebuild_fts",
]
