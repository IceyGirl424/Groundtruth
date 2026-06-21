"""
Shared message schemas exchanged between the SwarmCoordinator and DroneAgents.

These are kept in ONE module so both sides use byte-identical typed Models.
uAgents routes messages by each Model's schema digest, so the field names and
types MUST match exactly on both ends — defining them once here guarantees that
and prevents silent delivery failures from schema drift.
"""

from uagents import Model


class StatusRequest(Model):
    """Sent by the Coordinator to ask a drone for its current state."""
    requester: str  # who's asking (Coordinator's address), for logging


class StatusResponse(Model):
    """Sent by a Drone back to the Coordinator with its current state."""
    drone_id: str
    battery_pct: int
    position: str          # simple string for now, e.g. "Grid-B4"
    current_task: str
    remaining_tasks: list[str]
    can_accept_more: bool  # true if battery/capacity allows taking on extra tasks


class TaskAssignment(Model):
    """Sent by the Coordinator to a Drone to assign it a new task."""
    task: str
    reason: str  # human-readable rationale, so the drone (and logs) know why


class TaskAck(Model):
    """Drone confirms it accepted (or rejected) a task assignment."""
    drone_id: str
    accepted: bool
    note: str = ""
