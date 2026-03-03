"""
Traceability Reindex Orchestrator

Runs all indexers in sequence against an open database connection.
Used by the workflow engine to trigger incremental reindexing
(e.g., after commit_and_push or ticket completion).
"""

import sqlite3
from typing import Any

from workflow_engine.traceability.index_git import index_git_history
from workflow_engine.traceability.index_decisions import index_decisions
from workflow_engine.traceability.index_symbols import index_symbols
from workflow_engine.traceability.index_records import index_records
from workflow_engine.traceability.index_tickets import index_tickets
from workflow_engine.traceability.index_coverage import index_coverage


def reindex_all(
    conn: sqlite3.Connection,
    repo_root: str,
    *,
    source_dir: str = "msd",
    designs_dir: str = "docs/designs",
    tickets_dir: str = "tickets",
    models_path: str = "replay/replay/models.py",
    generated_models_path: str = "replay/replay/generated_models.py",
    skip_symbols: bool = False,
    coverage_info_path: str = "build/Debug/coverage_filtered.info",
    store_coverage_lines: bool = True,
) -> dict[str, Any]:
    """Run all traceability indexers incrementally.

    Args:
        conn: Open SQLite connection to the traceability database.
        repo_root: Path to the git repository root.
        source_dir: Relative path to C++ source directory.
        designs_dir: Relative path to designs directory.
        tickets_dir: Relative path to tickets directory.
        models_path: Relative path to Pydantic models file.
        generated_models_path: Relative path to generated models file.
        skip_symbols: If True, skip the symbol indexer (expensive).
        coverage_info_path: Relative path to lcov .info file.
        store_coverage_lines: Whether to store per-line coverage detail.

    Returns:
        Dict with results from each indexer.
    """
    results: dict[str, Any] = {}

    results["git"] = index_git_history(conn, repo_root)

    results["decisions"] = index_decisions(
        conn, repo_root, designs_dir=designs_dir, tickets_dir=tickets_dir
    )

    if not skip_symbols:
        results["symbols"] = index_symbols(
            conn, repo_root, source_dir=source_dir
        )
    else:
        results["symbols"] = {"skipped": True}

    results["records"] = index_records(
        conn, repo_root,
        models_path=models_path,
        generated_models_path=generated_models_path,
    )

    results["tickets"] = index_tickets(
        conn, repo_root, tickets_dir=tickets_dir,
    )

    results["coverage"] = index_coverage(
        conn, repo_root,
        info_file=coverage_info_path,
        store_lines=store_coverage_lines,
    )

    return results
