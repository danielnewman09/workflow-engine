#!/usr/bin/env python3
# Ticket: 0083_database_agent_orchestration
# Design: docs/designs/0083_database_agent_orchestration/design.md
"""
Workflow Engine Configuration Reader

Reads project-specific configuration from the consuming repository's
.workflow/ directory:
- .workflow/phases.yaml  — phase definitions, agent type mappings, conditions
- .workflow/config.yaml  — timeouts, priority rules, stale thresholds

The engine has sensible defaults for all settings. Both files are optional.
All project-specific knowledge (phase names, agent types, ticket metadata
fields) comes from these files — the engine contains no MSD-CPP-specific logic.
"""

import re
from pathlib import Path
from typing import Any

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

from .models import (
    PhaseCondition,
    PhaseDefinition,
    WorkflowConfig,
)


# ---------------------------------------------------------------------------
# Default phases.yaml (used when no .workflow/phases.yaml exists)
# ---------------------------------------------------------------------------

DEFAULT_PHASES_YAML = """
phases:
  - name: "Design"
    agent_type: "designer"
  - name: "Design Review"
    agent_type: null
  - name: "Implementation"
    agent_type: "implementer"
  - name: "Test Writing"
    agent_type: "test-writer"
  - name: "Review"
    agent_type: "reviewer"
  - name: "Documentation"
    agent_type: "docs-updater"

ticket_metadata:
  - field: "priority"
    type: "enum"
    values: ["Low", "Medium", "High", "Critical"]
    markdown_key: "Priority"
  - field: "languages"
    type: "list"
    default: ["C++"]
    markdown_key: "Languages"
"""


# ---------------------------------------------------------------------------
# Condition parser
# ---------------------------------------------------------------------------


def _parse_condition(cond_dict: dict[str, Any] | None) -> PhaseCondition | None:
    """Parse a condition dict from phases.yaml into a PhaseCondition."""
    if not cond_dict:
        return None
    return PhaseCondition(
        field=cond_dict["field"],
        value=cond_dict.get("value"),
        contains=cond_dict.get("contains"),
        has_multiple=bool(cond_dict.get("has_multiple", False)),
    )


# ---------------------------------------------------------------------------
# phases.yaml loader
# ---------------------------------------------------------------------------


def load_phase_definitions(phases_yaml: dict[str, Any]) -> list[PhaseDefinition]:
    """
    Parse a phases.yaml document into a list of PhaseDefinition objects.

    Handles:
    - Top-level phases list (sequential)
    - parallel_groups section (phases that run concurrently)

    All phases receive an `order` field that controls DB phase_order, used
    by the scheduler to determine availability sequence.
    """
    definitions: list[PhaseDefinition] = []
    order = 0

    # Sequential phases
    for phase_dict in phases_yaml.get("phases", []):
        definitions.append(
            PhaseDefinition(
                name=phase_dict["name"],
                agent_type=phase_dict.get("agent_type"),
                condition=_parse_condition(phase_dict.get("condition")),
                parallel_group=None,
                order=order,
            )
        )
        order += 1

    # Parallel groups — each member gets the same order slot as a group marker
    for group_name, group_dict in phases_yaml.get("parallel_groups", {}).items():
        for phase_dict in group_dict.get("phases", []):
            definitions.append(
                PhaseDefinition(
                    name=phase_dict["name"],
                    agent_type=phase_dict.get("agent_type"),
                    condition=_parse_condition(phase_dict.get("condition")),
                    parallel_group=group_name,
                    order=order,
                )
            )
        # After group: increment order so the next sequential phase comes after
        order += 1

    return definitions


def load_ticket_metadata_spec(phases_yaml: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the ticket_metadata spec list from phases.yaml."""
    return phases_yaml.get("ticket_metadata", [])


# ---------------------------------------------------------------------------
# config.yaml loader
# ---------------------------------------------------------------------------


def load_workflow_config(
    project_root: str | Path,
    phases_yaml_path: str | Path | None = None,
    config_yaml_path: str | Path | None = None,
) -> WorkflowConfig:
    """
    Load WorkflowConfig from .workflow/config.yaml and .workflow/phases.yaml.

    Args:
        project_root: Root of the consuming repository.
        phases_yaml_path: Override path for phases.yaml (default: .workflow/phases.yaml).
        config_yaml_path: Override path for config.yaml (default: .workflow/config.yaml).

    Returns:
        WorkflowConfig with all settings resolved (defaults applied where missing).
    """
    project_root = Path(project_root)
    phases_path = Path(phases_yaml_path) if phases_yaml_path else project_root / ".workflow" / "phases.yaml"
    config_path = Path(config_yaml_path) if config_yaml_path else project_root / ".workflow" / "config.yaml"

    if not HAS_YAML:
        raise ImportError(
            "PyYAML is required for workflow engine configuration. "
            "Install with: pip install pyyaml"
        )

    # Load phases.yaml
    if phases_path.exists():
        phases_doc = yaml.safe_load(phases_path.read_text(encoding="utf-8")) or {}
    else:
        phases_doc = yaml.safe_load(DEFAULT_PHASES_YAML) or {}

    phase_definitions = load_phase_definitions(phases_doc)

    # Load config.yaml
    config_doc: dict[str, Any] = {}
    if config_path.exists():
        config_doc = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    # Extract database settings
    db_section = config_doc.get("database", {})
    db_path = db_section.get("path", "build/Debug/docs/workflow.db")

    # Resolve relative paths against project_root
    if not Path(db_path).is_absolute():
        db_path = str(project_root / db_path)

    # Extract ticket settings
    tickets_section = config_doc.get("tickets", {})
    tickets_directory = tickets_section.get("directory", "tickets/")
    tickets_pattern = tickets_section.get("pattern", "*.md")
    id_regex = tickets_section.get("id_regex", r"^(\d{4}[a-z]?)_")

    # Resolve tickets directory against project_root
    if not Path(tickets_directory).is_absolute():
        tickets_directory = str(project_root / tickets_directory)

    # Extract agent settings
    agents_section = config_doc.get("agents", {})
    stale_timeout_minutes = int(agents_section.get("stale_timeout_minutes", 30))
    heartbeat_implicit = bool(agents_section.get("heartbeat_implicit", True))

    # Extract agent registry (maps agent_type -> spawn command)
    agent_registry: dict[str, dict] = {}
    registry_section = agents_section.get("registry", {})
    for agent_type, agent_config in registry_section.items():
        agent_registry[agent_type] = dict(agent_config)

    # Extract file conflict mode
    file_conflicts_section = config_doc.get("file_conflicts", {})
    file_conflict_mode = file_conflicts_section.get("mode", "advisory")

    # Extract markdown sync settings
    markdown_section = config_doc.get("markdown_sync", {})
    markdown_status_update = markdown_section.get("status_update", "realtime")
    markdown_log_update = markdown_section.get("workflow_log_update", "batch")

    # Priority order
    priority_order = config_doc.get("priority_order", ["Critical", "High", "Medium", "Low"])

    return WorkflowConfig(
        db_path=db_path,
        tickets_directory=tickets_directory,
        tickets_pattern=tickets_pattern,
        id_regex=id_regex,
        stale_timeout_minutes=stale_timeout_minutes,
        heartbeat_implicit=heartbeat_implicit,
        file_conflict_mode=file_conflict_mode,
        markdown_status_update=markdown_status_update,
        markdown_log_update=markdown_log_update,
        priority_order=priority_order,
        agent_registry=agent_registry,
        phase_definitions=phase_definitions,
    )


def extract_ticket_id(filename: str, id_regex: str) -> str | None:
    """
    Extract ticket ID from a filename using the configured regex.

    The regex must have one capture group that extracts the ID portion.

    Example:
        extract_ticket_id("0083_database_agent_orchestration.md", r"^(\\d{4}[a-z]?)_")
        → "0083"
    """
    match = re.match(id_regex, filename)
    if match:
        return match.group(1)
    return None
