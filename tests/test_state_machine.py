"""
Tests for engine/state_machine.py

Validates:
- All valid transitions are accepted
- All invalid transitions raise InvalidTransitionError
- Terminal states have no outgoing transitions
- is_terminal() and is_claimable() return correct values
"""

import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from workflow_engine.engine.state_machine import (
    InvalidTransitionError,
    UnknownStatusError,
    available_transitions,
    can_transition,
    is_claimable,
    is_terminal,
    validate_transition,
)
from workflow_engine.engine.models import PhaseStatus


# ---------------------------------------------------------------------------
# Valid transitions
# ---------------------------------------------------------------------------

VALID_TRANSITION_PAIRS = [
    (PhaseStatus.PENDING, PhaseStatus.BLOCKED),
    (PhaseStatus.PENDING, PhaseStatus.AVAILABLE),
    (PhaseStatus.PENDING, PhaseStatus.SKIPPED),
    (PhaseStatus.BLOCKED, PhaseStatus.AVAILABLE),
    (PhaseStatus.AVAILABLE, PhaseStatus.CLAIMED),
    (PhaseStatus.AVAILABLE, PhaseStatus.BLOCKED),
    (PhaseStatus.CLAIMED, PhaseStatus.RUNNING),
    (PhaseStatus.CLAIMED, PhaseStatus.AVAILABLE),
    (PhaseStatus.CLAIMED, PhaseStatus.FAILED),
    (PhaseStatus.RUNNING, PhaseStatus.COMPLETED),
    (PhaseStatus.RUNNING, PhaseStatus.FAILED),
]


@pytest.mark.parametrize("from_status,to_status", VALID_TRANSITION_PAIRS)
def test_valid_transitions_do_not_raise(from_status, to_status):
    """All valid transitions should pass without raising."""
    validate_transition(from_status, to_status)


@pytest.mark.parametrize("from_status,to_status", VALID_TRANSITION_PAIRS)
def test_can_transition_returns_true_for_valid(from_status, to_status):
    assert can_transition(from_status, to_status) is True


# ---------------------------------------------------------------------------
# Invalid transitions
# ---------------------------------------------------------------------------

INVALID_TRANSITION_PAIRS = [
    (PhaseStatus.PENDING, PhaseStatus.RUNNING),
    (PhaseStatus.PENDING, PhaseStatus.COMPLETED),
    (PhaseStatus.PENDING, PhaseStatus.FAILED),
    (PhaseStatus.BLOCKED, PhaseStatus.CLAIMED),
    (PhaseStatus.AVAILABLE, PhaseStatus.RUNNING),
    (PhaseStatus.AVAILABLE, PhaseStatus.COMPLETED),
    (PhaseStatus.CLAIMED, PhaseStatus.PENDING),
    (PhaseStatus.RUNNING, PhaseStatus.PENDING),
    (PhaseStatus.RUNNING, PhaseStatus.AVAILABLE),
    (PhaseStatus.COMPLETED, PhaseStatus.AVAILABLE),
    (PhaseStatus.COMPLETED, PhaseStatus.PENDING),
    (PhaseStatus.FAILED, PhaseStatus.AVAILABLE),
    (PhaseStatus.SKIPPED, PhaseStatus.AVAILABLE),
]


@pytest.mark.parametrize("from_status,to_status", INVALID_TRANSITION_PAIRS)
def test_invalid_transitions_raise(from_status, to_status):
    """Invalid transitions should raise InvalidTransitionError."""
    with pytest.raises(InvalidTransitionError):
        validate_transition(from_status, to_status)


@pytest.mark.parametrize("from_status,to_status", INVALID_TRANSITION_PAIRS)
def test_can_transition_returns_false_for_invalid(from_status, to_status):
    assert can_transition(from_status, to_status) is False


# ---------------------------------------------------------------------------
# Unknown status
# ---------------------------------------------------------------------------


def test_validate_unknown_from_status_raises():
    with pytest.raises(UnknownStatusError):
        validate_transition("totally_unknown", PhaseStatus.AVAILABLE)


def test_validate_unknown_to_status_raises():
    with pytest.raises(UnknownStatusError):
        validate_transition(PhaseStatus.PENDING, "totally_unknown")


# ---------------------------------------------------------------------------
# Terminal states
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [
    PhaseStatus.COMPLETED,
    PhaseStatus.FAILED,
    PhaseStatus.SKIPPED,
])
def test_terminal_states_are_terminal(status):
    assert is_terminal(status) is True


@pytest.mark.parametrize("status", [
    PhaseStatus.PENDING,
    PhaseStatus.BLOCKED,
    PhaseStatus.AVAILABLE,
    PhaseStatus.CLAIMED,
    PhaseStatus.RUNNING,
])
def test_non_terminal_states_are_not_terminal(status):
    assert is_terminal(status) is False


def test_terminal_states_have_no_outgoing_transitions():
    for status in PhaseStatus.TERMINAL:
        transitions = available_transitions(status)
        assert len(transitions) == 0, (
            f"Terminal status '{status}' should have no outgoing transitions, "
            f"but has: {transitions}"
        )


# ---------------------------------------------------------------------------
# is_claimable
# ---------------------------------------------------------------------------


def test_only_available_is_claimable():
    assert is_claimable(PhaseStatus.AVAILABLE) is True
    for status in PhaseStatus.ALL - {PhaseStatus.AVAILABLE}:
        assert is_claimable(status) is False, (
            f"Status '{status}' should not be claimable"
        )
