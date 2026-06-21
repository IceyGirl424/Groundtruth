"""
THROWAWAY local test client — NOT part of the product.
Simulates what ASI:One does: sends a chat message (a disruption scenario) to the
SwarmCoordinator and prints whatever chat replies come back. Used to verify the
full pipeline (query → collect → memory recall → Claude → reply) without the
ASI:One UI.

Run while coordinator_agent.py (8000) and the drones are up. Pick a scenario:
    python chat_test_client.py --scenario 1   # battery-critical (wording A)
    python chat_test_client.py --scenario 2   # battery-critical (wording B, semantically similar to 1)
    python chat_test_client.py --scenario 3   # weather exclusion zone (different scenario)
    python chat_test_client.py --message "custom disruption text"
"""

import argparse
from datetime import datetime
from uuid import uuid4

from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    TextContent,
    chat_protocol_spec,
)

COORDINATOR_ADDRESS = "agent1qw7awftrnyz2haxmwc7frd0u2mweelukfcueeer6lg2xcqq0mvef608jgmm"

# Disruption scenarios. 1 and 2 describe the SAME battery-critical situation in
# different words (should match each other in memory via semantic similarity);
# 3 is a genuinely different scenario (should NOT strongly match 1 or 2).
SCENARIOS = {
    1: "Drone-2's battery has dropped critical, reassign its remaining tasks",
    2: "Drone-2 is running dangerously low on power and can't finish its route, "
       "hand its remaining inspections to another drone",
    3: "A weather cell is moving into Grid-C creating a no-fly exclusion zone; "
       "pull the affected drone and redistribute its tasks",
}

parser = argparse.ArgumentParser()
parser.add_argument("--scenario", type=int, choices=sorted(SCENARIOS), default=1)
parser.add_argument("--message", type=str, default=None,
                    help="Custom disruption text (overrides --scenario)")
args = parser.parse_args()
DISRUPTION_TEXT = args.message or SCENARIOS[args.scenario]

# mailbox=False + explicit local endpoint so the coordinator can reply to us directly.
client = Agent(
    name="ChatTestClient",
    seed="groundtruth_chat_test_client_seed",
    port=8005,
    endpoint=["http://127.0.0.1:8005/submit"],
    mailbox=False,
)

chat_proto = Protocol(spec=chat_protocol_spec)


def text_chat(text: str) -> ChatMessage:
    return ChatMessage(
        timestamp=datetime.utcnow(),
        msg_id=uuid4(),
        content=[TextContent(type="text", text=text)],
    )


@client.on_event("startup")
async def send_probe(ctx: Context):
    ctx.logger.info(f"[client] Sending disruption to coordinator: {DISRUPTION_TEXT!r}")
    await ctx.send(COORDINATOR_ADDRESS, text_chat(DISRUPTION_TEXT))


@chat_proto.on_message(ChatMessage)
async def on_reply(ctx: Context, sender: str, msg: ChatMessage):
    for item in msg.content:
        if isinstance(item, TextContent):
            ctx.logger.info(f"[client] <<< REPLY from coordinator:\n{item.text}")
    # ack the reply
    await ctx.send(
        sender,
        ChatAcknowledgement(timestamp=datetime.utcnow(), acknowledged_msg_id=msg.msg_id),
    )


@chat_proto.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    ctx.logger.info(f"[client] got ack for {msg.acknowledged_msg_id}")


client.include(chat_proto)


if __name__ == "__main__":
    print(f"ChatTestClient address: {client.address}")
    print(f"Scenario: {DISRUPTION_TEXT!r}")
    client.run()
