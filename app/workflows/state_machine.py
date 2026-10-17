"""Server-enforced task lifecycle.

CREATED -> ASSIGNED -> ACKNOWLEDGED -> IN_PROGRESS -> COMPLETED
        -> PENDING_VERIFICATION -> VERIFIED
REOPENED returns work to IN_PROGRESS. BLOCKED can be entered from active
states and returns to the prior state on unblock (tracked via audit events).
Anything not listed here is rejected; the client cannot set arbitrary states.
"""
from __future__ import annotations

TRANSITIONS: dict[str, set[str]] = {
    "CREATED": {"ASSIGNED"},
    "ASSIGNED": {"ACKNOWLEDGED", "BLOCKED"},
    "ACKNOWLEDGED": {"IN_PROGRESS", "BLOCKED"},
    "IN_PROGRESS": {"COMPLETED", "BLOCKED"},
    "BLOCKED": {"ASSIGNED", "ACKNOWLEDGED", "IN_PROGRESS"},
    "COMPLETED": {"PENDING_VERIFICATION"},
    "PENDING_VERIFICATION": {"VERIFIED", "REOPENED", "REJECTED"},
    "VERIFIED": {"REOPENED"},
    "REOPENED": {"IN_PROGRESS"},
    "REJECTED": {"REOPENED"},
}

# Action endpoint -> target state
ACTION_TARGETS = {
    "acknowledge": "ACKNOWLEDGED",
    "start": "IN_PROGRESS",
    "complete": "COMPLETED",
    "request-verification": "PENDING_VERIFICATION",
    "verify": "VERIFIED",
    "reopen": "REOPENED",
    "reject": "REJECTED",
}

TERMINAL_OR_BEYOND = {"COMPLETED", "PENDING_VERIFICATION", "VERIFIED"}


def can_transition(from_status: str, to_status: str) -> bool:
    return to_status in TRANSITIONS.get(from_status, set())
