#!/usr/bin/env bash
#
# run.sh — launch the full Groundtruth stack (SwarmCoordinator + 3 DroneAgents)
# with all required secrets loaded from a local .env file.
#
# Usage:
#   cp .env.example .env     # then fill in your real values
#   ./run.sh
#
set -euo pipefail
cd "$(dirname "$0")"

# ---- Load secrets from .env ----
if [ ! -f .env ]; then
  echo "ERROR: .env not found."
  echo "Create it from the template:  cp .env.example .env   (then fill in your values)"
  exit 1
fi
set -a
# shellcheck disable=SC1091
source .env
set +a

# ---- Validate required secrets ----
required=(ANTHROPIC_API_KEY REDIS_URL ARIZE_SPACE_ID ARIZE_API_KEY)
missing=()
for v in "${required[@]}"; do
  [ -n "${!v:-}" ] || missing+=("$v")
done
if [ "${#missing[@]}" -gt 0 ]; then
  echo "ERROR: missing required values in .env: ${missing[*]}"
  exit 1
fi
# AGENTVERSE_API_TOKEN is injected for completeness but not currently consumed by
# the agents (the coordinator's mailbox is claimed interactively via Agentverse).
[ -n "${AGENTVERSE_API_TOKEN:-}" ] || echo "WARN: AGENTVERSE_API_TOKEN not set (optional)."

PY="${PYTHON:-python3}"
mkdir -p logs

echo "Starting Groundtruth stack..."

# ---- Launch the 3 drones (each its own port + stable seed via --id) ----
nohup "$PY" drone_agent.py --id 1 --battery 87 --tasks "Panel A1,Panel A2,Panel A3"            --port 8001 > logs/drone1.log 2>&1 &
D1=$!
nohup "$PY" drone_agent.py --id 2 --battery 64 --tasks "Panel B1,Panel B2"                     --port 8002 > logs/drone2.log 2>&1 &
D2=$!
nohup "$PY" drone_agent.py --id 3 --battery 91 --tasks "Panel C1,Panel C2,Panel C3,Panel C4"   --port 8003 > logs/drone3.log 2>&1 &
D3=$!

# ---- Launch the coordinator ----
nohup "$PY" coordinator_agent.py > logs/coordinator.log 2>&1 &
C=$!

# ---- Wait for each agent's HTTP server to come up ----
echo "Waiting for agents to start..."
for log in logs/drone1.log logs/drone2.log logs/drone3.log logs/coordinator.log; do
  for _ in $(seq 1 40); do
    if grep -q "Starting server" "$log" 2>/dev/null; then break; fi
    sleep 0.5
  done
done
sleep 1

# ---- Summary ----
printf '\n%s\n' "================== Groundtruth is running =================="
printf '%-18s %-6s %-8s %s\n' "COMPONENT" "PORT" "PID" "LOG"
printf '%-18s %-6s %-8s %s\n' "SwarmCoordinator" "8000" "$C"  "logs/coordinator.log"
printf '%-18s %-6s %-8s %s\n' "Drone-1"          "8001" "$D1" "logs/drone1.log"
printf '%-18s %-6s %-8s %s\n' "Drone-2"          "8002" "$D2" "logs/drone2.log"
printf '%-18s %-6s %-8s %s\n' "Drone-3"          "8003" "$D3" "logs/drone3.log"
printf '%s\n' "==========================================================="
printf '\n  Memory   : Redis vector index "groundtruth_incidents"\n'
printf   '  Tracing  : Arize project "groundtruth" (https://app.arize.com)\n'
printf '\n  Tail logs: tail -f logs/coordinator.log\n'
printf   '  Test     : %s chat_test_client.py --scenario 1   (local)\n' "$PY"
printf   '             ...or chat the SwarmCoordinator directly in ASI:One.\n'
printf   '  Stop all : pkill -f coordinator_agent.py ; pkill -f drone_agent.py\n\n'
