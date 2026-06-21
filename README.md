# Groundtruth — Swarm Tasking

**Advisory coordination infrastructure for commercial drone inspection fleets.**

When something goes wrong mid-mission — a drone's battery hits critical, a weather
cell rolls over part of a solar farm — Groundtruth renegotiates task assignments
across the fleet in real time and hands a human fleet manager a clear,
plain-English reassignment plan. It assists the operator; it does **not** fly the
aircraft. Authority over flight decisions stays with the human.

The entire workflow is demonstrable inside an **ASI:One** chat conversation: you
describe the disruption, and the coordinator queries the live fleet, reasons over
its state with Claude, recalls how similar past incidents were resolved, and
replies with a scored plan.

---

## Why this matters

Commercial drone operators (solar/utility inspection, agricultural survey,
infrastructure, post-disaster mapping) are scaling from single-drone to
multi-drone operations as BVLOS waivers expand — but coordination today is
usually a person watching several screens. Groundtruth is fleet-coordination
software, priced per-drone-per-month like fleet management in trucking/logistics.

---

## Architecture

```
                ASI:One chat  (the human fleet manager)
                       │  "Drone-2 battery critical, reassign its tasks"
                       ▼
          ┌─────────────────────────────┐
          │      SwarmCoordinator        │  uAgents + Agent Chat Protocol
          │        (mailbox agent)       │  → discoverable on Agentverse / ASI:One
          └─────────────────────────────┘
            │ 1. fan-out StatusRequest        ▲ 4. combined fleet status,
            ▼    to every drone               │    plan, and quality score
   ┌──────────┐ ┌──────────┐ ┌──────────┐
   │ Drone-1  │ │ Drone-2  │ │ Drone-3  │     each: battery %, position,
   │  :8001   │ │  :8002   │ │  :8003   │     current task, queue, capacity
   └──────────┘ └──────────┘ └──────────┘
                       │ 2. collect StatusResponses
                       ▼
          ┌─────────────────────────────┐
          │   Claude reasoning + memory  │
          │                              │
          │  • RedisVL vector memory ───▶ recall similar past incidents
          │  • Claude (Sonnet 4.6) ─────▶ produce reassignment plan
          │  • LLM-as-judge ────────────▶ score plan quality
          │  • Arize ───────────────────▶ trace every LLM call
          └─────────────────────────────┘
                       │ 3. store resolved incident back into memory
```

### Agents
- **`SwarmCoordinator`** (`coordinator_agent.py`) — entry point. Implements the
  Agent Chat Protocol, runs as a mailbox agent (no public endpoint needed),
  orchestrates everything below.
  Address: `agent1qw7awftrnyz2haxmwc7frd0u2mweelukfcueeer6lg2xcqq0mvef608jgmm`
- **`DroneAgent`** (`drone_agent.py`) — one process per drone, holds its own state
  (battery, position, task queue), answers status queries, accepts/rejects task
  assignments under a battery-safety rule. Launch many with distinct `--id`/`--port`.
- **Shared message schemas** (`messages.py`) — typed uAgents `Model`s exchanged
  between coordinator and drones (routed by schema digest, so both sides must
  match exactly).

### Reasoning, memory, observability
- **`claude_reasoning.py`** — builds the prompt from live fleet state + the
  disruption (+ recalled context), calls Claude for a structured plan, and
  contains the LLM-as-judge evaluator. All Claude calls are traced to Arize.
- **`agent_memory.py`** — RedisVL vector index over incident embeddings
  (`all-MiniLM-L6-v2`, local, 384-dim). Stores each resolved incident; retrieves
  semantically similar past ones to inform new decisions.

---

## Sponsor technology

| Sponsor | How it's used | Prize track |
|---|---|---|
| **Fetch.ai** (uAgents, Agent Chat Protocol, Agentverse, ASI:One) | All agents are uAgents; coordinator is registered on Agentverse and usable directly from ASI:One; agent-to-agent messaging uses typed protocols | Fetch.ai / ASI:One |
| **Anthropic Claude** (`claude-sonnet-4-6`) | The reasoning engine that produces reassignment plans, and the LLM-as-judge that scores them | — |
| **Redis** (RedisVL + vector search) | Agent memory: incidents embedded and stored in a Redis vector index; semantic similarity search retrieves relevant past incidents as context for new decisions | Redis (Agent Memory / vector search / context retrieval) |
| **Arize AX** (OpenTelemetry + OpenInference) | Distributed tracing of every Claude call (reasoning + judge), with plan-quality evaluation scores attached as span attributes | Arize |

---

## Setup

Requires **Python 3.11+**.

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Create your secrets file from the template and fill it in
cp .env.example .env
#    Edit .env with your real ANTHROPIC_API_KEY, REDIS_URL, ARIZE_SPACE_ID,
#    ARIZE_API_KEY, and AGENTVERSE_API_TOKEN.
```

### One-time: claim the coordinator's mailbox
The coordinator uses a mailbox so it's reachable through ASI:One without a public
endpoint. The first time you run it, claim the mailbox:

1. Start the coordinator (`./run.sh`).
2. Open the inspector URL printed in `logs/coordinator.log`
   (`https://agentverse.ai/inspect/?uri=...&address=...`) in **Chrome**.
3. Sign in to Agentverse and click **Connect / Create Mailbox**.
4. Restart (`./run.sh`) — the `Agent mailbox not found` warning disappears.

---

## Run

```bash
./run.sh
```

This loads secrets from `.env`, launches the coordinator (port 8000) and all
three drones (ports 8001–8003) in the background with logs under `logs/`, and
prints a summary of what's running.

Stop everything:

```bash
pkill -f coordinator_agent.py ; pkill -f drone_agent.py
```

---

## Try it

**In ASI:One (the primary workflow):** find the `SwarmCoordinator` agent and send
it a disruption, e.g. *"Drone-2's battery has dropped critical, reassign its
remaining tasks."* You'll get back the live fleet status, a reassignment plan with
rationale, and an LLM-as-judge quality score — and a 🧠 note if a similar past
incident was recalled from memory.

**Locally (no UI needed):** `chat_test_client.py` simulates an ASI:One user.

```bash
python3 chat_test_client.py --scenario 1   # battery critical (wording A)
python3 chat_test_client.py --scenario 2   # battery critical (wording B — semantically matches 1)
python3 chat_test_client.py --scenario 3   # weather exclusion zone (different scenario)
python3 chat_test_client.py --message "custom disruption text"
```

Run scenario 1 then 2 to see semantic memory recall: scenario 2 is worded
differently but recalls scenario 1 via vector similarity. Scenario 3 (weather) is
correctly judged *not* similar and reasoned from scratch.

---

## Repository layout

```
coordinator_agent.py   SwarmCoordinator: chat handler, fan-out, memory, reasoning, eval
drone_agent.py         DroneAgent: per-drone state + status/assignment protocol
messages.py            Shared typed message schemas (coordinator <-> drones)
claude_reasoning.py    Claude reasoning, LLM-as-judge, Arize tracing
agent_memory.py        RedisVL vector memory (store / retrieve similar incidents)
chat_test_client.py    Local test client (simulates an ASI:One user)
run.sh                 Launch the full stack from .env
requirements.txt       Python dependencies
.env.example           Template for secrets (copy to .env)
```

---

## Roadmap
- Real disruption detection (a `HazardAgent` monitoring battery/weather thresholds
  and proactively notifying the coordinator) instead of disruptions described in chat.
- Live map visualization driven off the Redis incident/message log (bonus polish).
