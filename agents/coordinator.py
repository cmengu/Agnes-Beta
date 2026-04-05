# agents/coordinator.py — constitutional check, skill reuse, HTN decomposition.
import hashlib
import json
import os
import time
from pathlib import Path

from openai import OpenAI

from memory import get_skill, get_user_memory

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONSTITUTION = (_PROJECT_ROOT / "constitution.md").read_text(encoding="utf-8")

client = OpenAI(
    base_url=os.getenv("ZENMUX_BASE_URL", "https://zenmux.ai/api/v1"),
    api_key=os.environ["ZENMUX_API_KEY"],
)
MODEL = os.getenv("LLM_MODEL", "sapiens-ai/agnes-1.5-pro")


def call_agnes(system: str, user: str, json_mode: bool = False):
    del json_mode  # reserved for OpenAI JSON response_format if needed
    resp = client.chat.completions.create(
        model=MODEL,
        max_tokens=1000,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return resp.choices[0].message.content.strip()


def log_provenance(state: dict, action: str, output):
    state["session_log"].append(
        {
            "agent": "coordinator",
            "t": time.time(),
            "action": action,
            "input_hash": hashlib.md5(state["goal"].encode()).hexdigest()[:8],
            "output": str(output)[:120],
            "next": state.get("next_agent", ""),
        }
    )


def coordinator(state: dict):
    # ── BUDGET CHECK ────────────────────────────────────────────────────────
    state["steps_remaining"] -= 1
    elapsed = time.time() - state["run_start_time"]
    if state["steps_remaining"] <= 0 or elapsed > state["time_budget_s"]:
        state["budget_exhausted"] = True
        state["error"] = "Budget exhausted at Coordinator."
        state["next_agent"] = "output"
        log_provenance(state, "budget_exhausted", "steps or time exceeded")
        return state

    state["status_messages"].append("Coordinator: checking goal against constitution...")

    # ── CONSTITUTIONAL ACTION CHECK ──────────────────────────────────────────
    check = call_agnes(
        system=(
            "You are a constitutional safety checker. Read the constitution and "
            "decide if the goal violates any rule. "
            "Respond with exactly: PROCEED or BLOCK:<one-line reason>."
        ),
        user=f"Constitution:\n{CONSTITUTION}\n\nGoal: {state['goal']}",
    )

    if check.startswith("BLOCK"):
        state["constitution_passed"] = False
        state["constitution_block_reason"] = check.replace("BLOCK:", "").strip()
        state["error"] = f"Goal blocked: {state['constitution_block_reason']}"
        state["next_agent"] = "output"
        state["status_messages"].append(
            f"Coordinator: goal blocked by constitution — {state['constitution_block_reason']}"
        )
        log_provenance(state, "constitution_block", state["constitution_block_reason"])
        return state

    state["constitution_passed"] = True
    state["status_messages"].append("Coordinator: constitution check passed.")

    # ── MEMORY + SKILL LOOKUP ────────────────────────────────────────────────
    memory = get_user_memory(state["user_id"])
    past_goals = memory.get("past_goals", [])
    user_preferences = memory.get("user_preferences", {})

    goal_type_raw = call_agnes(
        system="Classify this goal in 3 words or fewer. Respond with only the classification.",
        user=state["goal"],
    )
    state["goal_type"] = goal_type_raw.strip().lower()

    cached_skill = get_skill(state["goal_type"])
    if cached_skill:
        state["sub_tasks"] = cached_skill["sub_tasks"]
        state["coordinator_notes"] = cached_skill["notes"]
        state["micro_queries"] = cached_skill.get("micro_queries", {})
        state["skill_reused"] = True
        state["current_task_index"] = 0
        state["next_agent"] = "research"
        state["status_messages"].append(
            f"Coordinator: reusing skill '{state['goal_type']}' — "
            f"{len(state['sub_tasks'])} sub-tasks loaded from library."
        )
        log_provenance(state, "skill_reused", state["goal_type"])
        return state

    # ── HTN DECOMPOSITION ────────────────────────────────────────────────────
    state["status_messages"].append("Coordinator: decomposing goal into sub-tasks...")

    decomp_raw = call_agnes(
        system=(
            "You are a research coordinator. Given a goal and context, produce a JSON "
            "object with two keys: "
            "'sub_tasks' (array of 2–4 specific research questions) and "
            "'coordinator_notes' (one sentence of tone/style guidance for the Writer). "
            "Respond with only valid JSON."
        ),
        user=(
            f"Goal: {state['goal']}\n"
            f"Past goals this user has researched: {past_goals}\n"
            f"User preferences: {user_preferences}"
        ),
    )

    try:
        decomp = json.loads(decomp_raw)
        state["sub_tasks"] = decomp["sub_tasks"]
        state["coordinator_notes"] = decomp.get("coordinator_notes", "")
    except (json.JSONDecodeError, KeyError):
        state["error"] = f"Coordinator failed to parse sub_tasks. Raw: {decomp_raw[:200]}"
        state["next_agent"] = "output"
        return state

    if not state["sub_tasks"]:
        state["error"] = "Coordinator produced no sub_tasks for this goal."
        state["next_agent"] = "output"
        return state

    # ── HTN: micro_queries per sub_task ──────────────────────────────────────
    micro = {}
    for i, task in enumerate(state["sub_tasks"]):
        q_raw = call_agnes(
            system=(
                "Write exactly 2 specific web search queries for this research question. "
                "Return a JSON array of 2 strings."
            ),
            user=task,
        )
        try:
            micro[str(i)] = json.loads(q_raw)
        except json.JSONDecodeError:
            micro[str(i)] = [task]
    state["micro_queries"] = micro

    state["current_task_index"] = 0
    state["next_agent"] = "research"
    state["status_messages"].append(
        f"Coordinator: goal decomposed into {len(state['sub_tasks'])} sub-tasks "
        f"with {sum(len(v) for v in micro.values())} micro-queries."
    )

    log_provenance(state, "decomposed", f"{len(state['sub_tasks'])} sub_tasks")
    return state
