"""
Tests for workflow_engine/traceability/reindex.py

Ticket: 0085_traceability_workflow_consolidation
Design: docs/designs/0085_traceability_workflow_consolidation/design.md

Validates:
- reindex_all() returns results dict with keys for each indexer
- reindex_all(skip_symbols=True) skips symbol indexing
- reindex_all() is callable without error on a minimal repo structure
- reindex_all() with no-op indexers returns expected shape
"""

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from workflow_engine.traceability.schema import create_schema
from workflow_engine.traceability.reindex import reindex_all


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "traceability.db")


@pytest.fixture
def conn(db_path):
    c = create_schema(db_path)
    yield c
    c.close()


@pytest.fixture
def minimal_repo(tmp_path):
    """Create a minimal repo-like directory structure."""
    (tmp_path / "docs" / "designs").mkdir(parents=True)
    (tmp_path / "tickets").mkdir()
    (tmp_path / "msd").mkdir()
    return tmp_path


# ---------------------------------------------------------------------------
# reindex_all tests
# ---------------------------------------------------------------------------


def test_reindex_all_returns_dict_with_all_keys(conn, minimal_repo):
    """reindex_all() should always return a dict with git, decisions, symbols, records."""
    with (
        patch("workflow_engine.traceability.reindex.index_git_history", return_value={"new_count": 0, "total": 0}),
        patch("workflow_engine.traceability.reindex.index_decisions", return_value={"new_count": 0, "total": 0, "symbol_links": 0}),
        patch("workflow_engine.traceability.reindex.index_symbols", return_value={"snapshots": 0}),
        patch("workflow_engine.traceability.reindex.index_records", return_value={"composite_count": 0, "generated_count": 0}),
    ):
        result = reindex_all(conn, str(minimal_repo))

    assert "git" in result
    assert "decisions" in result
    assert "symbols" in result
    assert "records" in result


def test_reindex_all_skip_symbols_returns_skipped_marker(conn, minimal_repo):
    """reindex_all(skip_symbols=True) should mark symbols as skipped."""
    with (
        patch("workflow_engine.traceability.reindex.index_git_history", return_value={"new_count": 0, "total": 0}),
        patch("workflow_engine.traceability.reindex.index_decisions", return_value={"new_count": 0, "total": 0, "symbol_links": 0}),
        patch("workflow_engine.traceability.reindex.index_records", return_value={"composite_count": 0, "generated_count": 0}),
    ):
        result = reindex_all(conn, str(minimal_repo), skip_symbols=True)

    assert result["symbols"] == {"skipped": True}


def test_reindex_all_calls_index_git_with_repo_root(conn, minimal_repo):
    """reindex_all() should call index_git_history with the provided repo_root."""
    with (
        patch("workflow_engine.traceability.reindex.index_git_history", return_value={"new_count": 0, "total": 0}) as mock_git,
        patch("workflow_engine.traceability.reindex.index_decisions", return_value={"new_count": 0, "total": 0, "symbol_links": 0}),
        patch("workflow_engine.traceability.reindex.index_symbols", return_value={}),
        patch("workflow_engine.traceability.reindex.index_records", return_value={"composite_count": 0, "generated_count": 0}),
    ):
        reindex_all(conn, str(minimal_repo))

    mock_git.assert_called_once_with(conn, str(minimal_repo))


def test_reindex_all_calls_index_decisions_with_correct_dirs(conn, minimal_repo):
    """reindex_all() should pass designs_dir and tickets_dir to index_decisions."""
    with (
        patch("workflow_engine.traceability.reindex.index_git_history", return_value={"new_count": 0, "total": 0}),
        patch("workflow_engine.traceability.reindex.index_decisions", return_value={"new_count": 0, "total": 0, "symbol_links": 0}) as mock_dec,
        patch("workflow_engine.traceability.reindex.index_symbols", return_value={}),
        patch("workflow_engine.traceability.reindex.index_records", return_value={"composite_count": 0, "generated_count": 0}),
    ):
        reindex_all(
            conn,
            str(minimal_repo),
            designs_dir="custom/designs",
            tickets_dir="custom/tickets",
        )

    mock_dec.assert_called_once_with(
        conn,
        str(minimal_repo),
        designs_dir="custom/designs",
        tickets_dir="custom/tickets",
    )


def test_reindex_all_calls_index_symbols_when_not_skipped(conn, minimal_repo):
    """reindex_all() should call index_symbols with source_dir when not skipped."""
    with (
        patch("workflow_engine.traceability.reindex.index_git_history", return_value={"new_count": 0, "total": 0}),
        patch("workflow_engine.traceability.reindex.index_decisions", return_value={"new_count": 0, "total": 0, "symbol_links": 0}),
        patch("workflow_engine.traceability.reindex.index_symbols", return_value={"snapshots": 1}) as mock_sym,
        patch("workflow_engine.traceability.reindex.index_records", return_value={"composite_count": 0, "generated_count": 0}),
    ):
        reindex_all(conn, str(minimal_repo), source_dir="custom/src")

    mock_sym.assert_called_once_with(conn, str(minimal_repo), source_dir="custom/src")


def test_reindex_all_does_not_call_index_symbols_when_skipped(conn, minimal_repo):
    """reindex_all(skip_symbols=True) should not call index_symbols."""
    with (
        patch("workflow_engine.traceability.reindex.index_git_history", return_value={"new_count": 0, "total": 0}),
        patch("workflow_engine.traceability.reindex.index_decisions", return_value={"new_count": 0, "total": 0, "symbol_links": 0}),
        patch("workflow_engine.traceability.reindex.index_symbols") as mock_sym,
        patch("workflow_engine.traceability.reindex.index_records", return_value={"composite_count": 0, "generated_count": 0}),
    ):
        reindex_all(conn, str(minimal_repo), skip_symbols=True)

    mock_sym.assert_not_called()


def test_reindex_all_calls_index_records_with_model_paths(conn, minimal_repo):
    """reindex_all() should pass models_path and generated_models_path to index_records."""
    with (
        patch("workflow_engine.traceability.reindex.index_git_history", return_value={"new_count": 0, "total": 0}),
        patch("workflow_engine.traceability.reindex.index_decisions", return_value={"new_count": 0, "total": 0, "symbol_links": 0}),
        patch("workflow_engine.traceability.reindex.index_symbols", return_value={}),
        patch("workflow_engine.traceability.reindex.index_records", return_value={"composite_count": 0, "generated_count": 0}) as mock_rec,
    ):
        reindex_all(
            conn,
            str(minimal_repo),
            models_path="custom/models.py",
            generated_models_path="custom/gen.py",
        )

    mock_rec.assert_called_once_with(
        conn,
        str(minimal_repo),
        models_path="custom/models.py",
        generated_models_path="custom/gen.py",
    )


def test_reindex_all_default_source_dir(conn, minimal_repo):
    """reindex_all() default source_dir is 'msd'."""
    with (
        patch("workflow_engine.traceability.reindex.index_git_history", return_value={"new_count": 0, "total": 0}),
        patch("workflow_engine.traceability.reindex.index_decisions", return_value={"new_count": 0, "total": 0, "symbol_links": 0}),
        patch("workflow_engine.traceability.reindex.index_symbols", return_value={}) as mock_sym,
        patch("workflow_engine.traceability.reindex.index_records", return_value={"composite_count": 0, "generated_count": 0}),
    ):
        reindex_all(conn, str(minimal_repo))

    mock_sym.assert_called_once_with(conn, str(minimal_repo), source_dir="msd")


def test_reindex_all_result_values_from_indexers(conn, minimal_repo):
    """reindex_all() should propagate indexer return values into the result dict."""
    with (
        patch("workflow_engine.traceability.reindex.index_git_history", return_value={"new_count": 5, "total": 100}),
        patch("workflow_engine.traceability.reindex.index_decisions", return_value={"new_count": 3, "total": 50, "symbol_links": 10}),
        patch("workflow_engine.traceability.reindex.index_symbols", return_value={"snapshots": 7}),
        patch("workflow_engine.traceability.reindex.index_records", return_value={"composite_count": 4, "generated_count": 20}),
    ):
        result = reindex_all(conn, str(minimal_repo))

    assert result["git"]["new_count"] == 5
    assert result["git"]["total"] == 100
    assert result["decisions"]["new_count"] == 3
    assert result["symbols"]["snapshots"] == 7
    assert result["records"]["composite_count"] == 4
