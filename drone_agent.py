"""
DroneAgent
Represents a single drone in the fleet. Holds its own state (battery,
position, current task queue) and responds to status queries from the
SwarmCoordinator agent.

Run one process per drone, e.g.:
    python drone_agent.py --id 1 --battery 87 --tasks "Panel A1,Panel A2,Panel A3"
    python drone_agent.py --id 2 --battery 64 --tasks "Panel B1,Panel B2"
    python drone_agent.py --id 3 --battery 91 --tasks "Panel C1,Panel C2,Panel C3,Panel C4"

Each instance needs a UNIQUE seed (derived from --id below) so it gets its
own stable agent address on Agentverse.
"""

import argparse
from datetime import datetime
from uuid import uuid4

from uagents import Agent, Context, Protocol

# ---- Message schemas (agent-to-agent, not chat protocol) ----
# These typed Models are defined once in messages.py and shared with the
# Coordinator so both sides use byte-identical schemas (uAgents routes by
# schema digest). Keeping them typed is what makes this "real" agent
# communication, not just string passing.
from messages import StatusRequest, StatusResponse, TaskAssignment, TaskAck


# ---- CLI args so we can launch multiple distinct drones from one file ----
parser = argparse.ArgumentParser()
parser.add_argument("--id", type=str, required=True, help="Drone identifier, e.g. 1, 2, 3")
parser.add_argument("--battery", type=int, default=100, help="Starting battery percentage")
parser.add_argument("--position", type=str, default="Grid-A1", help="Starting position")
parser.add_argument(
    "--tasks",
    type=str,
    default="Panel 1,Panel 2,Panel 3",
    help="Comma-separated initial task list",
)
parser.add_argument(
    "--port",
    type=int,
    default=8001,
    help="Local port for this drone's server (must be unique per running agent)",
)
args = parser.parse_args()

DRONE_ID = f"Drone-{args.id}"

# ---- Mutable in-memory state for this drone ----
# (Swap this for Redis later — for now it's local to the process, which is
# fine since each drone IS its own process.)
state = {
    "battery_pct": args.battery,
    "position": args.position,
    "remaining_tasks": [t.strip() for t in args.tasks.split(",") if t.strip()],
    "current_task": None,
}
if state["remaining_tasks"]:
    state["current_task"] = state["remaining_tasks"].pop(0)


def can_accept_more_tasks() -> bool:
    """Simple battery-safety rule: don't accept new tasks below 30% battery."""
    return state["battery_pct"] >= 30


# ---- Agent setup ----
drone_agent = Agent(
    name=DRONE_ID,
    seed=f"groundtruth_drone_seed_{args.id}",  # unique per drone, keeps address stable across restarts
    port=args.port,
    endpoint=[f"http://127.0.0.1:{args.port}/submit"],
    mailbox=True,
)

drone_proto = Protocol(name="DroneStatusProtocol", version="1.0")


@drone_proto.on_message(model=StatusRequest, replies=StatusResponse)
async def handle_status_request(ctx: Context, sender: str, msg: StatusRequest):
    ctx.logger.info(f"[{DRONE_ID}] Status requested by {sender}")

    response = StatusResponse(
        drone_id=DRONE_ID,
        battery_pct=state["battery_pct"],
        position=state["position"],
        current_task=state["current_task"] or "idle",
        remaining_tasks=state["remaining_tasks"],
        can_accept_more=can_accept_more_tasks(),
    )
    await ctx.send(sender, response)


@drone_proto.on_message(model=TaskAssignment, replies=TaskAck)
async def handle_task_assignment(ctx: Context, sender: str, msg: TaskAssignment):
    ctx.logger.info(f"[{DRONE_ID}] Task assignment from {sender}: {msg.task} (reason: {msg.reason})")

    if can_accept_more_tasks():
        state["remaining_tasks"].append(msg.task)
        ack = TaskAck(drone_id=DRONE_ID, accepted=True, note=f"Added to queue. Battery: {state['battery_pct']}%")
    else:
        ack = TaskAck(
            drone_id=DRONE_ID,
            accepted=False,
            note=f"Rejected — battery too low ({state['battery_pct']}%)",
        )

    await ctx.send(sender, ack)


# ---- Simple background "flight" simulation: battery drains over time ----
# Gentle drain (1% every 30s) so a fresh fleet stays usable for testing without
# constant restarts.
@drone_agent.on_interval(period=30.0)
async def simulate_flight(ctx: Context):
    if state["battery_pct"] > 0:
        state["battery_pct"] = max(0, state["battery_pct"] - 1)
        ctx.logger.info(f"[{DRONE_ID}] Battery now at {state['battery_pct']}%")


drone_agent.include(drone_proto, publish_manifest=True)


if __name__ == "__main__":
    print(f"Starting {DRONE_ID}...")
    print(f"Agent address: {drone_agent.address}")
    print(f"Initial state: {state}")
    drone_agent.run()
