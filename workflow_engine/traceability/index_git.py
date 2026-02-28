"""
Git History Indexer

Walks all git commits in chronological order, extracts metadata (prefix, ticket
number, phase) from commit messages, and records file-level diffs. Populates the
`snapshots` and `file_changes` tables in the traceability database.

Supports incremental indexing — commits already present are skipped.
"""

import re
import sqlite3
import subprocess

# Regex patterns for commit message parsing
PREFIX_RE = re.compile(r"^(\w[\w-]*):\s")
TICKET_RE = re.compile(r"(\d{4}[a-e]?)")

# Map commit prefixes to workflow phases
PREFIX_TO_PHASE = {
    "design": "design",
    "review": "review",
    "impl": "implementation",
    "implement": "implementation",
    "fix": "fix",
    "bugfix": "fix",
    "docs": "documentation",
    "doc": "documentation",
    "test": "testing",
    "bench": "benchmarking",
    "refactor": "refactoring",
    "chore": "chore",
    "feat": "feature",
    "perf": "performance",
    "quality-gate": "quality-gate",
    "prototype": "prototype",
    "math": "math-design",
}


def parse_commit_message(message: str) -> tuple[str | None, str | None, str | None]:
    """Extract prefix, ticket number, and phase from a commit message.

    Returns:
        Tuple of (prefix, ticket_number, phase). Any may be None.
    """
    prefix = None
    phase = None

    prefix_match = PREFIX_RE.match(message)
    if prefix_match:
        prefix = prefix_match.group(1).lower()
        phase = PREFIX_TO_PHASE.get(prefix)

    ticket_number = None
    ticket_match = TICKET_RE.search(message)
    if ticket_match:
        ticket_number = ticket_match.group(1)

    return prefix, ticket_number, phase


def get_all_commits(repo_root: str) -> list[dict]:
    """Get all commits in chronological order (oldest first)."""
    result = subprocess.run(
        ["git", "log", "--reverse", "--format=%H%x00%aI%x00%an%x00%s"],
        capture_output=True, text=True, cwd=repo_root, check=True,
    )

    commits = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("\x00", 3)
        if len(parts) == 4:
            commits.append({
                "sha": parts[0],
                "date": parts[1],
                "author": parts[2],
                "message": parts[3],
            })
    return commits


def get_file_changes(sha: str, repo_root: str) -> list[dict]:
    """Get file-level changes for a commit using git diff-tree."""
    numstat_result = subprocess.run(
        ["git", "diff-tree", "--numstat", "-r", "-M", "--no-commit-id", sha],
        capture_output=True, text=True, cwd=repo_root, check=True,
    )

    status_result = subprocess.run(
        ["git", "diff-tree", "--name-status", "-r", "-M", "--no-commit-id", sha],
        capture_output=True, text=True, cwd=repo_root, check=True,
    )

    # Parse name-status output for change types
    status_map = {}
    rename_map = {}
    for line in status_result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) >= 2:
            change_type = parts[0][0]
            file_path = parts[-1]
            status_map[file_path] = change_type
            if change_type == "R" and len(parts) >= 3:
                rename_map[file_path] = parts[1]

    # Parse numstat for insertions/deletions
    changes = []
    for line in numstat_result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) >= 3:
            insertions = int(parts[0]) if parts[0] != "-" else 0
            deletions = int(parts[1]) if parts[1] != "-" else 0
            file_path = parts[2]
            if " => " in file_path:
                file_path = file_path.split(" => ")[-1].rstrip("}")
                if "{" in parts[2]:
                    prefix = parts[2].split("{")[0]
                    suffix = parts[2].split("}")[-1] if "}" in parts[2] else ""
                    new_part = parts[2].split(" => ")[1].split("}")[0]
                    file_path = prefix + new_part + suffix

            change_type = status_map.get(file_path, "M")
            old_path = rename_map.get(file_path)

            changes.append({
                "file_path": file_path,
                "change_type": change_type,
                "insertions": insertions,
                "deletions": deletions,
                "old_path": old_path,
            })

    return changes


def index_git_history(conn: sqlite3.Connection, repo_root: str) -> dict:
    """Index all git commits into the traceability database.

    Args:
        conn: Open SQLite connection to the traceability database.
        repo_root: Path to the git repository root.

    Returns:
        Dict with 'new_count' and 'total' keys.
    """
    # Get existing SHAs to support incremental indexing
    existing_shas = set()
    for row in conn.execute("SELECT sha FROM snapshots"):
        existing_shas.add(row["sha"])

    commits = get_all_commits(repo_root)
    new_count = 0

    for commit in commits:
        if commit["sha"] in existing_shas:
            continue

        prefix, ticket_number, phase = parse_commit_message(commit["message"])

        cursor = conn.execute(
            """INSERT INTO snapshots (sha, date, author, message, prefix, ticket_number, phase)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (commit["sha"], commit["date"], commit["author"], commit["message"],
             prefix, ticket_number, phase),
        )
        snapshot_id = cursor.lastrowid

        changes = get_file_changes(commit["sha"], repo_root)
        for change in changes:
            conn.execute(
                """INSERT INTO file_changes (snapshot_id, file_path, change_type, insertions, deletions, old_path)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (snapshot_id, change["file_path"], change["change_type"],
                 change["insertions"], change["deletions"], change["old_path"]),
            )

        new_count += 1

    conn.commit()

    total = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    return {"new_count": new_count, "total": total}
