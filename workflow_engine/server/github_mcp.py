"""
Git/GitHub MCP tool registrations.

Called from ``create_mcp_server()`` to register all git and PR-related tools.
"""

import json
from pathlib import Path
from typing import TYPE_CHECKING

from workflow_engine.engine import github as github_mod
from workflow_engine.engine import scheduler as sched_mod
from workflow_engine.traceability.reindex import reindex_all as trace_reindex_all

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from workflow_engine.server.server import WorkflowServer


def register_github_tools(mcp: "FastMCP", ws: "WorkflowServer") -> None:
    """Register all Git/GitHub MCP tools on *mcp* using *ws* state."""

    @mcp.tool()
    def get_pr_reviews() -> str:
        """
        Fetch all reviews and comments from the current branch's PR.

        Returns PR reviews (approve/request changes), conversation comments,
        and inline code review comments. Use this to check feedback before
        approving or requesting revisions on a gate.
        """
        return json.dumps(
            github_mod.get_pr_reviews(ws.project_root), indent=2, default=str
        )

    @mcp.tool()
    def reply_to_review_comment(
        comment_id: int,
        body: str,
    ) -> str:
        """
        Reply to an inline review comment on the current branch's PR.

        The reply is posted as a threaded response to the specific review
        comment, attributed to Claude. Use get_pr_reviews to find comment IDs.

        Args:
            comment_id: The review comment ID to reply to (from get_pr_reviews review_comments[].id)
            body: The reply text (Claude signature is appended automatically)
        """
        return json.dumps(
            github_mod.reply_to_review_comment(ws.project_root, comment_id, body),
            indent=2,
        )

    @mcp.tool()
    def request_gate_revisions(
        gate_id: int,
        decided_by: str,
        notes: str | None = None,
    ) -> str:
        """
        Request revisions on a human review gate, pulling PR comments as context.

        Fetches all reviews and comments from the current branch's PR,
        combines them with any additional notes, and marks the gate as
        changes_requested. The associated phase remains blocked.

        Args:
            gate_id: The gate ID (from list_blocked or get_ticket_status)
            decided_by: Name of the human reviewer requesting revisions
            notes: Optional additional notes beyond the PR comments
        """
        # Fetch PR reviews
        pr_data = github_mod.get_pr_reviews(ws.project_root)

        # Build revision notes from PR feedback
        feedback_parts = []
        if notes:
            feedback_parts.append(notes)

        if "error" not in pr_data:
            for review in pr_data.get("reviews", []):
                if review.get("body"):
                    feedback_parts.append(
                        f"[Review by {review['author']} ({review['state']})]: {review['body']}"
                    )
            for comment in pr_data.get("comments", []):
                if comment.get("body"):
                    feedback_parts.append(
                        f"[Comment by {comment['author']}]: {comment['body']}"
                    )
            for rc in pr_data.get("review_comments", []):
                if rc.get("body"):
                    location = f"{rc.get('path', '?')}:{rc.get('line', '?')}"
                    feedback_parts.append(
                        f"[Inline at {location} by {rc['author']}]: {rc['body']}"
                    )

        combined_notes = "\n\n".join(feedback_parts) if feedback_parts else None

        result = sched_mod.resolve_gate(
            ws.conn, gate_id, "changes_requested", decided_by, combined_notes
        )
        result["pr_number"] = pr_data.get("pr_number")
        result["feedback_items"] = len(feedback_parts)
        return json.dumps(result, indent=2)

    @mcp.tool()
    def setup_branch(ticket_id: str) -> str:
        """
        Create or check out a git branch for a ticket.

        Derives the branch name from the ticket's full_name (underscores become
        hyphens). If the branch already exists, checks it out. Otherwise creates
        it from main.

        Args:
            ticket_id: Ticket ID (e.g. '0041')
        """
        return json.dumps(
            github_mod.setup_branch(ws.project_root, ticket_id, ws.conn), indent=2
        )

    @mcp.tool()
    def commit_and_push(
        agent_id: str,
        phase_id: int,
        file_paths: str,
        message: str | None = None,
    ) -> str:
        """
        Stage files, commit, and push to remote.

        If no message is provided, auto-generates one from the phase and ticket
        info (e.g. 'impl: feature name').

        Args:
            agent_id: Your agent ID
            phase_id: Your claimed phase ID
            file_paths: JSON array of file paths to stage (e.g. '["src/foo.cpp"]')
            message: Optional commit message (auto-generated if omitted)
        """
        paths = json.loads(file_paths)
        result = github_mod.commit_and_push(
            ws.project_root, ws.conn, agent_id, phase_id, paths, message
        )

        # Trigger incremental traceability reindex after successful commit
        if ws.trace_server is not None and result.get("status") != "error":
            try:
                ws._require_trace()
                trace_result = trace_reindex_all(
                    ws.trace_conn,
                    str(ws.project_root),
                    source_dir=ws.config.traceability_source_dir,
                    designs_dir=ws.config.traceability_designs_dir,
                    tickets_dir=str(Path(ws.config.tickets_directory).relative_to(ws.project_root))
                    if Path(ws.config.tickets_directory).is_absolute()
                    else ws.config.tickets_directory,
                    models_path=ws.config.traceability_models_path,
                    generated_models_path=ws.config.traceability_generated_models_path,
                    skip_symbols=True,
                    coverage_info_path=ws.config.traceability_coverage_info_path,
                )
                result["traceability_reindex"] = trace_result
            except Exception as e:
                result["traceability_reindex_error"] = str(e)

        return json.dumps(result, indent=2)

    @mcp.tool()
    def create_or_update_pr(
        agent_id: str,
        phase_id: int,
        title: str | None = None,
        body: str | None = None,
        draft: bool = True,
    ) -> str:
        """
        Create a new PR or update an existing one for the current branch.

        If a PR already exists, returns its info. If draft=False and a draft PR
        exists, marks it as ready for review. Auto-generates title and body from
        ticket/phase info if not provided.

        Args:
            agent_id: Your agent ID
            phase_id: Your claimed phase ID
            title: Optional PR title (auto-generated if omitted)
            body: Optional PR body (auto-generated if omitted)
            draft: Whether to create as draft PR (default: True)
        """
        return json.dumps(
            github_mod.create_or_update_pr(
                ws.project_root, ws.conn, agent_id, phase_id, title, body, draft
            ),
            indent=2,
        )

    @mcp.tool()
    def post_pr_comment(body: str) -> str:
        """
        Post a comment on the PR associated with the current branch.

        Finds the PR for the current branch and posts the given comment body.

        Args:
            body: The comment text to post
        """
        return json.dumps(
            github_mod.post_pr_comment(ws.project_root, body), indent=2
        )
