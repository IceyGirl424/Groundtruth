"""
claude_reasoning.py

The actual "brain" of Groundtruth. Takes the live fleet state collected from
all DroneAgents (via StatusResponse messages) plus a disruption description,
and asks Claude to produce a structured task-reassignment plan with a
human-readable rationale.

Kept as a standalone module so it can be unit-tested / iterated on without
touching the agent message-handling logic in coordinator_agent.py.
"""

import json
import os

import anthropic

# ---- Config ----
# Reads from environment variable ANTHROPIC_API_KEY by default.
# Set this in your shell before running coordinator_agent.py:
#   export ANTHROPIC_API_KEY="sk-ant-..."
client = anthropic.Anthropic()

MODEL = "claude-sonnet-4-6"

# ---- Arize AX tracing (optional, fully guarded) ----
# If ARIZE_SPACE_ID / ARIZE_API_KEY are set, register an OpenTelemetry tracer
# that exports to Arize and auto-instrument every Anthropic call. If anything is
# missing or fails, we log a warning and keep running with tracing disabled —
# tracing must never break the reasoning path.
_tracer = None
_tracer_provider = None
try:
    if os.environ.get("ARIZE_SPACE_ID") and os.environ.get("ARIZE_API_KEY"):
        from arize.otel import register as _arize_register
        from openinference.instrumentation.anthropic import AnthropicInstrumentor

        _tracer_provider = _arize_register(
            project_name=os.environ.get("ARIZE_PROJECT", "groundtruth"),
            log_to_console=False,
        )
        AnthropicInstrumentor().instrument(tracer_provider=_tracer_provider)
        _tracer = _tracer_provider.get_tracer("groundtruth.claude_reasoning")
        print("[claude_reasoning] Arize tracing ENABLED (project=groundtruth)")
    else:
        print("[claude_reasoning] ARIZE_SPACE_ID/ARIZE_API_KEY not set — tracing disabled")
except Exception as _exc:  # noqa: BLE001
    print(f"[claude_reasoning] Arize tracing unavailable: {type(_exc).__name__}: {_exc}")


def flush_traces() -> None:
    """Force-export any batched spans to Arize (call after a request so traces
    show up promptly during a demo). Safe no-op if tracing is disabled."""
    try:
        if _tracer_provider is not None:
            _tracer_provider.force_flush()
    except Exception as exc:  # noqa: BLE001
        print(f"[claude_reasoning] flush_traces failed: {type(exc).__name__}: {exc}")

SYSTEM_PROMPT = """You are the reasoning engine for Groundtruth, an autonomous \
coordination system for commercial drone inspection fleets (e.g. solar farm \
panel inspection).

You will be given:
1. The current live status of every drone in the fleet (battery %, position, \
current task, remaining task queue, whether it can safely accept more work).
2. A disruption event (e.g. a drone going offline, critical battery, a \
weather exclusion zone).

Your job is to produce a task reassignment plan that:
- NEVER assigns new tasks to a drone with can_accept_more = false.
- Prioritizes giving a disrupted drone's remaining tasks to the drone(s) with \
the most safe remaining capacity (highest battery, fewest existing tasks).
- Minimizes total disruption — don't reshuffle drones that don't need to be \
touched.
- Is conservative about safety. If no healthy drone can safely take on a \
task, say so explicitly rather than forcing an unsafe assignment.

You are advisory infrastructure assisting a human fleet manager, not an \
autonomous flight controller. Do not imply you are directly controlling \
aircraft movement, only proposing task/work assignments.

Respond ONLY with valid JSON, no markdown fences, no preamble, matching \
exactly this schema:

{
  "reassignments": [
    {"task": "<task name>", "from_drone": "<drone id or 'unassigned'>", "to_drone": "<drone id>", "reason": "<short reason>"}
  ],
  "unassignable_tasks": ["<task name>", ...],
  "rationale_summary": "<2-3 sentence plain-English explanation of the overall plan, suitable to show a human fleet manager>"
}
"""


def get_reassignment_plan(
    fleet_status: list[dict], disruption: str, memory_context: str = ""
) -> dict:
    """
    fleet_status: list of dicts, one per drone, shaped like:
        {
            "drone_id": "Drone-1",
            "battery_pct": 47,
            "position": "Grid-A1",
            "current_task": "Panel A1",
            "remaining_tasks": ["Panel A2", "Panel A3"],
            "can_accept_more": True
        }
    disruption: plain-English description of what happened, e.g.
        "Drone-2 has gone offline and its remaining tasks need reassignment."
    memory_context: optional text block describing similar PAST incidents
        (retrieved from vector memory) to inform the plan. Empty by default.

    Returns a dict matching the JSON schema in SYSTEM_PROMPT.
    Raises if Claude's response isn't valid JSON (caller should handle this
    and fall back to a safe "couldn't compute a plan" message rather than
    crashing the agent).
    """
    memory_block = ""
    if memory_context.strip():
        memory_block = (
            "For reference, here is relevant prior experience. Use it to inform "
            "your plan where applicable, but always prioritise the CURRENT fleet "
            f"state and safety rules:\n{memory_context}\n\n"
        )

    user_message = (
        f"Current fleet status:\n{json.dumps(fleet_status, indent=2)}\n\n"
        f"Disruption event:\n{disruption}\n\n"
        f"{memory_block}"
        f"Produce the reassignment plan."
    )

    def _invoke() -> dict:
        response = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        raw_text = response.content[0].text.strip()
        # Defensive: strip markdown fences if Claude adds them despite instructions
        if raw_text.startswith("```"):
            raw_text = raw_text.strip("`")
            if raw_text.startswith("json"):
                raw_text = raw_text[4:].strip()
        return json.loads(raw_text)

    # Wrap the reasoning in an Arize span (the inner Anthropic call is
    # auto-instrumented and nests under it). No-op if tracing is disabled.
    if _tracer is None:
        return _invoke()

    with _tracer.start_as_current_span("reassignment_plan") as span:
        span.set_attribute("openinference.span.kind", "CHAIN")
        span.set_attribute("input.value", user_message)
        span.set_attribute("groundtruth.disruption", disruption)
        span.set_attribute("groundtruth.num_drones", len(fleet_status))
        span.set_attribute("groundtruth.used_memory_context", bool(memory_context.strip()))
        plan = _invoke()
        span.set_attribute("output.value", json.dumps(plan))
        return plan


def format_plan_for_chat(plan: dict) -> str:
    """Turn the structured plan into a readable chat message for ASI:One."""
    lines = ["**Reassignment Plan**\n"]

    if plan.get("reassignments"):
        for r in plan["reassignments"]:
            lines.append(
                f"• {r['task']}: {r['from_drone']} → {r['to_drone']} "
                f"({r['reason']})"
            )
    else:
        lines.append("• No reassignments needed.")

    if plan.get("unassignable_tasks"):
        lines.append(f"\n⚠️ Could not safely assign: {', '.join(plan['unassignable_tasks'])}")

    if plan.get("rationale_summary"):
        lines.append(f"\n{plan['rationale_summary']}")

    return "\n".join(lines)


# ---- LLM-as-judge plan quality evaluator ----
JUDGE_SYSTEM_PROMPT = """You are a strict, impartial QA evaluator for an autonomous \
drone-fleet task-reassignment system. You are given the fleet status at decision \
time, the disruption, and the proposed reassignment plan. Score the plan ONLY on \
these two objective criteria:

1. battery_safety_respected: TRUE only if the plan assigns NO new task to any drone \
whose can_accept_more is false. If any reassignment's to_drone had can_accept_more \
= false, this is FALSE.

2. no_unnecessary_unassigned: TRUE only if the plan did NOT leave any task in \
unassignable_tasks when at least one drone with can_accept_more = true had capacity \
to take it. If a task was abandoned even though a safe drone could have taken it, \
this is FALSE. (If tasks were unassignable because genuinely no safe drone existed, \
this is TRUE.)

Respond ONLY with valid JSON, no markdown fences, exactly this schema:
{
  "battery_safety_respected": true,
  "battery_safety_explanation": "<one sentence citing specific drones/tasks>",
  "no_unnecessary_unassigned": true,
  "unassigned_explanation": "<one sentence citing specific drones/tasks>",
  "overall_score": <integer 1-5, where 5 = both criteria satisfied and plan is sensible>,
  "summary": "<one-sentence overall verdict>"
}
"""


def evaluate_plan(fleet_status: list[dict], disruption: str, plan: dict) -> dict:
    """LLM-as-judge: score a reassignment plan on battery-safety and whether it
    needlessly left tasks unassigned. Returns a dict matching JUDGE_SYSTEM_PROMPT's
    schema. Raises on invalid JSON (caller should guard)."""
    user_message = (
        f"Fleet status at decision time:\n{json.dumps(fleet_status, indent=2)}\n\n"
        f"Disruption:\n{disruption}\n\n"
        f"Proposed plan:\n{json.dumps(plan, indent=2)}\n\n"
        f"Evaluate the plan."
    )

    def _invoke() -> dict:
        response = client.messages.create(
            model=MODEL,
            max_tokens=512,
            system=JUDGE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        raw_text = response.content[0].text.strip()
        if raw_text.startswith("```"):
            raw_text = raw_text.strip("`")
            if raw_text.startswith("json"):
                raw_text = raw_text[4:].strip()
        return json.loads(raw_text)

    if _tracer is None:
        return _invoke()

    with _tracer.start_as_current_span("plan_evaluation") as span:
        span.set_attribute("openinference.span.kind", "EVALUATOR")
        span.set_attribute("input.value", user_message)
        judgment = _invoke()
        span.set_attribute("output.value", json.dumps(judgment))
        # Surface the scores as span attributes so they're filterable in Arize.
        for k in ("battery_safety_respected", "no_unnecessary_unassigned", "overall_score"):
            if k in judgment:
                span.set_attribute(f"eval.{k}", judgment[k])
        return judgment


def format_evaluation_for_chat(judgment: dict) -> str:
    """Render the judge's verdict as a readable chat message."""
    def mark(ok: bool) -> str:
        return "✅" if ok else "❌"

    return "\n".join([
        f"**Plan Quality (LLM-as-judge): {judgment.get('overall_score', '?')}/5**",
        f"{mark(judgment.get('battery_safety_respected'))} Battery-safety respected — "
        f"{judgment.get('battery_safety_explanation', '')}",
        f"{mark(judgment.get('no_unnecessary_unassigned'))} No needless unassigned tasks — "
        f"{judgment.get('unassigned_explanation', '')}",
        f"\n{judgment.get('summary', '')}",
    ])


if __name__ == "__main__":
    # Quick standalone test, run this file directly to sanity-check the
    # prompt/schema without needing the full agent stack running.
    test_fleet = [
        {
            "drone_id": "Drone-1",
            "battery_pct": 47,
            "position": "Grid-A1",
            "current_task": "Panel A1",
            "remaining_tasks": ["Panel A2", "Panel A3"],
            "can_accept_more": True,
        },
        {
            "drone_id": "Drone-2",
            "battery_pct": 12,
            "position": "Grid-B2",
            "current_task": "Panel B1",
            "remaining_tasks": ["Panel B2"],
            "can_accept_more": False,
        },
        {
            "drone_id": "Drone-3",
            "battery_pct": 91,
            "position": "Grid-C1",
            "current_task": "Panel C1",
            "remaining_tasks": ["Panel C2", "Panel C3", "Panel C4"],
            "can_accept_more": True,
        },
    ]
    test_disruption = "Drone-2's battery has dropped critical and it must return to base immediately. Its remaining task (Panel B2) needs reassignment."

    plan = get_reassignment_plan(test_fleet, test_disruption)
    print(json.dumps(plan, indent=2))
    print("\n--- formatted for chat ---\n")
    print(format_plan_for_chat(plan))

    print("\n--- LLM-as-judge evaluation ---\n")
    judgment = evaluate_plan(test_fleet, test_disruption, plan)
    print(json.dumps(judgment, indent=2))
    print()
    print(format_evaluation_for_chat(judgment))

    # push any batched spans to Arize before the process exits
    flush_traces()
