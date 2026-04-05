# memory.py — persistence for user runs, distilled skills, and cross-user source cache.
import json
import time
from pathlib import Path

MEMORY_FILE = Path("memory_store.json")
SKILL_FILE = Path("skill_store.json")
COMMUNITY_FILE = Path("community_store.json")
SKILL_SCORE_THRESHOLD = 0.85


def _load(path: Path):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def _save(path: Path, data):
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ── USER MEMORY ──────────────────────────────────────────────────────────────


def get_user_memory(user_id: str):
    store = _load(MEMORY_FILE)
    return store.get(user_id, {"past_goals": [], "user_preferences": {}, "past_runs": []})


def save_run(user_id: str, goal: str, state: dict):
    """
    Called by output agent at end of every run.
    Persists: goal, causal chain, skill distillation (if score high enough),
    community source cache.
    """
    store = _load(MEMORY_FILE)
    user = store.setdefault(
        user_id, {"past_goals": [], "user_preferences": {}, "past_runs": []}
    )

    causal_record = {
        "goal": goal,
        "timestamp": time.time(),
        "session_id": state.get("session_id", ""),
        "sub_tasks": state.get("sub_tasks", []),
        "micro_queries": state.get("micro_queries", {}),
        "sources_used": [
            s["url"] for s in state.get("raw_sources", []) if not s.get("tainted")
        ],
        "research_confidence": state.get("research_confidence", 0),
        "critic_cycles": state.get("revision_count", 0),
        "critic_score": state.get("critic_score", 0),
        "critic_score_completeness": state.get("critic_score_completeness", 0),
        "critic_score_clarity": state.get("critic_score_clarity", 0),
        "critic_score_actionability": state.get("critic_score_actionability", 0),
        "draft_version": state.get("draft_version", 0),
        "budget_exhausted": state.get("budget_exhausted", False),
        "session_log": state.get("session_log", []),
    }
    user["past_goals"].append(goal)
    user["past_runs"].append(causal_record)
    _save(MEMORY_FILE, store)

    if state.get("critic_score", 0) >= SKILL_SCORE_THRESHOLD and not state.get(
        "skill_reused"
    ):
        save_skill(
            goal_type=state.get("goal_type", "unknown"),
            sub_tasks=state.get("sub_tasks", []),
            micro_queries=state.get("micro_queries", {}),
            coordinator_notes=state.get("coordinator_notes", ""),
        )

    clean_sources = [
        s
        for s in state.get("raw_sources", [])
        if not s.get("tainted") and s.get("full_content")
    ]
    if clean_sources:
        save_community_sources(
            goal_type=state.get("goal_type", "unknown"),
            sources=clean_sources,
            score=state.get("critic_score", 0),
        )


def save_user_preferences(user_id: str, preferences_delta: dict):
    store = _load(MEMORY_FILE)
    user = store.setdefault(
        user_id, {"past_goals": [], "user_preferences": {}, "past_runs": []}
    )
    user["user_preferences"].update(preferences_delta)
    _save(MEMORY_FILE, store)


# ── SKILL STORE ──────────────────────────────────────────────────────────────


def get_skill(goal_type: str):
    store = _load(SKILL_FILE)
    return store.get(goal_type)


def save_skill(goal_type: str, sub_tasks: list, micro_queries: dict, coordinator_notes: str):
    store = _load(SKILL_FILE)
    store[goal_type] = {
        "sub_tasks": sub_tasks,
        "micro_queries": micro_queries,
        "notes": coordinator_notes,
        "saved_at": time.time(),
    }
    _save(SKILL_FILE, store)


# ── CROSS-AGENT COMMUNITY CACHE ───────────────────────────────────────────────


def get_community_sources(goal_type: str):
    store = _load(COMMUNITY_FILE)
    entry = store.get(goal_type, {})
    return entry.get("sources", [])


def save_community_sources(goal_type: str, sources: list, score: float):
    store = _load(COMMUNITY_FILE)
    existing_score = store.get(goal_type, {}).get("score", 0)
    if score > existing_score:
        store[goal_type] = {
            "sources": sources,
            "score": score,
            "saved_at": time.time(),
        }
        _save(COMMUNITY_FILE, store)
