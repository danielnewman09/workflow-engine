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
preamble: []

integration: []

language_pipelines:
  condition_field: "languages"
  steps:
    - step: "Design"
      role: "architect"
    - step: "Design Review"
      role: null
    - step: "Implementation"
      role: "implementer"
    - step: "Test Writing"
      role: "test-writer"
  languages:
    - name: "C++"
      slug: "cpp"

postamble:
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
# Language pipeline expansion
# ---------------------------------------------------------------------------


def _step_slug(step_name: str) -> str:
    """Convert a step name to a slug for parallel groups.

    e.g. "Design Review" -> "design_review", "Test Writing" -> "test_writing"
    """
    return step_name.lower().replace(" ", "_")


def _expand_language_pipelines(
    pipelines_dict: dict[str, Any],
) -> list[PhaseDefinition]:
    """Expand the language_pipelines template into individual PhaseDefinitions.

    For each step, generates one phase per language. All phases within the
    same step share the same order and parallel_group, so they can run
    concurrently for multi-language tickets.

    Args:
        pipelines_dict: The language_pipelines section from phases.yaml.

    Returns:
        List of PhaseDefinition objects for all language/step combinations.
    """
    condition_field = pipelines_dict.get("condition_field", "languages")
    steps = pipelines_dict.get("steps", [])
    languages = pipelines_dict.get("languages", [])

    definitions: list[PhaseDefinition] = []

    for step_dict in steps:
        step_name = step_dict["step"]
        role = step_dict.get("role")
        parallel_group = _step_slug(step_name)

        for lang_dict in languages:
            lang_name = lang_dict["name"]
            slug = lang_dict["slug"]

            phase_name = f"{lang_name} {step_name}"
            agent_type = f"{slug}-{role}" if role is not None else None
            condition = PhaseCondition(field=condition_field, contains=lang_name)

            definitions.append(
                PhaseDefinition(
                    name=phase_name,
                    agent_type=agent_type,
                    condition=condition,
                    parallel_group=parallel_group,
                    order=0,  # order is assigned later by load_phase_definitions
                )
            )

    return definitions


# ---------------------------------------------------------------------------
# phases.yaml loader
# ---------------------------------------------------------------------------


def _parse_section_phases(
    section: list[dict[str, Any]] | None,
) -> list[PhaseDefinition]:
    """Parse a preamble/integration/postamble section into PhaseDefinitions."""
    if not section:
        return []
    definitions: list[PhaseDefinition] = []
    for phase_dict in section:
        definitions.append(
            PhaseDefinition(
                name=phase_dict["name"],
                agent_type=phase_dict.get("agent_type"),
                condition=_parse_condition(phase_dict.get("condition")),
                parallel_group=None,
                order=0,  # assigned later
            )
        )
    return definitions


def load_phase_definitions(phases_yaml: dict[str, Any]) -> list[PhaseDefinition]:
    """
    Parse a phases.yaml document into a list of PhaseDefinition objects.

    Supports two formats:
    1. New format: preamble, integration, language_pipelines, postamble
    2. Legacy format: phases list + parallel_groups

    All phases receive an `order` field that controls DB phase_order, used
    by the scheduler to determine availability sequence.
    """
    # Detect format: new format has language_pipelines or preamble key
    if "language_pipelines" in phases_yaml or "preamble" in phases_yaml:
        return _load_phase_definitions_new(phases_yaml)
    return _load_phase_definitions_legacy(phases_yaml)


def _load_phase_definitions_new(phases_yaml: dict[str, Any]) -> list[PhaseDefinition]:
    """Parse new-format phases.yaml (preamble/integration/language_pipelines/postamble)."""
    all_phases: list[PhaseDefinition] = []

    # 1. Preamble
    all_phases.extend(_parse_section_phases(phases_yaml.get("preamble")))

    # 2. Integration
    all_phases.extend(_parse_section_phases(phases_yaml.get("integration")))

    # 3. Language pipelines
    pipelines = phases_yaml.get("language_pipelines")
    if pipelines:
        all_phases.extend(_expand_language_pipelines(pipelines))

    # 4. Postamble
    all_phases.extend(_parse_section_phases(phases_yaml.get("postamble")))

    # Assign order: sequential phases get individual orders,
    # phases sharing a parallel_group get the same order
    order = 0
    i = 0
    while i < len(all_phases):
        phase = all_phases[i]
        if phase.parallel_group is not None:
            # Collect all phases with the same parallel_group (contiguous)
            group = phase.parallel_group
            while i < len(all_phases) and all_phases[i].parallel_group == group:
                all_phases[i].order = order
                i += 1
            order += 1
        else:
            phase.order = order
            order += 1
            i += 1

    return all_phases


def _load_phase_definitions_legacy(phases_yaml: dict[str, Any]) -> list[PhaseDefinition]:
    """Parse legacy-format phases.yaml (phases list + parallel_groups)."""
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

    # Parallel groups
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

    # GitHub settings
    github_section = config_doc.get("github", {})
    github_repository = github_section.get("repository")

    # Traceability settings
    trace_section = config_doc.get("traceability", {})
    traceability_db_path = trace_section.get("db_path")
    if traceability_db_path and not Path(traceability_db_path).is_absolute():
        traceability_db_path = str(project_root / traceability_db_path)
    traceability_source_dir = trace_section.get("source_dir", "msd")
    traceability_designs_dir = trace_section.get("designs_dir", "docs/designs")
    traceability_models_path = trace_section.get("models_path", "replay/replay/models.py")
    traceability_generated_models_path = trace_section.get(
        "generated_models_path", "replay/replay/generated_models.py"
    )
    traceability_coverage_info_path = trace_section.get(
        "coverage_info_path", "build/Debug/coverage_filtered.info"
    )

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
        github_repository=github_repository,
        traceability_db_path=traceability_db_path,
        traceability_source_dir=traceability_source_dir,
        traceability_designs_dir=traceability_designs_dir,
        traceability_models_path=traceability_models_path,
        traceability_generated_models_path=traceability_generated_models_path,
        traceability_coverage_info_path=traceability_coverage_info_path,
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
