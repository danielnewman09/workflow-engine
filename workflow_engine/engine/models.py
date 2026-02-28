#!/usr/bin/env python3
# Ticket: 0083_database_agent_orchestration
# Design: docs/designs/0083_database_agent_orchestration/design.md
"""
Workflow Engine Data Models

Typed dataclasses representing the core domain objects in the workflow engine.
All models use @dataclass (not Pydantic) per design review decision to keep the
engine dependency-free beyond fastmcp. JSON serialization is handled manually.

Design note: Fields use Python-native types (str | None, list[str]) rather than
Optional[str] to match the Python 3.12+ style referenced in the prototype results.
"""

import json
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Phase status constants
# ---------------------------------------------------------------------------

class PhaseStatus:
    PENDING = "pending"
    BLOCKED = "blocked"
    AVAILABLE = "available"
    CLAIMED = "claimed"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"

    ALL = frozenset([
        PENDING, BLOCKED, AVAILABLE, CLAIMED, RUNNING,
        COMPLETED, FAILED, SKIPPED,
    ])

    # Terminal states — once reached, the phase does not change further
    TERMINAL = frozenset([COMPLETED, FAILED, SKIPPED])


class AgentStatus:
    IDLE = "idle"
    WORKING = "working"
    STALE = "stale"
    TERMINATED = "terminated"


class GateStatus:
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CHANGES_REQUESTED = "changes_requested"


# ---------------------------------------------------------------------------
# Core dataclasses
# ---------------------------------------------------------------------------


@dataclass
class Ticket:
    """Mirrors a ticket markdown file's metadata in the database."""
    id: str                         # e.g. "0083"
    name: str                       # e.g. "database_agent_orchestration"
    full_name: str                  # e.g. "0083_database_agent_orchestration"
    current_status: str
    markdown_path: str              # path to tickets/*.md
    priority: str | None = None     # Low, Medium, High, Critical
    complexity: str | None = None   # Small, Medium, Large, XL
    components: str | None = None   # comma-separated
    languages: str = "C++"          # comma-separated
    github_issue: int | None = None
    custom_metadata: str | None = None  # JSON blob
    created_at: str | None = None
    updated_at: str | None = None

    @property
    def language_list(self) -> list[str]:
        """Return languages as a list."""
        return [lang.strip() for lang in self.languages.split(",") if lang.strip()]

    @property
    def component_list(self) -> list[str]:
        """Return components as a list."""
        if not self.components:
            return []
        return [c.strip() for c in self.components.split(",") if c.strip()]

    def get_custom_metadata(self) -> dict[str, Any]:
        """Parse and return custom_metadata JSON, or empty dict."""
        if not self.custom_metadata:
            return {}
        try:
            return json.loads(self.custom_metadata)
        except (json.JSONDecodeError, TypeError):
            return {}

    @classmethod
    def from_row(cls, row: Any) -> "Ticket":
        """Construct from a sqlite3.Row or dict."""
        d = dict(row)
        return cls(
            id=d["id"],
            name=d["name"],
            full_name=d["full_name"],
            current_status=d["current_status"],
            markdown_path=d["markdown_path"],
            priority=d.get("priority"),
            complexity=d.get("complexity"),
            components=d.get("components"),
            languages=d.get("languages") or "C++",
            github_issue=d.get("github_issue"),
            custom_metadata=d.get("custom_metadata"),
            created_at=d.get("created_at"),
            updated_at=d.get("updated_at"),
        )


@dataclass
class Phase:
    """An individual workflow phase as a claimable work item."""
    id: int
    ticket_id: str
    phase_name: str
    phase_order: int
    status: str = PhaseStatus.PENDING
    agent_type: str | None = None   # None means human gate
    claimed_by: str | None = None   # agent instance ID
    claimed_at: str | None = None
    heartbeat_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    result_summary: str | None = None
    error_details: str | None = None
    artifacts: str | None = None    # JSON array of file paths
    parallel_group: str | None = None

    @property
    def is_human_gate(self) -> bool:
        """A phase with no agent_type requires human review."""
        return self.agent_type is None

    @property
    def artifact_list(self) -> list[str]:
        """Return artifacts as a list of file paths."""
        if not self.artifacts:
            return []
        try:
            return json.loads(self.artifacts)
        except (json.JSONDecodeError, TypeError):
            return []

    @classmethod
    def from_row(cls, row: Any) -> "Phase":
        """Construct from a sqlite3.Row or dict."""
        d = dict(row)
        return cls(
            id=d["id"],
            ticket_id=d["ticket_id"],
            phase_name=d["phase_name"],
            phase_order=d["phase_order"],
            status=d["status"],
            agent_type=d.get("agent_type"),
            claimed_by=d.get("claimed_by"),
            claimed_at=d.get("claimed_at"),
            heartbeat_at=d.get("heartbeat_at"),
            started_at=d.get("started_at"),
            completed_at=d.get("completed_at"),
            result_summary=d.get("result_summary"),
            error_details=d.get("error_details"),
            artifacts=d.get("artifacts"),
            parallel_group=d.get("parallel_group"),
        )


@dataclass
class HumanGate:
    """A human review gate that blocks the next phase until resolved."""
    id: int
    phase_id: int
    ticket_id: str
    gate_type: str                  # e.g. "design_review", "prototype_review"
    status: str = GateStatus.PENDING
    requested_at: str | None = None
    decided_at: str | None = None
    decided_by: str | None = None   # human reviewer identifier
    decision_notes: str | None = None
    context: str | None = None      # JSON blob

    def get_context(self) -> dict[str, Any]:
        """Parse and return context JSON, or empty dict."""
        if not self.context:
            return {}
        try:
            return json.loads(self.context)
        except (json.JSONDecodeError, TypeError):
            return {}

    @classmethod
    def from_row(cls, row: Any) -> "HumanGate":
        """Construct from a sqlite3.Row or dict."""
        d = dict(row)
        return cls(
            id=d["id"],
            phase_id=d["phase_id"],
            ticket_id=d["ticket_id"],
            gate_type=d["gate_type"],
            status=d["status"],
            requested_at=d.get("requested_at"),
            decided_at=d.get("decided_at"),
            decided_by=d.get("decided_by"),
            decision_notes=d.get("decision_notes"),
            context=d.get("context"),
        )


@dataclass
class Agent:
    """Registered agent instance for liveness tracking."""
    id: str                         # UUID
    agent_type: str                 # e.g. "cpp-architect"
    status: str = AgentStatus.IDLE
    current_phase_id: int | None = None
    registered_at: str | None = None
    last_heartbeat: str | None = None
    metadata: str | None = None     # JSON blob

    def get_metadata(self) -> dict[str, Any]:
        """Parse and return metadata JSON, or empty dict."""
        if not self.metadata:
            return {}
        try:
            return json.loads(self.metadata)
        except (json.JSONDecodeError, TypeError):
            return {}

    @classmethod
    def from_row(cls, row: Any) -> "Agent":
        """Construct from a sqlite3.Row or dict."""
        d = dict(row)
        return cls(
            id=d["id"],
            agent_type=d["agent_type"],
            status=d["status"],
            current_phase_id=d.get("current_phase_id"),
            registered_at=d.get("registered_at"),
            last_heartbeat=d.get("last_heartbeat"),
            metadata=d.get("metadata"),
        )


@dataclass
class Dependency:
    """Inter-ticket dependency — blocked_ticket waits for blocking_ticket."""
    id: int
    blocked_ticket_id: str
    blocking_ticket_id: str
    dependency_type: str = "completion"  # completion, design, implementation
    resolved: bool = False
    created_at: str | None = None
    resolved_at: str | None = None

    @classmethod
    def from_row(cls, row: Any) -> "Dependency":
        """Construct from a sqlite3.Row or dict."""
        d = dict(row)
        return cls(
            id=d["id"],
            blocked_ticket_id=d["blocked_ticket_id"],
            blocking_ticket_id=d["blocking_ticket_id"],
            dependency_type=d.get("dependency_type", "completion"),
            resolved=bool(d.get("resolved", 0)),
            created_at=d.get("created_at"),
            resolved_at=d.get("resolved_at"),
        )


@dataclass
class FileLock:
    """File-level lock for conflict detection."""
    id: int
    file_path: str
    phase_id: int
    agent_id: str
    acquired_at: str | None = None
    released_at: str | None = None

    @property
    def is_active(self) -> bool:
        """Lock is active when not yet released."""
        return self.released_at is None

    @classmethod
    def from_row(cls, row: Any) -> "FileLock":
        """Construct from a sqlite3.Row or dict."""
        d = dict(row)
        return cls(
            id=d["id"],
            file_path=d["file_path"],
            phase_id=d["phase_id"],
            agent_id=d["agent_id"],
            acquired_at=d.get("acquired_at"),
            released_at=d.get("released_at"),
        )


@dataclass
class AuditEntry:
    """Immutable audit log entry for a state transition."""
    id: int
    timestamp: str
    actor: str                      # agent ID, "human:{name}", or "scheduler"
    action: str                     # e.g. "claim_phase", "complete_phase"
    entity_type: str                # "ticket", "phase", "gate", "agent"
    entity_id: str
    old_state: str | None = None
    new_state: str | None = None
    details: str | None = None      # JSON blob

    def get_details(self) -> dict[str, Any]:
        """Parse and return details JSON, or empty dict."""
        if not self.details:
            return {}
        try:
            return json.loads(self.details)
        except (json.JSONDecodeError, TypeError):
            return {}

    @classmethod
    def from_row(cls, row: Any) -> "AuditEntry":
        """Construct from a sqlite3.Row or dict."""
        d = dict(row)
        return cls(
            id=d["id"],
            timestamp=d["timestamp"],
            actor=d["actor"],
            action=d["action"],
            entity_type=d["entity_type"],
            entity_id=str(d["entity_id"]),
            old_state=d.get("old_state"),
            new_state=d.get("new_state"),
            details=d.get("details"),
        )


# ---------------------------------------------------------------------------
# Phase definition (from phases.yaml — not stored in DB, only used at runtime)
# ---------------------------------------------------------------------------


@dataclass
class PhaseCondition:
    """Condition that determines whether a phase applies to a given ticket."""
    field: str                          # ticket metadata field name
    value: Any = None                   # exact value match
    contains: str | None = None         # list-contains check
    has_multiple: bool = False          # list has 2+ items

    def evaluate(self, ticket_metadata: dict[str, Any]) -> bool:
        """Evaluate this condition against ticket metadata."""
        val = ticket_metadata.get(self.field)
        if self.has_multiple:
            if isinstance(val, list):
                return len(val) >= 2
            if isinstance(val, str):
                return len([v for v in val.split(",") if v.strip()]) >= 2
            return False
        if self.contains is not None:
            if isinstance(val, list):
                return self.contains in val
            if isinstance(val, str):
                return self.contains in [v.strip() for v in val.split(",")]
            return False
        if self.value is not None:
            # Normalize boolean comparison
            if isinstance(self.value, bool):
                if isinstance(val, str):
                    return val.lower() in ("yes", "true", "1") if self.value else val.lower() in ("no", "false", "0", "")
                return bool(val) == self.value
            return val == self.value
        return True


@dataclass
class PhaseDefinition:
    """A phase definition from phases.yaml."""
    name: str
    agent_type: str | None = None       # None = human gate
    condition: PhaseCondition | None = None
    parallel_group: str | None = None
    order: int = 0                      # assigned by loader

    def is_applicable(self, ticket_metadata: dict[str, Any]) -> bool:
        """Return True if this phase applies to a ticket with the given metadata."""
        if self.condition is None:
            return True
        return self.condition.evaluate(ticket_metadata)


@dataclass
class WorkflowConfig:
    """Runtime configuration loaded from .workflow/config.yaml."""
    db_path: str = "build/Debug/docs/workflow.db"
    tickets_directory: str = "tickets/"
    tickets_pattern: str = "*.md"
    id_regex: str = r"^(\d{4}[a-z]?)_"
    stale_timeout_minutes: int = 30
    heartbeat_implicit: bool = True
    file_conflict_mode: str = "advisory"    # "advisory" or "blocking"
    markdown_status_update: str = "realtime"
    markdown_log_update: str = "batch"
    priority_order: list[str] = field(
        default_factory=lambda: ["Critical", "High", "Medium", "Low"]
    )
    agent_registry: dict[str, dict] = field(default_factory=dict)
    phase_definitions: list[PhaseDefinition] = field(default_factory=list)
