"""
Tests for engine/config.py

Validates:
- load_workflow_config reads .workflow/config.yaml and .workflow/phases.yaml
- Sensible defaults when files are absent
- Phase condition evaluation (value, contains, has_multiple)
- extract_ticket_id uses configured regex
- Parallel group phases are parsed correctly
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from workflow_engine.engine.config import (
    extract_ticket_id,
    load_phase_definitions,
    load_workflow_config,
)
from workflow_engine.engine.models import PhaseCondition, PhaseDefinition


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project_root(tmp_path):
    """Create a minimal project root with .workflow/ directory."""
    workflow_dir = tmp_path / ".workflow"
    workflow_dir.mkdir()
    return tmp_path


def write_yaml(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Default config (no files)
# ---------------------------------------------------------------------------


def test_load_config_defaults_when_no_files(project_root):
    """load_workflow_config should return defaults when config files don't exist."""
    config = load_workflow_config(project_root)

    # Key defaults
    assert config.stale_timeout_minutes == 30
    assert config.heartbeat_implicit is True
    assert config.file_conflict_mode == "advisory"
    assert config.priority_order == ["Critical", "High", "Medium", "Low"]


def test_load_config_db_path_default(project_root):
    """Default db_path should be project_root/build/Debug/docs/workflow.db."""
    config = load_workflow_config(project_root)
    assert config.db_path.endswith("workflow.db")


# ---------------------------------------------------------------------------
# config.yaml parsing
# ---------------------------------------------------------------------------


def test_load_config_reads_stale_timeout(project_root):
    """load_workflow_config should read stale_timeout_minutes from config.yaml."""
    write_yaml(
        project_root / ".workflow" / "config.yaml",
        "agents:\n  stale_timeout_minutes: 45\n",
    )
    config = load_workflow_config(project_root)
    assert config.stale_timeout_minutes == 45


def test_load_config_reads_agent_registry(project_root):
    """load_workflow_config should read agent registry from config.yaml."""
    write_yaml(
        project_root / ".workflow" / "config.yaml",
        """\
agents:
  stale_timeout_minutes: 30
  registry:
    cpp-architect:
      command: "claude"
      args: ["--agent", "cpp-architect"]
      max_concurrent: 1
""",
    )
    config = load_workflow_config(project_root)
    assert "cpp-architect" in config.agent_registry
    assert config.agent_registry["cpp-architect"]["max_concurrent"] == 1


# ---------------------------------------------------------------------------
# phases.yaml parsing
# ---------------------------------------------------------------------------


def test_load_phase_definitions_sequential(project_root):
    """load_phase_definitions should parse sequential phase list."""
    write_yaml(
        project_root / ".workflow" / "phases.yaml",
        """\
phases:
  - name: "Design"
    agent_type: "cpp-architect"
  - name: "Design Review"
    agent_type: null
  - name: "Implementation"
    agent_type: "cpp-implementer"
""",
    )
    config = load_workflow_config(project_root)
    assert len(config.phase_definitions) == 3
    assert config.phase_definitions[0].name == "Design"
    assert config.phase_definitions[0].agent_type == "cpp-architect"
    assert config.phase_definitions[1].name == "Design Review"
    assert config.phase_definitions[1].agent_type is None  # human gate
    assert config.phase_definitions[2].name == "Implementation"


def test_load_phase_definitions_order(project_root):
    """Phase order should reflect position in phases.yaml."""
    write_yaml(
        project_root / ".workflow" / "phases.yaml",
        """\
phases:
  - name: "First"
    agent_type: "agent-a"
  - name: "Second"
    agent_type: "agent-b"
  - name: "Third"
    agent_type: "agent-c"
""",
    )
    config = load_workflow_config(project_root)
    orders = [p.order for p in config.phase_definitions]
    assert orders == [0, 1, 2]


def test_load_phase_definitions_with_condition(project_root):
    """Conditions should be parsed into PhaseCondition objects."""
    write_yaml(
        project_root / ".workflow" / "phases.yaml",
        """\
phases:
  - name: "Math Design"
    agent_type: "math-designer"
    condition:
      field: "requires_math_design"
      value: true
  - name: "Design"
    agent_type: "cpp-architect"
""",
    )
    config = load_workflow_config(project_root)
    math_phase = config.phase_definitions[0]
    assert math_phase.condition is not None
    assert math_phase.condition.field == "requires_math_design"
    assert math_phase.condition.value is True

    design_phase = config.phase_definitions[1]
    assert design_phase.condition is None


def test_load_phase_definitions_parallel_group(project_root):
    """Parallel group phases should have parallel_group set."""
    write_yaml(
        project_root / ".workflow" / "phases.yaml",
        """\
phases:
  - name: "Design"
    agent_type: "cpp-architect"

parallel_groups:
  impl:
    phases:
      - name: "C++ Implementation"
        agent_type: "cpp-implementer"
      - name: "Python Implementation"
        agent_type: "python-implementer"
""",
    )
    config = load_workflow_config(project_root)
    parallel = [p for p in config.phase_definitions if p.parallel_group]
    assert len(parallel) == 2
    assert all(p.parallel_group == "impl" for p in parallel)


# ---------------------------------------------------------------------------
# Condition evaluation tests
# ---------------------------------------------------------------------------


def test_condition_value_true():
    cond = PhaseCondition(field="requires_math_design", value=True)
    assert cond.evaluate({"requires_math_design": True}) is True
    assert cond.evaluate({"requires_math_design": False}) is False
    assert cond.evaluate({"requires_math_design": "Yes"}) is True
    assert cond.evaluate({"requires_math_design": "No"}) is False


def test_condition_contains():
    cond = PhaseCondition(field="languages", contains="Python")
    assert cond.evaluate({"languages": ["C++", "Python"]}) is True
    assert cond.evaluate({"languages": ["C++"]}) is False
    assert cond.evaluate({"languages": "C++, Python"}) is True
    assert cond.evaluate({"languages": "C++"}) is False


def test_condition_has_multiple():
    cond = PhaseCondition(field="languages", has_multiple=True)
    assert cond.evaluate({"languages": ["C++", "Python"]}) is True
    assert cond.evaluate({"languages": ["C++"]}) is False
    assert cond.evaluate({"languages": "C++, Python"}) is True
    assert cond.evaluate({"languages": "C++"}) is False


def test_condition_no_condition_always_true():
    phase = PhaseDefinition(name="Design", agent_type="cpp-architect", order=0)
    assert phase.is_applicable({}) is True
    assert phase.is_applicable({"languages": ["C++"]}) is True


# ---------------------------------------------------------------------------
# extract_ticket_id tests
# ---------------------------------------------------------------------------


def test_extract_ticket_id_standard_format():
    assert extract_ticket_id("0083_database_agent_orchestration.md", r"^(\d{4}[a-z]?)_") == "0083"


def test_extract_ticket_id_with_letter_suffix():
    assert extract_ticket_id("0078e_clang_tidy_rules.md", r"^(\d{4}[a-z]?)_") == "0078e"


def test_extract_ticket_id_no_match():
    assert extract_ticket_id("README.md", r"^(\d{4}[a-z]?)_") is None


def test_extract_ticket_id_custom_regex():
    assert extract_ticket_id("PROJ-001_feature.md", r"^(PROJ-\d+)_") == "PROJ-001"
