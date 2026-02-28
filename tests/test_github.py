"""
Tests for engine/github.py

Validates git/GitHub operations with mocked subprocess calls:
- _derive_branch_name helper
- _feature_name_from_full_name helper
- setup_branch (new branch vs existing)
- commit_and_push (explicit message vs auto-generated)
- create_or_update_pr (new PR vs existing)
- post_pr_comment (happy path and PR not found)
"""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from workflow_engine.engine.github import (
    PHASE_PREFIX_MAP,
    _derive_branch_name,
    _feature_name_from_full_name,
    setup_branch,
    commit_and_push,
    create_or_update_pr,
    post_pr_comment,
)
from workflow_engine.engine.schema import create_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_conn(tmp_path):
    """Open a fresh database with schema."""
    conn = create_db(str(tmp_path / "test.db"))
    yield conn
    conn.close()


@pytest.fixture
def project_root(tmp_path):
    """Provide a temporary project root path."""
    return tmp_path / "project"


def _insert_ticket(conn, ticket_id="0041", full_name="0041_feature_name"):
    conn.execute(
        "INSERT OR IGNORE INTO tickets (id, name, full_name, current_status, markdown_path, priority, languages) "
        "VALUES (?, ?, ?, 'Draft', ?, 'High', 'C++')",
        (ticket_id, f"test_{ticket_id}", full_name, f"tickets/{full_name}.md"),
    )
    conn.commit()


def _insert_phase(conn, ticket_id, phase_name, agent_type="cpp-implementer"):
    conn.execute(
        "INSERT INTO phases (ticket_id, phase_name, phase_order, status, agent_type) "
        "VALUES (?, ?, 0, 'running', ?)",
        (ticket_id, phase_name, agent_type),
    )
    conn.commit()
    return conn.execute(
        "SELECT id FROM phases WHERE ticket_id=? AND phase_name=?",
        (ticket_id, phase_name),
    ).fetchone()["id"]


def _mock_run(returncode=0, stdout="", stderr=""):
    """Create a mock CompletedProcess."""
    result = MagicMock(spec=subprocess.CompletedProcess)
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


# ---------------------------------------------------------------------------
# _derive_branch_name tests
# ---------------------------------------------------------------------------


class TestDeriveBranchName:
    def test_simple(self):
        assert _derive_branch_name("0041_feature_name") == "0041-feature-name"

    def test_with_letter_suffix(self):
        assert _derive_branch_name("0083a_workflow_engine") == "0083a-workflow-engine"

    def test_single_word(self):
        assert _derive_branch_name("0001_setup") == "0001-setup"

    def test_multiple_underscores(self):
        assert _derive_branch_name("0010_a_b_c_d") == "0010-a-b-c-d"


# ---------------------------------------------------------------------------
# _feature_name_from_full_name tests
# ---------------------------------------------------------------------------


class TestFeatureNameFromFullName:
    def test_simple(self):
        assert _feature_name_from_full_name("0041_feature_name") == "feature name"

    def test_with_letter_suffix(self):
        assert _feature_name_from_full_name("0083a_workflow_engine") == "workflow engine"

    def test_long_name(self):
        assert (
            _feature_name_from_full_name("0015_profiling_trace_parser")
            == "profiling trace parser"
        )


# ---------------------------------------------------------------------------
# setup_branch tests
# ---------------------------------------------------------------------------


class TestSetupBranch:
    @patch("workflow_engine.engine.github._run_git")
    def test_creates_new_branch(self, mock_git, db_conn, project_root):
        _insert_ticket(db_conn, "0041", "0041_feature_name")

        # branch --list returns empty (branch doesn't exist)
        # checkout -b succeeds
        mock_git.side_effect = [
            _mock_run(stdout=""),  # git branch --list
            _mock_run(),  # git checkout -b
        ]

        result = setup_branch(project_root, "0041", db_conn)

        assert result["branch_name"] == "0041-feature-name"
        assert result["created"] is True
        assert result["checked_out"] is True

        # Verify correct git commands
        calls = mock_git.call_args_list
        assert calls[0] == call(project_root, ["branch", "--list", "0041-feature-name"])
        assert calls[1] == call(
            project_root, ["checkout", "-b", "0041-feature-name", "main"]
        )

    @patch("workflow_engine.engine.github._run_git")
    def test_checks_out_existing_branch(self, mock_git, db_conn, project_root):
        _insert_ticket(db_conn, "0041", "0041_feature_name")

        mock_git.side_effect = [
            _mock_run(stdout="  0041-feature-name\n"),  # branch exists
            _mock_run(),  # git checkout
        ]

        result = setup_branch(project_root, "0041", db_conn)

        assert result["branch_name"] == "0041-feature-name"
        assert result["created"] is False
        assert result["checked_out"] is True

        calls = mock_git.call_args_list
        assert calls[1] == call(project_root, ["checkout", "0041-feature-name"])

    def test_ticket_not_found(self, db_conn, project_root):
        result = setup_branch(project_root, "9999", db_conn)
        assert "error" in result
        assert "not found" in result["error"]


# ---------------------------------------------------------------------------
# commit_and_push tests
# ---------------------------------------------------------------------------


class TestCommitAndPush:
    @patch("workflow_engine.engine.github._run_git")
    def test_with_explicit_message(self, mock_git, db_conn, project_root):
        _insert_ticket(db_conn, "0041", "0041_feature_name")
        phase_id = _insert_phase(db_conn, "0041", "C++ Implementation")

        mock_git.side_effect = [
            _mock_run(),  # git add
            _mock_run(),  # git commit
            _mock_run(stdout="0041-feature-name\n"),  # git branch --show-current
            _mock_run(),  # git rev-parse upstream (success = has upstream)
            _mock_run(),  # git push
            _mock_run(stdout="abc123def\n"),  # git rev-parse HEAD
        ]

        result = commit_and_push(
            project_root,
            db_conn,
            "agent-001",
            phase_id,
            ["src/foo.cpp", "src/foo.h"],
            message="custom: my message",
        )

        assert result["commit_sha"] == "abc123def"
        assert result["pushed"] is True
        assert result["branch_name"] == "0041-feature-name"
        assert result["files_staged"] == ["src/foo.cpp", "src/foo.h"]

    @patch("workflow_engine.engine.github._run_git")
    def test_auto_generated_message(self, mock_git, db_conn, project_root):
        _insert_ticket(db_conn, "0041", "0041_feature_name")
        phase_id = _insert_phase(db_conn, "0041", "C++ Implementation")

        mock_git.side_effect = [
            _mock_run(),  # git add
            _mock_run(),  # git commit
            _mock_run(stdout="0041-feature-name\n"),  # branch
            _mock_run(returncode=1),  # upstream check fails (no upstream)
            _mock_run(),  # git push -u origin
            _mock_run(stdout="abc123\n"),  # rev-parse HEAD
        ]

        result = commit_and_push(
            project_root, db_conn, "agent-001", phase_id, ["src/foo.cpp"]
        )

        assert result["pushed"] is True

        # Verify auto-generated commit message was passed
        commit_call = mock_git.call_args_list[1]
        commit_msg = commit_call[0][1][2]  # args[1] = ["commit", "-m", message]
        assert commit_msg.startswith("impl: feature name")
        assert "Co-Authored-By:" in commit_msg

        # Verify push -u was used (no upstream)
        push_call = mock_git.call_args_list[4]
        assert push_call == call(
            project_root, ["push", "-u", "origin", "0041-feature-name"]
        )

    @patch("workflow_engine.engine.github._run_git")
    def test_git_add_failure(self, mock_git, db_conn, project_root):
        _insert_ticket(db_conn, "0041", "0041_feature_name")
        phase_id = _insert_phase(db_conn, "0041", "C++ Implementation")

        mock_git.return_value = _mock_run(returncode=1, stderr="pathspec error")

        result = commit_and_push(
            project_root, db_conn, "agent-001", phase_id, ["nonexistent.cpp"]
        )

        assert "error" in result
        assert "git add failed" in result["error"]


# ---------------------------------------------------------------------------
# create_or_update_pr tests
# ---------------------------------------------------------------------------


class TestCreateOrUpdatePr:
    @patch("workflow_engine.engine.github._run_gh")
    @patch("workflow_engine.engine.github._run_git")
    def test_creates_new_pr(self, mock_git, mock_gh, db_conn, project_root):
        _insert_ticket(db_conn, "0041", "0041_feature_name")
        phase_id = _insert_phase(db_conn, "0041", "C++ Implementation")

        mock_git.return_value = _mock_run(stdout="0041-feature-name\n")

        mock_gh.side_effect = [
            _mock_run(stdout=""),  # no existing PR
            _mock_run(stdout="https://github.com/org/repo/pull/42\n"),  # pr create
        ]

        result = create_or_update_pr(
            project_root, db_conn, "agent-001", phase_id
        )

        assert result["action"] == "created"
        assert result["pr_number"] == 42
        assert "pull/42" in result["pr_url"]

    @patch("workflow_engine.engine.github._run_gh")
    @patch("workflow_engine.engine.github._run_git")
    def test_existing_pr_returns_info(self, mock_git, mock_gh, db_conn, project_root):
        _insert_ticket(db_conn, "0041", "0041_feature_name")
        phase_id = _insert_phase(db_conn, "0041", "C++ Implementation")

        mock_git.return_value = _mock_run(stdout="0041-feature-name\n")

        mock_gh.return_value = _mock_run(
            stdout=json.dumps({"number": 42, "url": "https://github.com/org/repo/pull/42"})
        )

        result = create_or_update_pr(
            project_root, db_conn, "agent-001", phase_id
        )

        assert result["action"] == "already_exists"
        assert result["pr_number"] == 42

    @patch("workflow_engine.engine.github._run_gh")
    @patch("workflow_engine.engine.github._run_git")
    def test_marks_draft_ready(self, mock_git, mock_gh, db_conn, project_root):
        _insert_ticket(db_conn, "0041", "0041_feature_name")
        phase_id = _insert_phase(db_conn, "0041", "C++ Implementation")

        mock_git.return_value = _mock_run(stdout="0041-feature-name\n")

        mock_gh.side_effect = [
            _mock_run(
                stdout=json.dumps({"number": 42, "url": "https://github.com/org/repo/pull/42"})
            ),  # existing PR
            _mock_run(),  # gh pr ready
        ]

        result = create_or_update_pr(
            project_root, db_conn, "agent-001", phase_id, draft=False
        )

        assert result["action"] == "marked_ready"
        assert result["pr_number"] == 42


# ---------------------------------------------------------------------------
# post_pr_comment tests
# ---------------------------------------------------------------------------


class TestPostPrComment:
    @patch("workflow_engine.engine.github._run_gh")
    @patch("workflow_engine.engine.github._run_git")
    def test_happy_path(self, mock_git, mock_gh, project_root):
        mock_git.return_value = _mock_run(stdout="0041-feature-name\n")

        mock_gh.side_effect = [
            _mock_run(stdout="42\n"),  # pr list --jq
            _mock_run(),  # pr comment
        ]

        result = post_pr_comment(project_root, "Phase complete!")

        assert result["pr_number"] == 42
        assert result["commented"] is True

        # Verify comment body was passed
        comment_call = mock_gh.call_args_list[1]
        assert comment_call == call(
            project_root, ["pr", "comment", "42", "--body", "Phase complete!"]
        )

    @patch("workflow_engine.engine.github._run_gh")
    @patch("workflow_engine.engine.github._run_git")
    def test_no_pr_found(self, mock_git, mock_gh, project_root):
        mock_git.return_value = _mock_run(stdout="some-branch\n")
        mock_gh.return_value = _mock_run(stdout="")  # no PR

        result = post_pr_comment(project_root, "comment")

        assert "error" in result
        assert "No PR found" in result["error"]
