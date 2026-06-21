# Project: Swarm Tasking

## What we're building
An autonomous coordination system for drone fleets doing inspection/survey
work (e.g. solar farm panel inspection). When a disruption happens mid-mission
(a drone's battery drops critical, a weather cell rolls over part of the
site), the fleet renegotiates task assignments in real time instead of
waiting for a human to manually replan.

Each drone is modeled as an independent agent. A Coordinator agent receives
disruption events, queries the Drone agents for their current status
(battery, position, task queue), and uses Claude to reason over their
responses and produce a reassignment plan with a plain-English rationale.

This is pitched as advisory coordination infrastructure that assists human
fleet managers, NOT autonomous flight control. Important framing, don't let
any copy/pitch text imply we're replacing human authority over flight
decisions.

## Why this matters / who pays
Commercial drone fleet operators (solar/utility inspection, agricultural
survey, infrastructure inspection, post-disaster survey) are scaling from
single-drone to multi-drone operations as BVLOS waivers expand, but have no
real coordination software, just a person watching multiple screens. We sell
fleet-coordination software priced per-drone-per-month, similar to fleet
management software in trucking/logistics.

## Architecture
- **Agent framework**: uAgents (Fetch.ai), using the Agent Chat Protocol
  (chat_protocol_spec) so agents are discoverable/usable through ASI:One.
- **Agent types** (build in this order):
  1. `SwarmCoordinator` — entry point, receives chat messages via ASI:One,
     orchestrates everything else. (This one exists already, see
     coordinator_agent.py)
  2. `DroneAgent` (one instance per simulated drone) — holds its own state
     (battery %, position, current task queue), responds to status queries
     from the Coordinator.
  3. (Stretch) `HazardAgent` — independently monitors simulated
     weather/battery thresholds and proactively notifies the Coordinator of
     disruptions, rather than disruptions only being manually triggered.
- **Claude**: called from inside the Coordinator's message handler. Input:
  current fleet state (from querying Drone agents) + the disruption event.
  Output: a reassignment plan (which drone takes which task) plus a
  human-readable rationale.
- **Redis**: stores live fleet state and logs every agent-to-agent message
  exchanged (timestamp, from, to, content). This log is both our audit trail
  and, later, the data source for an optional live map visualization.
- **Arize**: traces the Claude reasoning calls inside the Coordinator, plus
  an evaluator that scores plan quality (did it respect battery-safety
  margins, did it maximize task coverage).
- **Devin**: used in parallel for self-contained, low-ambiguity tasks only
  (e.g. the drone state simulator / Redis read-write layer). NOT used for
  the Claude reasoning logic or core agent message handling, that stays
  hand-written so we can explain it fluently to judges.

## Mandatory requirements for the Fetch.ai / ASI:One track (non-negotiable)
- At least one agent registered on Agentverse.
- Agent Chat Protocol implemented.
- Agent discoverable and directly usable through ASI:One.
- The PRIMARY workflow must be demonstrable entirely within an ASI:One chat
  conversation, NOT requiring a custom frontend. (A map/UI can exist as a
  bonus for general hackathon judging, but is not the Fetch.ai deliverable.)
- Public GitHub repo with run instructions.
- Final submission needs: a public ASI:One shared chat session URL, the
  Agentverse agent profile URL(s), the GitHub repo URL, and a 3-5 min demo
  video.

## Fetch.ai judging weights (prioritize accordingly)
- Functionality & Technical Implementation: 25%
- Use of Fetch.ai Technology (must be central, not bolted on): 25%
- Innovation & Creativity: 20%
- Real-World Impact & Usefulness: 20%
- UX & Presentation: 10% (lowest weight, don't over-invest in UI for this
  track specifically)

## Current status
- coordinator_agent.py exists, implements the chat protocol, runs with
  mailbox=True (no public endpoint/ngrok needed).
- Not yet confirmed running/registered on Agentverse.
- DroneAgent not yet built.
- Claude reasoning not yet wired in (currently a placeholder response).
- Redis not yet connected.
- Arize not yet connected.

## What NOT to do
- Don't build the live map/visualization before the chat-only workflow is
  fully working end to end, the map is bonus polish, not core.
- Don't add Simulang/computer-use automation, it was scoped out as
  too risky to get reliable solo in the time available.
- Don't let Devin touch the Claude reasoning prompt or core agent logic.
