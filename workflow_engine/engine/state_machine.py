#!/usr/bin/env python3
# Ticket: 0083_database_agent_orchestration
# Design: docs/designs/0083_database_agent_orchestration/design.md
"""
Workflow Engine Phase State Machine

Defines valid phase status transitions and validates them.

State diagram (from design.md):
    pending   → blocked    (dependency not met or human gate pending)
    pending   → available  (all prerequisites satisfied)
    blocked   → available  (dependency resolved or gate approved)
    available → claimed    (agent atomically claims)
    claimed   → running    (agent begins execution)
    running   → completed  (agent reports success)
    running   → failed     (agent reports failure or stale claim cleanup)
    pending   → skipped    (conditional phase not applicable)
    claimed   → available  (agent releases — stale recovery or explicit release)
    claimed   → failed     (stale recovery marks directly failed)

The scheduler uses this state machine to validate all status updates.
Invalid transitions raise InvalidTransitionError.
"""

from .models import PhaseStatus


# ---------------------------------------------------------------------------
# Valid transitions: {from_status: set(to_statuses)}
# ---------------------------------------------------------------------------

VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    PhaseStatus.PENDING: frozenset([
        PhaseStatus.BLOCKED,
        PhaseStatus.AVAILABLE,
        PhaseStatus.SKIPPED,
    ]),
    PhaseStatus.BLOCKED: frozenset([
        PhaseStatus.AVAILABLE,
    ]),
    PhaseStatus.AVAILABLE: frozenset([
        PhaseStatus.CLAIMED,
        PhaseStatus.BLOCKED,    # can be re-blocked if dependency re-emerges
    ]),
    PhaseStatus.CLAIMED: frozenset([
        PhaseStatus.RUNNING,
        PhaseStatus.AVAILABLE,  # release back to available (stale recovery)
        PhaseStatus.FAILED,     # stale recovery may mark directly failed
    ]),
    PhaseStatus.RUNNING: frozenset([
        PhaseStatus.COMPLETED,
        PhaseStatus.FAILED,
    ]),
    # Terminal states: no outgoing transitions
    PhaseStatus.COMPLETED: frozenset(),
    PhaseStatus.FAILED: frozenset(),
    PhaseStatus.SKIPPED: frozenset(),
}


# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------


class InvalidTransitionError(ValueError):
    """Raised when a phase status transition is not allowed by the state machine."""

    def __init__(self, from_status: str, to_status: str, phase_id: int | None = None):
        self.from_status = from_status
        self.to_status = to_status
        self.phase_id = phase_id
        phase_info = f" (phase_id={phase_id})" if phase_id is not None else ""
        super().__init__(
            f"Invalid phase transition{phase_info}: "
            f"'{from_status}' → '{to_status}'. "
            f"Valid transitions from '{from_status}': "
            f"{sorted(VALID_TRANSITIONS.get(from_status, frozenset()))}"
        )


class UnknownStatusError(ValueError):
    """Raised when an unknown phase status is encountered."""

    def __init__(self, status: str):
        self.status = status
        super().__init__(
            f"Unknown phase status: '{status}'. "
            f"Valid statuses: {sorted(PhaseStatus.ALL)}"
        )


# ---------------------------------------------------------------------------
# State machine functions
# ---------------------------------------------------------------------------


def validate_transition(
    from_status: str,
    to_status: str,
    phase_id: int | None = None,
) -> None:
    """
    Validate that a phase status transition is allowed.

    Raises:
        UnknownStatusError: if either status is not in PhaseStatus.ALL
        InvalidTransitionError: if the transition is not in VALID_TRANSITIONS
    """
    if from_status not in PhaseStatus.ALL:
        raise UnknownStatusError(from_status)
    if to_status not in PhaseStatus.ALL:
        raise UnknownStatusError(to_status)

    allowed = VALID_TRANSITIONS.get(from_status, frozenset())
    if to_status not in allowed:
        raise InvalidTransitionError(from_status, to_status, phase_id)


def can_transition(from_status: str, to_status: str) -> bool:
    """Return True if the transition from_status → to_status is valid."""
    return to_status in VALID_TRANSITIONS.get(from_status, frozenset())


def is_terminal(status: str) -> bool:
    """Return True if the status is terminal (no further transitions possible)."""
    return status in PhaseStatus.TERMINAL


def is_claimable(status: str) -> bool:
    """Return True if the phase can be claimed by an agent."""
    return status == PhaseStatus.AVAILABLE


def available_transitions(from_status: str) -> frozenset[str]:
    """Return the set of valid destination statuses from from_status."""
    return VALID_TRANSITIONS.get(from_status, frozenset())
