"""
Workflow Engine — SQLite-backed multi-agent work queue.

A project-agnostic engine for concurrent multi-agent ticket orchestration.
All project-specific configuration (phase definitions, agent types, timeouts)
lives in the consuming repository's .workflow/ directory.

Quickstart:
    pip install git+https://github.com/danielnewman09/workflow-engine.git

    # Start MCP server (stdio mode, used by Claude Code):
    python -m workflow_engine.server build/workflow.db --project-root .

    # Human CLI:
    workflow-engine gates
    workflow-engine approve <gate-id>
"""

__version__ = "0.1.0"
