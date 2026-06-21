"""
SwarmCoordinator Agent
Based on Fetch.ai's official uAgents Chat Protocol quick-start.
This is the entry point agent that ASI:One will route messages to.
"""

import asyncio
from datetime import datetime
from uuid import uuid4

from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    EndSessionContent,
    StartSessionContent,
    TextContent,
    chat_protocol_spec,
)

from messages import StatusRequest, StatusResponse
from claude_reasoning import (
    get_reassignment_plan,
    format_plan_for_chat,
    evaluate_plan,
    format_evaluation_for_chat,
    flush_traces,
)
from agent_memory import retrieve_similar, store_incident, format_memory_context

# ---- Fleet config ----
# Stable addresses of each drone (derived from their seeds
# "groundtruth_drone_seed_<id>"). For now hard-coded; later this becomes a
# dynamic fleet registry.
DRONE_ADDRESSES = [
    "agent1q2eprmflcm6a0afrrzk78zeddkl9uylkhk24q2kjcs2f7u5p272vumk2aej",  # Drone-1 (:8001)
    "agent1q0v029ge6accyy8cafnea50lrvxp9j0xshyzj8c93mznancvrausk9yv5r4",  # Drone-2 (:8002)
    "agent1q2zwq95qepz69tgy4yu4eqhevuwk84075z8vk2qcnuwfhea3dnm8w2lqnh2",  # Drone-3 (:8003)
]

# How long to wait for drones to reply before relaying a combined summary.
# Must comfortably exceed the mailbox poll interval (1.0s) so replies routed
# via the coordinator's mailbox land inside the window — 2.0s was too tight to
# reliably catch all drones, 3.0s clears ~3 polls.
STATUS_COLLECT_SECONDS = 3.0

# Fallback disruption if a chat message somehow arrives with no text. Normally
# the disruption is taken straight from the user's chat message (that's how an
# ASI:One user describes what happened).
DEFAULT_DISRUPTION = "A drone has gone offline; reassign its remaining tasks"

# Minimum cosine similarity for a recalled past incident to be treated as a
# genuine match and fed into the prompt as context. Measured separation:
# reworded battery incidents ~0.70, battery-vs-weather ~0.35-0.42, so 0.5 cleanly
# splits "same situation, different words" from "genuinely different scenario".
MEMORY_SIMILARITY_THRESHOLD = 0.5

# ---- Agent setup ----
# Using mailbox=True means this agent connects to Agentverse via mailbox,
# no public endpoint / ngrok needed.
# handle_messages_concurrently=True lets the chat handler await a collection
# window while StatusResponse handlers run concurrently and populate the buffer.
agent = Agent(
    name="SwarmCoordinator",
    seed="swarm_coordinator_seed_phrase_change_this",  # any unique string, keeps your address stable across restarts
    mailbox=True,
    handle_messages_concurrently=True,
)

# In-flight buffer of drone replies for the current query. Single process +
# asyncio (no true parallelism), so a module-level list is safe to append to
# from the concurrent StatusResponse handlers.
collected_statuses: list[StatusResponse] = []

# ---- Chat protocol setup ----
chat_proto = Protocol(spec=chat_protocol_spec)


def create_text_chat(text: str, end_session: bool = False) -> ChatMessage:
    content = [TextContent(type="text", text=text)]
    if end_session:
        content.append(EndSessionContent(type="end-session"))
    return ChatMessage(
        timestamp=datetime.utcnow(),
        msg_id=uuid4(),
        content=content,
    )


def build_fleet_summary(statuses: list[StatusResponse]) -> str:
    """Format collected drone statuses into a single combined chat reply."""
    if not statuses:
        return "No drones responded within the collection window."

    # Stable ordering by drone_id so the summary reads consistently.
    statuses = sorted(statuses, key=lambda s: s.drone_id)
    lines = [f"**Fleet status — {len(statuses)} drone(s) reporting**", ""]
    for s in statuses:
        remaining = ", ".join(s.remaining_tasks) if s.remaining_tasks else "none"
        lines.append(
            f"**{s.drone_id}** — battery {s.battery_pct}%, at {s.position}\n"
            f"  • Current task: {s.current_task}\n"
            f"  • Remaining: {remaining}\n"
            f"  • Can accept more: {'yes' if s.can_accept_more else 'no'}"
        )
    return "\n".join(lines)


@chat_proto.on_message(ChatMessage)
async def handle_message(ctx: Context, sender: str, msg: ChatMessage):
    ctx.logger.info(f"Received message from {sender}")

    # Always acknowledge receipt
    await ctx.send(
        sender,
        ChatAcknowledgement(timestamp=datetime.utcnow(), acknowledged_msg_id=msg.msg_id),
    )

    for item in msg.content:
        if isinstance(item, StartSessionContent):
            ctx.logger.info(f"Session started with {sender}")

        elif isinstance(item, TextContent):
            ctx.logger.info(f"Text message from {sender}: {item.text}")

            # The user's chat message IS the disruption description.
            disruption = item.text.strip() or DEFAULT_DISRUPTION

            # ---- Step 1: query the whole fleet for live status ----
            # Start a fresh collection window, then fan out a StatusRequest to
            # every drone. Replies land asynchronously in handle_status_response
            # and append to collected_statuses.
            collected_statuses.clear()
            ctx.logger.info(f"Fanning out StatusRequest to {len(DRONE_ADDRESSES)} drones...")
            for addr in DRONE_ADDRESSES:
                await ctx.send(addr, StatusRequest(requester=str(agent.address)))

            await ctx.send(
                sender,
                create_text_chat(
                    f"Querying the fleet ({len(DRONE_ADDRESSES)} drones) for live status…"
                ),
            )

            # Wait briefly for replies to arrive, then relay a combined summary.
            await asyncio.sleep(STATUS_COLLECT_SECONDS)

            replies = list(collected_statuses)
            ctx.logger.info(
                f"Collected {len(replies)}/{len(DRONE_ADDRESSES)} drone replies "
                f"within {STATUS_COLLECT_SECONDS}s"
            )
            await ctx.send(sender, create_text_chat(build_fleet_summary(replies)))

            # ---- Step 2: ask Claude for a reassignment plan ----
            # Convert the collected StatusResponse objects into the plain-dict
            # shape claude_reasoning.py expects.
            fleet_status = [
                {
                    "drone_id": s.drone_id,
                    "battery_pct": s.battery_pct,
                    "position": s.position,
                    "current_task": s.current_task,
                    "remaining_tasks": s.remaining_tasks,
                    "can_accept_more": s.can_accept_more,
                }
                for s in replies
            ]

            if fleet_status:
                # ---- Step 2a: recall similar past incidents from vector memory ----
                # All memory calls run off the event loop and are guarded so a
                # Redis/embedding hiccup never crashes the agent. We only feed a
                # recalled incident into the prompt if it's semantically similar
                # ENOUGH (>= threshold), so a genuinely different scenario doesn't
                # get spuriously "matched" to an unrelated past incident.
                memory_context = ""
                try:
                    matches = await asyncio.to_thread(retrieve_similar, disruption, 2)

                    # Annotate each candidate with cosine similarity (1 - distance).
                    for m in matches:
                        try:
                            m["similarity"] = 1.0 - float(m["vector_distance"])
                        except Exception:
                            m["similarity"] = 0.0
                        ctx.logger.info(
                            f"Vector memory candidate: similarity={m['similarity']:.3f} "
                            f"disruption='{m.get('disruption', '')[:70]}'"
                        )

                    strong = [m for m in matches if m["similarity"] >= MEMORY_SIMILARITY_THRESHOLD]
                    if strong:
                        memory_context = format_memory_context(strong)
                        top = max(m["similarity"] for m in strong)
                        ctx.logger.info(
                            f"Vector memory: {len(strong)} strong match(es) "
                            f"(>= {MEMORY_SIMILARITY_THRESHOLD}), top similarity {top:.3f}"
                        )
                        await ctx.send(
                            sender,
                            create_text_chat(
                                f"🧠 Recalled {len(strong)} similar past incident(s) "
                                f"(top similarity {top:.2f}) — using them as context for this plan."
                            ),
                        )
                    elif matches:
                        nearest = max(m["similarity"] for m in matches)
                        ctx.logger.info(
                            f"Vector memory: nearest similarity {nearest:.3f} below "
                            f"threshold {MEMORY_SIMILARITY_THRESHOLD} — reasoning fresh"
                        )
                        await ctx.send(
                            sender,
                            create_text_chat(
                                f"🧠 No strongly similar past incident (nearest similarity "
                                f"{nearest:.2f} < {MEMORY_SIMILARITY_THRESHOLD}) — reasoning from scratch."
                            ),
                        )
                    else:
                        ctx.logger.info("Vector memory: empty (cold start)")
                except Exception:
                    ctx.logger.exception("Memory retrieval failed; proceeding without context")

                # ---- Step 2b: ask Claude for a reassignment plan ----
                ctx.logger.info(f"Asking Claude for a reassignment plan. Disruption: {disruption}")
                plan = None
                try:
                    # Run the blocking Anthropic SDK call off the event loop so
                    # mailbox polling / other handlers stay responsive.
                    plan = await asyncio.to_thread(
                        get_reassignment_plan, fleet_status, disruption, memory_context
                    )
                    plan_text = format_plan_for_chat(plan)
                except Exception as exc:
                    # A malformed JSON response (or API error) must not crash the
                    # agent — fall back to a plain message.
                    ctx.logger.exception("Claude reasoning failed")
                    plan_text = (
                        "⚠️ Could not compute a reassignment plan right now "
                        f"({type(exc).__name__}: {exc})."
                    )
                await ctx.send(sender, create_text_chat(plan_text))

                # ---- Step 2c: LLM-as-judge scores the plan (traced in Arize) ----
                if plan is not None:
                    try:
                        judgment = await asyncio.to_thread(
                            evaluate_plan, fleet_status, disruption, plan
                        )
                        ctx.logger.info(
                            f"Plan evaluation: score={judgment.get('overall_score')}/5, "
                            f"battery_safety={judgment.get('battery_safety_respected')}, "
                            f"no_unnecessary_unassigned={judgment.get('no_unnecessary_unassigned')}"
                        )
                        await ctx.send(sender, create_text_chat(format_evaluation_for_chat(judgment)))
                    except Exception:
                        ctx.logger.exception("Plan evaluation failed")

                # ---- Step 2d: store this resolved incident back into memory ----
                if plan is not None:
                    try:
                        key = await asyncio.to_thread(store_incident, disruption, plan)
                        ctx.logger.info(f"Vector memory: stored incident as {key}")
                    except Exception:
                        ctx.logger.exception("Storing incident in memory failed")

                # Push batched spans to Arize so the trace shows up promptly.
                await asyncio.to_thread(flush_traces)

        elif isinstance(item, EndSessionContent):
            ctx.logger.info(f"Session ended with {sender}")

        else:
            ctx.logger.info(f"Received unexpected content type from {sender}")


@chat_proto.on_message(ChatAcknowledgement)
async def handle_acknowledgement(ctx: Context, sender: str, msg: ChatAcknowledgement):
    ctx.logger.info(f"Received acknowledgement from {sender} for {msg.acknowledged_msg_id}")


# ---- Drone status query handling (agent-to-agent, not chat protocol) ----
@agent.on_message(model=StatusResponse)
async def handle_status_response(ctx: Context, sender: str, msg: StatusResponse):
    """Collect a drone's live state into the in-flight buffer.

    The chat handler waits a short window, then reads collected_statuses and
    relays a single combined summary, so we don't reply per-drone here.
    """
    ctx.logger.info(
        f"Status from {msg.drone_id}: battery={msg.battery_pct}%, position={msg.position}, "
        f"current_task={msg.current_task}, remaining={msg.remaining_tasks}, "
        f"can_accept_more={msg.can_accept_more}"
    )
    collected_statuses.append(msg)


# Register the protocol and publish the manifest so Agentverse/ASI:One can discover it
agent.include(chat_proto, publish_manifest=True)


if __name__ == "__main__":
    print(f"Starting SwarmCoordinator...")
    print(f"Agent address: {agent.address}")
    agent.run()
