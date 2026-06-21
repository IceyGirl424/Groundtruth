"""
agent_memory.py

Redis-backed vector memory for Groundtruth (Redis prize track: Agent Memory +
vector search + context retrieval).

After each disruption is resolved we store the disruption text + Claude's
reassignment plan as a Redis hash, indexed by a vector embedding of the
disruption text (RedisVL vector index). Before resolving a NEW disruption we run
a vector similarity search to retrieve the most similar past incidents and feed
them back into the reasoning prompt as extra context — so the coordinator
"remembers" how comparable situations were handled.

Embeddings: sentence-transformers/all-MiniLM-L6-v2 (384-dim, runs locally, no
extra API key). Vector index + query: RedisVL.

Every public function is defensive: any Redis/embedding failure is caught and
turned into a safe no-op (empty list / None) so a memory hiccup never crashes
the agent.
"""

import json
import os
import time

from redisvl.index import SearchIndex
from redisvl.query import VectorQuery
from redisvl.utils.vectorize import HFTextVectorizer

REDIS_URL = os.environ.get("REDIS_URL")

INDEX_NAME = "groundtruth_incidents"
KEY_PREFIX = "groundtruth:incident"
EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBED_DIMS = 384  # all-MiniLM-L6-v2 output dimensionality

# RedisVL index schema: one hash per incident, vector-indexed on the disruption.
_SCHEMA = {
    "index": {
        "name": INDEX_NAME,
        "prefix": KEY_PREFIX,
        "storage_type": "hash",
    },
    "fields": [
        {"name": "disruption", "type": "text"},
        {"name": "plan_json", "type": "text"},
        {"name": "timestamp", "type": "text"},
        {
            "name": "embedding",
            "type": "vector",
            "attrs": {
                "dims": EMBED_DIMS,
                "distance_metric": "cosine",
                "algorithm": "flat",
                "datatype": "float32",
            },
        },
    ],
}

# Lazily-initialised singletons (model load + index connect are not free).
_index = None
_vectorizer = None


def _get_vectorizer() -> HFTextVectorizer:
    global _vectorizer
    if _vectorizer is None:
        _vectorizer = HFTextVectorizer(model=EMBED_MODEL)
    return _vectorizer


def _get_index() -> SearchIndex:
    global _index
    if _index is None:
        if not REDIS_URL:
            raise RuntimeError("REDIS_URL is not set in the environment")
        _index = SearchIndex.from_dict(_SCHEMA, redis_url=REDIS_URL)
    # Ensure the index actually exists on EVERY access. It may be absent on a
    # fresh DB or if it was dropped out-of-band — and querying a missing index
    # errors out. create(overwrite=False) is a no-op when it already exists.
    if not _index.exists():
        _index.create(overwrite=False)
    return _index


def store_incident(disruption: str, plan: dict) -> str | None:
    """Store a resolved incident (disruption + Claude plan) with a vector
    embedding of the disruption text. Returns the Redis key, or None on failure.
    """
    try:
        embedding = _get_vectorizer().embed(disruption, as_buffer=True)
        record = {
            "disruption": disruption,
            "plan_json": json.dumps(plan),
            "timestamp": str(time.time()),
            "embedding": embedding,
        }
        keys = _get_index().load([record])
        return keys[0] if keys else None
    except Exception as exc:  # noqa: BLE001 - memory must never crash the agent
        print(f"[agent_memory] store_incident failed: {type(exc).__name__}: {exc}")
        return None


def retrieve_similar(disruption: str, top_k: int = 2) -> list[dict]:
    """Return up to top_k past incidents most similar to the given disruption.

    Each result dict contains: disruption, plan_json, timestamp, vector_distance
    (lower = more similar, cosine distance). Returns [] on any failure.
    """
    try:
        query_vec = _get_vectorizer().embed(disruption, as_buffer=True)
        query = VectorQuery(
            vector=query_vec,
            vector_field_name="embedding",
            return_fields=["disruption", "plan_json", "timestamp"],
            num_results=top_k,
        )
        return _get_index().query(query) or []
    except Exception as exc:  # noqa: BLE001
        print(f"[agent_memory] retrieve_similar failed: {type(exc).__name__}: {exc}")
        return []


def format_memory_context(matches: list[dict]) -> str:
    """Turn retrieved past incidents into a plain-text block for the reasoning
    prompt. Returns "" when there are no matches (so the prompt is unchanged for
    the very first incident)."""
    if not matches:
        return ""

    lines = ["Similar PAST incidents and how they were previously resolved:"]
    for i, m in enumerate(matches, 1):
        rationale = ""
        try:
            rationale = json.loads(m.get("plan_json", "{}")).get("rationale_summary", "")
        except Exception:  # noqa: BLE001
            pass
        try:
            similarity = f" (similarity {1 - float(m['vector_distance']):.2f})"
        except Exception:  # noqa: BLE001
            similarity = ""
        lines.append(f"{i}. Disruption{similarity}: {m.get('disruption', '')}")
        if rationale:
            lines.append(f"   Past resolution: {rationale}")
    return "\n".join(lines)


if __name__ == "__main__":
    # Standalone smoke test: store one incident, then retrieve it.
    demo_plan = {
        "reassignments": [
            {"task": "Panel B2", "from_drone": "Drone-2", "to_drone": "Drone-3",
             "reason": "highest battery"}
        ],
        "unassignable_tasks": [],
        "rationale_summary": "Drone-2 critical; its task moved to Drone-3 (most capacity).",
    }
    key = store_incident("Drone-2 battery dropped critical, reassign its tasks", demo_plan)
    print("stored key:", key)
    matches = retrieve_similar("a drone has low battery and needs its work moved", top_k=2)
    print("retrieved:", len(matches), "match(es)")
    print(format_memory_context(matches))
