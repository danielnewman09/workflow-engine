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
import zlib
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# PlantUML rendering
# ---------------------------------------------------------------------------

# PlantUML uses a custom base64 alphabet for URL encoding
_PLANTUML_ALPHABET = (
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-_"
)


def _plantuml_encode(text: str) -> str:
    """Encode PlantUML source into the URL-safe text format used by plantuml.com."""
    compressed = zlib.compress(text.encode("utf-8"))[2:-4]  # raw deflate (strip zlib header/checksum)
    encoded = []
    for i in range(0, len(compressed), 3):
        chunk = compressed[i:i + 3]
        if len(chunk) == 3:
            b0, b1, b2 = chunk
            encoded.append(_PLANTUML_ALPHABET[b0 >> 2])
            encoded.append(_PLANTUML_ALPHABET[((b0 & 0x3) << 4) | (b1 >> 4)])
            encoded.append(_PLANTUML_ALPHABET[((b1 & 0xF) << 2) | (b2 >> 6)])
            encoded.append(_PLANTUML_ALPHABET[b2 & 0x3F])
        elif len(chunk) == 2:
            b0, b1 = chunk
            encoded.append(_PLANTUML_ALPHABET[b0 >> 2])
            encoded.append(_PLANTUML_ALPHABET[((b0 & 0x3) << 4) | (b1 >> 4)])
            encoded.append(_PLANTUML_ALPHABET[(b1 & 0xF) << 2])
        elif len(chunk) == 1:
            b0 = chunk[0]
            encoded.append(_PLANTUML_ALPHABET[b0 >> 2])
            encoded.append(_PLANTUML_ALPHABET[(b0 & 0x3) << 4])
    return "".join(encoded)


def _post_plantuml_comments(
    project_root: Path,
    file_paths: list[str],
) -> list[dict[str, Any]]:
    """
    For any .puml files in file_paths, render via plantuml.com and post
    the image as a PR comment on the current branch's PR.

    Returns a list of results (one per .puml file posted).
    """
    puml_files = [f for f in file_paths if f.endswith(".puml")]
    if not puml_files:
        return []

    results = []
    for puml_path in puml_files:
        full_path = project_root / puml_path
        if not full_path.exists():
            results.append({"file": puml_path, "error": "file not found"})
            continue

        source = full_path.read_text(encoding="utf-8")
        encoded = _plantuml_encode(source)
        image_url = f"https://www.plantuml.com/plantuml/svg/{encoded}"

        # Build comment body
        filename = Path(puml_path).name
        comment_body = (
            f"### {filename}\n\n"
            f"![{filename}]({image_url})\n\n"
            f"<details><summary>PlantUML source</summary>\n\n"
            f"```plantuml\n{source}\n```\n\n"
            f"</details>"
        )

        post_result = post_pr_comment(project_root, comment_body)
        results.append({"file": puml_path, **post_result})

    return results


# ---------------------------------------------------------------------------
# Commit message prefixes
# ---------------------------------------------------------------------------

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

    # Check if there are staged changes to commit
    diff_result = _run_git(project_root, ["diff", "--cached", "--quiet"])
    has_staged = diff_result.returncode != 0  # exit 1 = diffs exist

    if has_staged:
        # Commit
        commit_result = _run_git(project_root, ["commit", "-m", message])
        if commit_result.returncode != 0:
            return {"error": f"git commit failed: {commit_result.stderr.strip()}"}
        committed = True
    else:
        committed = False

    # Get commit SHA and branch regardless of whether we just committed
    sha_result = _run_git(project_root, ["rev-parse", "HEAD"])
    commit_sha = sha_result.stdout.strip()

    branch_result = _run_git(project_root, ["branch", "--show-current"])
    branch_name = branch_result.stdout.strip()

    # Check if HEAD is already pushed (local vs remote)
    upstream_result = _run_git(
        project_root, ["rev-parse", "--abbrev-ref", f"{branch_name}@{{upstream}}"]
    )
    has_upstream = upstream_result.returncode == 0

    needs_push = True
    if has_upstream:
        remote_sha = _run_git(
            project_root, ["rev-parse", f"{branch_name}@{{upstream}}"]
        )
        if remote_sha.returncode == 0 and remote_sha.stdout.strip() == commit_sha:
            needs_push = False

    if needs_push:
        if has_upstream:
            push_result = _run_git(project_root, ["push"])
        else:
            push_result = _run_git(project_root, ["push", "-u", "origin", branch_name])

        if push_result.returncode != 0:
            # Report push failure but include commit info so caller knows state
            return {
                "error": f"git push failed: {push_result.stderr.strip()}",
                "commit_sha": commit_sha,
                "committed": committed,
                "pushed": False,
                "branch_name": branch_name,
                "files_staged": file_paths,
            }

    # Post PlantUML diagrams as PR comments after successful push
    puml_results = []
    if needs_push:
        puml_results = _post_plantuml_comments(project_root, file_paths)

    result = {
        "commit_sha": commit_sha,
        "committed": committed,
        "pushed": needs_push,
        "branch_name": branch_name,
        "files_staged": file_paths,
    }
    if puml_results:
        result["plantuml_comments"] = puml_results
    return result


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

    # Ensure labels exist (idempotent — gh label create is a no-op if it exists)
    labels = [f"ticket:{ticket_id}", f"phase:{row['phase_name']}"]
    valid_labels = []
    for label in labels:
        create_label = _run_gh(
            project_root,
            ["label", "create", label, "--force", "--description", ""],
        )
        if create_label.returncode == 0:
            valid_labels.append(label)

    # Build gh pr create args
    create_args = [
        "pr", "create",
        "--title", title,
        "--body", body,
    ]
    for label in valid_labels:
        create_args.extend(["--label", label])
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


# Claude attribution signature appended to agent-authored comments
CLAUDE_SIGNATURE = "\n\n---\n*🤖 — Claude*"


def _find_pr_for_branch(
    project_root: Path,
) -> dict[str, Any]:
    """
    Find the PR number for the current branch.

    Returns:
        {"pr_number": int, "branch_name": str} on success
        {"error": str} on failure
    """
    branch_result = _run_git(project_root, ["branch", "--show-current"])
    branch_name = branch_result.stdout.strip()

    if not branch_name:
        return {"error": "Could not determine current branch"}

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

    return {"pr_number": pr_number, "branch_name": branch_name}


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
    pr_info = _find_pr_for_branch(project_root)
    if "error" in pr_info:
        return pr_info

    pr_number = pr_info["pr_number"]

    comment_result = _run_gh(
        project_root, ["pr", "comment", str(pr_number), "--body", body + CLAUDE_SIGNATURE]
    )

    if comment_result.returncode != 0:
        return {"error": f"gh pr comment failed: {comment_result.stderr.strip()}"}

    return {
        "pr_number": pr_number,
        "commented": True,
    }


def reply_to_review_comment(
    project_root: Path,
    comment_id: int,
    body: str,
) -> dict[str, Any]:
    """
    Reply to an inline review comment on the current branch's PR.

    Uses the GitHub API to create a threaded reply on a specific
    review comment, identified by comment_id (from get_pr_reviews).

    Returns:
        {pr_number, comment_id, replied} on success
        {error: str} on failure
    """
    pr_info = _find_pr_for_branch(project_root)
    if "error" in pr_info:
        return pr_info

    pr_number = pr_info["pr_number"]
    signed_body = body + CLAUDE_SIGNATURE

    # GitHub API: POST /repos/{owner}/{repo}/pulls/{pr}/comments/{comment_id}/replies
    reply_result = _run_gh(
        project_root,
        [
            "api",
            f"repos/{{owner}}/{{repo}}/pulls/{pr_number}/comments/{comment_id}/replies",
            "-f", f"body={signed_body}",
        ],
    )

    if reply_result.returncode != 0:
        return {"error": f"Reply failed: {reply_result.stderr.strip()}"}

    return {
        "pr_number": pr_number,
        "comment_id": comment_id,
        "replied": True,
    }


def get_pr_reviews(
    project_root: Path,
) -> dict[str, Any]:
    """
    Fetch review comments and PR comments from the current branch's PR.

    Returns:
        {pr_number, reviews: [...], comments: [...], review_comments: [...]} on success
        {error: str} on failure

    Each review_comment includes an 'id' field that can be passed to
    reply_to_review_comment() to create a threaded reply.
    """
    pr_info = _find_pr_for_branch(project_root)
    if "error" in pr_info:
        return pr_info

    pr_number = pr_info["pr_number"]

    # Fetch PR reviews (approve/request changes/comment)
    reviews_result = _run_gh(
        project_root,
        [
            "pr", "view", str(pr_number),
            "--json", "reviews",
            "--jq", '.reviews[] | {author: .author.login, state: .state, body: .body, submittedAt: .submittedAt}',
        ],
    )

    reviews = []
    if reviews_result.returncode == 0 and reviews_result.stdout.strip():
        for line in reviews_result.stdout.strip().split("\n"):
            line = line.strip()
            if line:
                try:
                    reviews.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    # Fetch PR comments (conversation comments, not inline review comments)
    comments_result = _run_gh(
        project_root,
        [
            "pr", "view", str(pr_number),
            "--json", "comments",
            "--jq", '.comments[] | {author: .author.login, body: .body, createdAt: .createdAt}',
        ],
    )

    comments = []
    if comments_result.returncode == 0 and comments_result.stdout.strip():
        for line in comments_result.stdout.strip().split("\n"):
            line = line.strip()
            if line:
                try:
                    comments.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    # Fetch inline review comments (code-level comments)
    review_comments_result = _run_gh(
        project_root,
        [
            "api", f"repos/{{owner}}/{{repo}}/pulls/{pr_number}/comments",
            "--jq", '.[] | {id: .id, author: .user.login, body: .body, path: .path, line: .line, createdAt: .created_at}',
        ],
    )

    review_comments = []
    if review_comments_result.returncode == 0 and review_comments_result.stdout.strip():
        for line in review_comments_result.stdout.strip().split("\n"):
            line = line.strip()
            if line:
                try:
                    review_comments.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    return {
        "pr_number": pr_number,
        "reviews": reviews,
        "comments": comments,
        "review_comments": review_comments,
    }
