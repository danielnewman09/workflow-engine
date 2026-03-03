"""Coverage tables: coverage_runs, coverage_files, and coverage_lines."""

import sqlite3


def create_coverage_runs_table(conn: sqlite3.Connection, prefix: str = "") -> None:
    """One row per coverage collection run.

    Captures aggregate coverage statistics and metadata for a single
    test-coverage run, typically produced from lcov/gcov output.

    Columns:
        timestamp:      ISO-8601 timestamp of the coverage run.
        commit_sha:     Git commit SHA the coverage was collected against.
        total_lines:    Total source lines analyzed.
        total_hits:     Total lines executed during tests.
        line_rate:      Line coverage ratio (0.0–1.0).
        branch_rate:    Branch coverage ratio (0.0–1.0).
        function_rate:  Function coverage ratio (0.0–1.0).
        info_file_path: Path to the raw .info coverage file.

    Indexes:
        idx_cr_sha: Lookup by commit SHA.
    """
    conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS {prefix}coverage_runs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp       TEXT NOT NULL,
            commit_sha      TEXT,
            total_lines     INTEGER NOT NULL DEFAULT 0,
            total_hits      INTEGER NOT NULL DEFAULT 0,
            line_rate       REAL,
            branch_rate     REAL,
            function_rate   REAL,
            info_file_path  TEXT
        );
        CREATE INDEX IF NOT EXISTS {prefix}idx_cr_sha ON {prefix}coverage_runs(commit_sha);
    """)


def create_coverage_files_table(conn: sqlite3.Connection, prefix: str = "") -> None:
    """Per-file coverage summary within a coverage run.

    Breaks down coverage statistics by source file, recording how
    many lines, functions, and branches were found vs. hit.

    Columns:
        run_id:          FK to coverage_runs.id.
        file_path:       Source file path.
        lines_found:     Total lines in this file.
        lines_hit:       Lines executed during tests.
        functions_found: Total functions in this file.
        functions_hit:   Functions called during tests.
        branches_found:  Total branches in this file.
        branches_hit:    Branches taken during tests.

    Indexes:
        idx_cf_run:  Join with coverage_runs.
        idx_cf_path: Lookup by file path.
    """
    conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS {prefix}coverage_files (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id          INTEGER NOT NULL REFERENCES {prefix}coverage_runs(id),
            file_path       TEXT NOT NULL,
            lines_found     INTEGER NOT NULL DEFAULT 0,
            lines_hit       INTEGER NOT NULL DEFAULT 0,
            functions_found INTEGER NOT NULL DEFAULT 0,
            functions_hit   INTEGER NOT NULL DEFAULT 0,
            branches_found  INTEGER NOT NULL DEFAULT 0,
            branches_hit    INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS {prefix}idx_cf_run ON {prefix}coverage_files(run_id);
        CREATE INDEX IF NOT EXISTS {prefix}idx_cf_path ON {prefix}coverage_files(file_path);
    """)


def create_coverage_lines_table(conn: sqlite3.Connection, prefix: str = "") -> None:
    """Per-line coverage detail within a coverage run.

    Records individual line execution counts, allowing precise
    identification of uncovered code.

    Columns:
        run_id:      FK to coverage_runs.id.
        file_path:   Source file path.
        line_number: Line number in the source file.
        hit_count:   Number of times this line was executed.

    Indexes:
        idx_cl_run_file: Composite index for efficient per-file queries.
    """
    conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS {prefix}coverage_lines (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id          INTEGER NOT NULL REFERENCES {prefix}coverage_runs(id),
            file_path       TEXT NOT NULL,
            line_number     INTEGER NOT NULL,
            hit_count       INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS {prefix}idx_cl_run_file ON {prefix}coverage_lines(run_id, file_path);
    """)


def create_coverage_tables(conn: sqlite3.Connection, prefix: str = "") -> None:
    """Create all coverage tables: coverage_runs, coverage_files, and coverage_lines."""
    create_coverage_runs_table(conn, prefix)
    create_coverage_files_table(conn, prefix)
    create_coverage_lines_table(conn, prefix)
