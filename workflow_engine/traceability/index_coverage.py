"""
Coverage Indexer

Parses lcov .info files and stores per-file and per-line coverage data
into the traceability database. Each indexing run creates a new snapshot
(coverage_runs) with associated file and line records.
"""

import hashlib
import os
import re
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _get_git_sha(repo_root: str) -> str | None:
    """Get the current HEAD SHA from the repository."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _file_hash(path: Path) -> str:
    """Compute a hash of file path + mtime for deduplication."""
    mtime = path.stat().st_mtime
    return hashlib.sha256(f"{path}:{mtime}".encode()).hexdigest()[:16]


def _make_relative(file_path: str, repo_root: str) -> str:
    """Convert an absolute path to a path relative to repo_root."""
    try:
        return str(Path(file_path).relative_to(repo_root))
    except ValueError:
        return file_path


def parse_lcov_info(content: str, repo_root: str | None = None) -> dict[str, Any]:
    """Parse lcov .info file content into structured data.

    Args:
        content: Raw text content of the .info file.
        repo_root: If provided, file paths are made relative to this root.

    Returns:
        Dict with keys:
            files: list of per-file coverage dicts
            total_lines: total lines found across all files
            total_hits: total lines hit across all files
    """
    files: list[dict[str, Any]] = []
    current_file: dict[str, Any] | None = None

    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue

        if line.startswith("SF:"):
            # Start of file record
            file_path = line[3:]
            if repo_root:
                file_path = _make_relative(file_path, repo_root)
            current_file = {
                "file_path": file_path,
                "lines_found": 0,
                "lines_hit": 0,
                "functions_found": 0,
                "functions_hit": 0,
                "branches_found": 0,
                "branches_hit": 0,
                "line_data": [],
            }

        elif line.startswith("DA:") and current_file is not None:
            # Line data: DA:<line_number>,<execution_count>
            parts = line[3:].split(",")
            if len(parts) >= 2:
                try:
                    line_num = int(parts[0])
                    hit_count = int(parts[1])
                    current_file["line_data"].append((line_num, hit_count))
                except ValueError:
                    pass

        elif line.startswith("LF:") and current_file is not None:
            try:
                current_file["lines_found"] = int(line[3:])
            except ValueError:
                pass

        elif line.startswith("LH:") and current_file is not None:
            try:
                current_file["lines_hit"] = int(line[3:])
            except ValueError:
                pass

        elif line.startswith("FNF:") and current_file is not None:
            try:
                current_file["functions_found"] = int(line[4:])
            except ValueError:
                pass

        elif line.startswith("FNH:") and current_file is not None:
            try:
                current_file["functions_hit"] = int(line[4:])
            except ValueError:
                pass

        elif line.startswith("BRF:") and current_file is not None:
            try:
                current_file["branches_found"] = int(line[4:])
            except ValueError:
                pass

        elif line.startswith("BRH:") and current_file is not None:
            try:
                current_file["branches_hit"] = int(line[4:])
            except ValueError:
                pass

        elif line == "end_of_record" and current_file is not None:
            files.append(current_file)
            current_file = None

    # Handle file without end_of_record
    if current_file is not None:
        files.append(current_file)

    total_lines = sum(f["lines_found"] for f in files)
    total_hits = sum(f["lines_hit"] for f in files)

    return {
        "files": files,
        "total_lines": total_lines,
        "total_hits": total_hits,
    }


def index_coverage(
    conn: sqlite3.Connection,
    repo_root: str,
    *,
    info_file: str = "build/Debug/coverage_filtered.info",
    store_lines: bool = True,
) -> dict:
    """Index lcov coverage data into the traceability database.

    Each call creates a new coverage_run snapshot. Deduplicates by checking
    if the exact .info file (path + mtime) has already been indexed.

    Args:
        conn: Open SQLite connection to the traceability database.
        repo_root: Path to the git repository root.
        info_file: Relative path to the lcov .info file.
        store_lines: Whether to store per-line coverage detail.

    Returns:
        Dict with indexing results: run_id, files_count, total_lines, total_hits, skipped.
    """
    info_path = Path(repo_root) / info_file
    if not info_path.is_file():
        return {
            "run_id": None,
            "files_count": 0,
            "total_lines": 0,
            "total_hits": 0,
            "skipped": True,
            "reason": f"Info file not found: {info_file}",
        }

    # Check if this exact file has already been indexed
    file_id = _file_hash(info_path)
    existing = conn.execute(
        "SELECT id FROM coverage_runs WHERE info_file_path = ?",
        (f"{info_file}:{file_id}",),
    ).fetchone()
    if existing:
        return {
            "run_id": existing["id"],
            "files_count": 0,
            "total_lines": 0,
            "total_hits": 0,
            "skipped": True,
            "reason": "Already indexed (file unchanged)",
        }

    # Parse the .info file
    content = info_path.read_text(encoding="utf-8", errors="replace")
    parsed = parse_lcov_info(content, repo_root=repo_root)

    # Get git SHA
    commit_sha = _get_git_sha(repo_root)

    # Compute rates
    total_lines = parsed["total_lines"]
    total_hits = parsed["total_hits"]
    line_rate = total_hits / total_lines if total_lines > 0 else None

    # Compute aggregate function/branch rates
    total_fn_found = sum(f["functions_found"] for f in parsed["files"])
    total_fn_hit = sum(f["functions_hit"] for f in parsed["files"])
    total_br_found = sum(f["branches_found"] for f in parsed["files"])
    total_br_hit = sum(f["branches_hit"] for f in parsed["files"])
    function_rate = total_fn_hit / total_fn_found if total_fn_found > 0 else None
    branch_rate = total_br_hit / total_br_found if total_br_found > 0 else None

    now = datetime.now(timezone.utc).isoformat()

    # Insert coverage run
    cursor = conn.execute(
        """INSERT INTO coverage_runs
           (timestamp, commit_sha, total_lines, total_hits,
            line_rate, branch_rate, function_rate, info_file_path)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (now, commit_sha, total_lines, total_hits,
         line_rate, branch_rate, function_rate, f"{info_file}:{file_id}"),
    )
    run_id = cursor.lastrowid

    # Insert per-file coverage
    for file_data in parsed["files"]:
        conn.execute(
            """INSERT INTO coverage_files
               (run_id, file_path, lines_found, lines_hit,
                functions_found, functions_hit, branches_found, branches_hit)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, file_data["file_path"], file_data["lines_found"],
             file_data["lines_hit"], file_data["functions_found"],
             file_data["functions_hit"], file_data["branches_found"],
             file_data["branches_hit"]),
        )

        # Insert per-line coverage detail
        if store_lines and file_data["line_data"]:
            conn.executemany(
                """INSERT INTO coverage_lines
                   (run_id, file_path, line_number, hit_count)
                   VALUES (?, ?, ?, ?)""",
                [
                    (run_id, file_data["file_path"], line_num, hit_count)
                    for line_num, hit_count in file_data["line_data"]
                ],
            )

    conn.commit()

    return {
        "run_id": run_id,
        "files_count": len(parsed["files"]),
        "total_lines": total_lines,
        "total_hits": total_hits,
        "skipped": False,
    }
