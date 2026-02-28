"""
Git and GitHub operations for workflow engine agents.

Provides functions for branch management, committing, pushing, and
PR creation/management. All functions use subprocess to invoke git/gh
CLI tools and return structured dicts.

Functions are designed to be idempotent and handle errors gracefully
(return error info rather than raising exceptions).
"""

import json
import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Any


# Phase name → commit message prefix
PHASE_PREFIX_MAP = {
    "C++ Design": "design:",
    "Python Design": "design:",
    "Frontend Design": "design:",
    "Integration Design": "design:",
    "C++ Implementation": "impl:",
    "Python Implementation": "impl:",
    "Frontend Implementation": "impl:",
    "C++ Test Writing": "test:",
    "Python Test Writing": "test:",
    "Design Review": "review:",
    "Math Review": "review:",
    "Implementation Review": "review:",
    "Documentation Update": "docs:",
    "Math Formulation": "math:",
}


def _run_git(
    project_root: Path, args: list[str], check: bool = False
) -> subprocess.CompletedProcess:
    """Run a git command and return the CompletedProcess result."""
    return subprocess.run(
        ["git"] + args,
        capture_output=True,
        text=True,
        cwd=project_root,
        check=check,
    )


def _run_gh(
    project_root: Path, args: list[str], check: bool = False
) -> subprocess.CompletedProcess:
    """Run a gh CLI command and return the CompletedProcess result."""
    return subprocess.run(
        ["gh"] + args,
        capture_output=True,
        text=True,
        cwd=project_root,
        check=check,
    )


def _derive_branch_name(full_name: str) -> str:
    """
    Derive a git branch name from a ticket full_name.

    Examples:
        "0041_feature_name"  → "0041-feature-name"
        "0083a_workflow_engine" → "0083a-workflow-engine"
    """
    return re.sub(r"_", "-", full_name)


def _feature_name_from_full_name(full_name: str) -> str:
    """
    Extract a human-readable feature name from a ticket full_name.

    Examples:
        "0041_feature_name" → "feature name"
        "0083a_workflow_engine_extraction" → "workflow engine extraction"
    """
    # Strip leading ticket number (digits + optional letter suffix)
    stripped = re.sub(r"^\d+[a-z]?_", "", full_name)
    return stripped.replace("_", " ")


def setup_branch(
    project_root: Path,
    ticket_id: str,
    conn: sqlite3.Connection,
) -> dict[str, Any]:
    """
    Create or check out a branch for the given ticket.

    Derives the branch name from the ticket's full_name. If the branch
    already exists, checks it out. Otherwise, creates it from main.

    Returns:
        {branch_name, created, checked_out} on success
        {error: str} on failure
    """
    row = conn.execute(
        "SELECT id, full_name FROM tickets WHERE id = ?", (ticket_id,)
    ).fetchone()

    if row is None:
        return {"error": f"Ticket '{ticket_id}' not found"}

    branch_name = _derive_branch_name(row["full_name"])

    # Check if branch already exists
    result = _run_git(project_root, ["branch", "--list", branch_name])
    branch_exists = branch_name in result.stdout.strip()

    if branch_exists:
        checkout = _run_git(project_root, ["checkout", branch_name])
        if checkout.returncode != 0:
            return {"error": f"Failed to checkout branch: {checkout.stderr.strip()}"}
        return {
            "branch_name": branch_name,
            "created": False,
            "checked_out": True,
        }
    else:
        checkout = _run_git(project_root, ["checkout", "-b", branch_name, "main"])
        if checkout.returncode != 0:
            return {"error": f"Failed to create branch: {checkout.stderr.strip()}"}
        return {
            "branch_name": branch_name,
            "created": True,
            "checked_out": True,
        }


def commit_and_push(
    project_root: Path,
    conn: sqlite3.Connection,
    agent_id: str,
    phase_id: int,
    file_paths: list[str],
    message: str | None = None,
) -> dict[str, Any]:
    """
    Stage files, commit, and push to remote.

    If no message is provided, auto-generates one from the phase/ticket info
    using the PHASE_PREFIX_MAP.

    Returns:
        {commit_sha, pushed, branch_name, files_staged} on success
        {error: str} on failure
    """
    # Stage files
    add_result = _run_git(project_root, ["add"] + file_paths)
    if add_result.returncode != 0:
        return {"error": f"git add failed: {add_result.stderr.strip()}"}

    # Auto-generate commit message if not provided
    if message is None:
        row = conn.execute(
            "SELECT p.phase_name, t.full_name "
            "FROM phases p JOIN tickets t ON p.ticket_id = t.id "
            "WHERE p.id = ?",
            (phase_id,),
        ).fetchone()

        if row is None:
            return {"error": f"Phase {phase_id} not found"}

        prefix = PHASE_PREFIX_MAP.get(row["phase_name"], "chore:")
        feature_name = _feature_name_from_full_name(row["full_name"])
        message = (
            f"{prefix} {feature_name}\n\n"
            "Co-Authored-By: Claude <noreply@anthropic.com>"
        )

    # Commit
    commit_result = _run_git(project_root, ["commit", "-m", message])
    if commit_result.returncode != 0:
        return {"error": f"git commit failed: {commit_result.stderr.strip()}"}

    # Determine current branch
    branch_result = _run_git(project_root, ["branch", "--show-current"])
    branch_name = branch_result.stdout.strip()

    # Check if upstream is set
    upstream_result = _run_git(
        project_root, ["rev-parse", "--abbrev-ref", f"{branch_name}@{{upstream}}"]
    )
    has_upstream = upstream_result.returncode == 0

    # Push
    if has_upstream:
        push_result = _run_git(project_root, ["push"])
    else:
        push_result = _run_git(project_root, ["push", "-u", "origin", branch_name])

    if push_result.returncode != 0:
        return {"error": f"git push failed: {push_result.stderr.strip()}"}

    # Get commit SHA
    sha_result = _run_git(project_root, ["rev-parse", "HEAD"])
    commit_sha = sha_result.stdout.strip()

    return {
        "commit_sha": commit_sha,
        "pushed": True,
        "branch_name": branch_name,
        "files_staged": file_paths,
    }


def create_or_update_pr(
    project_root: Path,
    conn: sqlite3.Connection,
    agent_id: str,
    phase_id: int,
    title: str | None = None,
    body: str | None = None,
    draft: bool = True,
) -> dict[str, Any]:
    """
    Create a new PR or update an existing one for the current branch.

    If a PR already exists for this branch, transitions it from draft
    to ready if draft=False. Otherwise creates a new PR with auto-generated
    title and body from ticket/phase info.

    Returns:
        {pr_number, pr_url, action} on success
        {error: str} on failure
    """
    # Get current branch
    branch_result = _run_git(project_root, ["branch", "--show-current"])
    branch_name = branch_result.stdout.strip()

    if not branch_name:
        return {"error": "Could not determine current branch"}

    # Check for existing PR on this branch
    pr_check = _run_gh(
        project_root,
        ["pr", "list", "--head", branch_name, "--json", "number,url", "--jq", ".[0]"],
    )

    existing_pr = None
    if pr_check.returncode == 0 and pr_check.stdout.strip():
        try:
            existing_pr = json.loads(pr_check.stdout.strip())
        except json.JSONDecodeError:
            pass

    if existing_pr:
        # PR already exists
        pr_number = existing_pr["number"]
        pr_url = existing_pr["url"]

        if not draft:
            # Transition from draft to ready
            ready_result = _run_gh(
                project_root, ["pr", "ready", str(pr_number)]
            )
            if ready_result.returncode != 0:
                return {
                    "error": f"Failed to mark PR ready: {ready_result.stderr.strip()}"
                }
            return {
                "pr_number": pr_number,
                "pr_url": pr_url,
                "action": "marked_ready",
            }

        return {
            "pr_number": pr_number,
            "pr_url": pr_url,
            "action": "already_exists",
        }

    # Look up ticket/phase info for auto-generated title/body
    row = conn.execute(
        "SELECT p.phase_name, t.id AS ticket_id, t.full_name "
        "FROM phases p JOIN tickets t ON p.ticket_id = t.id "
        "WHERE p.id = ?",
        (phase_id,),
    ).fetchone()

    if row is None:
        return {"error": f"Phase {phase_id} not found"}

    ticket_id = row["ticket_id"]
    feature_name = _feature_name_from_full_name(row["full_name"])

    if title is None:
        title = f"{ticket_id}: {feature_name.title()}"

    if body is None:
        body = (
            f"## Ticket: {ticket_id}\n\n"
            f"Phase: {row['phase_name']}\n\n"
            f"Agent: {agent_id}\n"
        )

    # Build gh pr create args
    create_args = [
        "pr", "create",
        "--title", title,
        "--body", body,
        "--label", f"ticket:{ticket_id}",
        "--label", f"phase:{row['phase_name']}",
    ]
    if draft:
        create_args.append("--draft")

    create_result = _run_gh(project_root, create_args)

    if create_result.returncode != 0:
        return {"error": f"gh pr create failed: {create_result.stderr.strip()}"}

    # Parse PR URL from output to get number
    pr_url = create_result.stdout.strip()
    pr_number = None
    match = re.search(r"/pull/(\d+)", pr_url)
    if match:
        pr_number = int(match.group(1))

    return {
        "pr_number": pr_number,
        "pr_url": pr_url,
        "action": "created",
    }


def post_pr_comment(
    project_root: Path,
    body: str,
) -> dict[str, Any]:
    """
    Post a comment on the PR associated with the current branch.

    Returns:
        {pr_number, commented} on success
        {error: str} on failure
    """
    # Get current branch
    branch_result = _run_git(project_root, ["branch", "--show-current"])
    branch_name = branch_result.stdout.strip()

    if not branch_name:
        return {"error": "Could not determine current branch"}

    # Find PR for this branch
    pr_check = _run_gh(
        project_root,
        ["pr", "list", "--head", branch_name, "--json", "number", "--jq", ".[0].number"],
    )

    if pr_check.returncode != 0 or not pr_check.stdout.strip():
        return {"error": f"No PR found for branch '{branch_name}'"}

    try:
        pr_number = int(pr_check.stdout.strip())
    except ValueError:
        return {"error": f"Could not parse PR number from: {pr_check.stdout.strip()}"}

    # Post comment
    comment_result = _run_gh(
        project_root, ["pr", "comment", str(pr_number), "--body", body]
    )

    if comment_result.returncode != 0:
        return {"error": f"gh pr comment failed: {comment_result.stderr.strip()}"}

    return {
        "pr_number": pr_number,
        "commented": True,
    }
