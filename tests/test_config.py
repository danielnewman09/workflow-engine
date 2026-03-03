"""
Tests for engine/config.py

Validates:
- load_workflow_config reads .workflow/config.yaml and .workflow/phases.yaml
- Sensible defaults when files are absent
- Phase condition evaluation (value, contains, has_multiple)
- extract_ticket_id uses configured regex
- _expand_language_pipelines produces correct phases
- New format (preamble/integration/language_pipelines/postamble) parsing
- Legacy format (phases/parallel_groups) still works
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from workflow_engine.engine.config import (
    _expand_language_pipelines,
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
      args: ["--agent", "cpp/architect"]
      max_concurrent: 1
""",
    )
    config = load_workflow_config(project_root)
    assert "cpp-architect" in config.agent_registry
    assert config.agent_registry["cpp-architect"]["max_concurrent"] == 1


# ---------------------------------------------------------------------------
# New format: preamble/integration/language_pipelines/postamble
# ---------------------------------------------------------------------------


def test_load_phase_definitions_new_format_basic(project_root):
    """New format should produce phases in order: preamble, integration, pipelines, postamble."""
    write_yaml(
        project_root / ".workflow" / "phases.yaml",
        """\
preamble:
  - name: "Math Design"
    agent_type: "math-designer"
    condition:
      field: "requires_math_design"
      value: true

integration: []

language_pipelines:
  condition_field: "languages"
  steps:
    - step: "Design"
      role: "architect"
    - step: "Implementation"
      role: "implementer"
  languages:
    - name: "C++"
      slug: "cpp"

postamble:
  - name: "Review"
    agent_type: "reviewer"
""",
    )
    config = load_workflow_config(project_root)
    names = [p.name for p in config.phase_definitions]
    assert names == [
        "Math Design",
        "C++ Design",
        "C++ Implementation",
        "Review",
    ]


def test_load_phase_definitions_new_format_order(project_root):
    """Phases should get monotonically increasing order values."""
    write_yaml(
        project_root / ".workflow" / "phases.yaml",
        """\
preamble:
  - name: "Preamble Phase"
    agent_type: "agent-a"

language_pipelines:
  condition_field: "languages"
  steps:
    - step: "Design"
      role: "architect"
  languages:
    - name: "C++"
      slug: "cpp"
    - name: "Python"
      slug: "python"

postamble:
  - name: "Final Phase"
    agent_type: "agent-b"
""",
    )
    config = load_workflow_config(project_root)
    orders = [p.order for p in config.phase_definitions]
    # preamble=0, design group (C++ Design, Python Design) both=1, postamble=2
    assert orders == [0, 1, 1, 2]


def test_load_phase_definitions_multi_language_parallel_groups(project_root):
    """Multi-language steps should share parallel_group within each step."""
    write_yaml(
        project_root / ".workflow" / "phases.yaml",
        """\
language_pipelines:
  condition_field: "languages"
  steps:
    - step: "Design"
      role: "architect"
    - step: "Test Writing"
      role: "test-writer"
  languages:
    - name: "C++"
      slug: "cpp"
    - name: "Python"
      slug: "python"
""",
    )
    config = load_workflow_config(project_root)
    phases = config.phase_definitions

    design_phases = [p for p in phases if "Design" in p.name]
    assert all(p.parallel_group == "design" for p in design_phases)

    test_phases = [p for p in phases if "Test Writing" in p.name]
    assert all(p.parallel_group == "test_writing" for p in test_phases)


def test_load_phase_definitions_conditions_on_pipeline_phases(project_root):
    """Pipeline phases should have contains conditions for their language."""
    write_yaml(
        project_root / ".workflow" / "phases.yaml",
        """\
language_pipelines:
  condition_field: "languages"
  steps:
    - step: "Design"
      role: "architect"
  languages:
    - name: "C++"
      slug: "cpp"
    - name: "Python"
      slug: "python"
""",
    )
    config = load_workflow_config(project_root)

    cpp_design = config.phase_definitions[0]
    assert cpp_design.name == "C++ Design"
    assert cpp_design.condition is not None
    assert cpp_design.condition.field == "languages"
    assert cpp_design.condition.contains == "C++"

    python_design = config.phase_definitions[1]
    assert python_design.name == "Python Design"
    assert python_design.condition.contains == "Python"


# ---------------------------------------------------------------------------
# _expand_language_pipelines tests
# ---------------------------------------------------------------------------


def test_expand_single_language():
    """Single language should produce one phase per step."""
    pipelines = {
        "condition_field": "languages",
        "steps": [
            {"step": "Design", "role": "architect"},
            {"step": "Design Review", "role": None},
            {"step": "Test Writing", "role": "test-writer"},
            {"step": "Implementation", "role": "implementer"},
        ],
        "languages": [
            {"name": "C++", "slug": "cpp"},
        ],
    }
    phases = _expand_language_pipelines(pipelines)

    assert len(phases) == 4
    assert [p.name for p in phases] == [
        "C++ Design",
        "C++ Design Review",
        "C++ Test Writing",
        "C++ Implementation",
    ]


def test_expand_multi_language():
    """Multiple languages produce phases grouped by step."""
    pipelines = {
        "condition_field": "languages",
        "steps": [
            {"step": "Design", "role": "architect"},
            {"step": "Implementation", "role": "implementer"},
        ],
        "languages": [
            {"name": "C++", "slug": "cpp"},
            {"name": "Python", "slug": "python"},
            {"name": "Frontend", "slug": "frontend"},
        ],
    }
    phases = _expand_language_pipelines(pipelines)

    assert len(phases) == 6
    names = [p.name for p in phases]
    assert names == [
        "C++ Design",
        "Python Design",
        "Frontend Design",
        "C++ Implementation",
        "Python Implementation",
        "Frontend Implementation",
    ]


def test_expand_agent_type_slug_role_concatenation():
    """Agent type should be slug-role (e.g., 'cpp-architect')."""
    pipelines = {
        "condition_field": "languages",
        "steps": [
            {"step": "Design", "role": "architect"},
            {"step": "Test Writing", "role": "test-writer"},
        ],
        "languages": [
            {"name": "C++", "slug": "cpp"},
            {"name": "Python", "slug": "python"},
        ],
    }
    phases = _expand_language_pipelines(pipelines)

    agent_types = [p.agent_type for p in phases]
    assert agent_types == [
        "cpp-architect",
        "python-architect",
        "cpp-test-writer",
        "python-test-writer",
    ]


def test_expand_null_role_produces_human_gate():
    """A step with role=null should produce agent_type=None (human gate)."""
    pipelines = {
        "condition_field": "languages",
        "steps": [
            {"step": "Design Review", "role": None},
        ],
        "languages": [
            {"name": "C++", "slug": "cpp"},
        ],
    }
    phases = _expand_language_pipelines(pipelines)

    assert len(phases) == 1
    assert phases[0].name == "C++ Design Review"
    assert phases[0].agent_type is None


def test_expand_parallel_groups():
    """All languages within the same step should share a parallel_group."""
    pipelines = {
        "condition_field": "languages",
        "steps": [
            {"step": "Design", "role": "architect"},
            {"step": "Implementation", "role": "implementer"},
        ],
        "languages": [
            {"name": "C++", "slug": "cpp"},
            {"name": "Python", "slug": "python"},
        ],
    }
    phases = _expand_language_pipelines(pipelines)

    design_group = {p.parallel_group for p in phases if "Design" in p.name}
    impl_group = {p.parallel_group for p in phases if "Implementation" in p.name}
    assert design_group == {"design"}
    assert impl_group == {"implementation"}


def test_expand_conditions():
    """Each phase should have a contains condition for its language."""
    pipelines = {
        "condition_field": "languages",
        "steps": [
            {"step": "Design", "role": "architect"},
        ],
        "languages": [
            {"name": "C++", "slug": "cpp"},
            {"name": "Frontend", "slug": "frontend"},
        ],
    }
    phases = _expand_language_pipelines(pipelines)

    assert phases[0].condition.field == "languages"
    assert phases[0].condition.contains == "C++"
    assert phases[1].condition.contains == "Frontend"


# ---------------------------------------------------------------------------
# Legacy format (phases + parallel_groups) — backward compatibility
# ---------------------------------------------------------------------------


def test_load_phase_definitions_legacy_sequential(project_root):
    """Legacy format with phases list should still work."""
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


def test_load_phase_definitions_legacy_order(project_root):
    """Legacy format phase order should reflect position in list."""
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


def test_load_phase_definitions_legacy_with_condition(project_root):
    """Legacy format conditions should be parsed into PhaseCondition objects."""
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


def test_load_phase_definitions_legacy_parallel_group(project_root):
    """Legacy format parallel group phases should have parallel_group set."""
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
# Full fixture file test
# ---------------------------------------------------------------------------


def test_load_fixture_phases_yaml():
    """The test fixture phases.yaml should expand to the expected phases."""
    import yaml

    fixture_path = Path(__file__).parent / "fixtures" / ".workflow" / "phases.yaml"
    phases_doc = yaml.safe_load(fixture_path.read_text(encoding="utf-8"))
    phases = load_phase_definitions(phases_doc)

    names = [p.name for p in phases]

    # Preamble
    assert "Math Design" in names
    assert "Math Design Review" in names

    # Integration
    assert "Integration Design" in names
    assert "Integration Review" in names

    # Language pipelines — all 3 languages × 5 steps = 15 phases
    for lang in ["C++", "Python", "Frontend"]:
        for step in ["Design", "Design Review", "Test Writing", "Implementation", "Quality Gate"]:
            assert f"{lang} {step}" in names, f"Missing {lang} {step}"

    # Postamble
    assert "Integration Test" in names
    assert "Implementation Review" in names
    assert "Documentation" in names
    assert "Tutorial" in names

    # Total: 2 preamble + 2 integration + 15 pipeline + 4 postamble = 23
    assert len(phases) == 23


def test_fixture_phases_yaml_ordering():
    """Fixture phases should have correct ordering: preamble < integration < pipelines < postamble."""
    import yaml

    fixture_path = Path(__file__).parent / "fixtures" / ".workflow" / "phases.yaml"
    phases_doc = yaml.safe_load(fixture_path.read_text(encoding="utf-8"))
    phases = load_phase_definitions(phases_doc)

    phase_order = {p.name: p.order for p in phases}

    # Preamble before integration
    assert phase_order["Math Design"] < phase_order["Integration Design"]

    # Integration before language pipelines
    assert phase_order["Integration Design"] < phase_order["C++ Design"]

    # Language pipeline steps in order
    assert phase_order["C++ Design"] < phase_order["C++ Design Review"]
    assert phase_order["C++ Design Review"] < phase_order["C++ Test Writing"]
    assert phase_order["C++ Test Writing"] < phase_order["C++ Implementation"]
    assert phase_order["C++ Implementation"] < phase_order["C++ Quality Gate"]

    # Same-step languages share order
    assert phase_order["C++ Design"] == phase_order["Python Design"]
    assert phase_order["C++ Design"] == phase_order["Frontend Design"]
    assert phase_order["C++ Quality Gate"] == phase_order["Python Quality Gate"]

    # Language pipelines before postamble
    assert phase_order["C++ Quality Gate"] < phase_order["Integration Test"]


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
