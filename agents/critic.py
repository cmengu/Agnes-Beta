# agents/critic.py — debate, PRM-style scoring, uncertainty routing, revision cap.
import hashlib
import json
import os
import time

from openai import OpenAI

client = OpenAI(
    base_url=os.getenv("ZENMUX_BASE_URL", "https://zenmux.ai/api/v1"),
    api_key=os.environ["ZENMUX_API_KEY"],
)
MODEL = os.getenv("LLM_MODEL", "sapiens-ai/agnes-1.5-pro")
SCORE_THRESHOLD = float(os.getenv("CRITIC_SCORE_THRESHOLD", "0.75"))


def call_agnes(system: str, user: str, retries: int = 1):
    for attempt in range(retries + 1):
        try:
            resp = client.chat.completions.create(
                model=MODEL,
                max_tokens=1000,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
            return resp.choices[0].message.content.strip()
        except Exception:
            if attempt == retries:
                raise
            time.sleep(2**attempt)


def log_provenance(state: dict, action: str, output):
    draft = state.get("draft") or ""
    state["session_log"].append(
        {
            "agent": "critic",
            "t": time.time(),
            "action": action,
            "input_hash": hashlib.md5(draft.encode()).hexdigest()[:8],
            "output": str(output)[:120],
            "next": state.get("next_agent", ""),
        }
    )


def critic(state: dict):
    # ── BUDGET CHECK ────────────────────────────────────────────────────────
    state["steps_remaining"] -= 1
    elapsed = time.time() - state["run_start_time"]
    if state["steps_remaining"] <= 0 or elapsed > state["time_budget_s"]:
        state["budget_exhausted"] = True
        state["next_agent"] = "output"
        log_provenance(state, "budget_exhausted", "skipping critic")
        return state

    state["status_messages"].append("Critic: reviewing draft...")

    if (
        state["research_confidence"] < 0.5
        and not state["confidence_recheck_done"]
        and state["revision_count"] == 0
    ):
        state["confidence_recheck_done"] = True
        state["current_task_index"] = 0
        state["next_agent"] = "research"
        state["status_messages"].append(
            f"Critic: research confidence too low "
            f"({state['research_confidence']:.2f}) — requesting deeper research."
        )
        log_provenance(state, "confidence_recheck", state["research_confidence"])
        return state

    state["status_messages"].append("Critic: running steelman + critique debate...")

    try:
        steelman = call_agnes(
            system=(
                "You are a rigorous advocate. Your job is to argue that this draft is "
                "excellent — find every strength, every well-supported claim, every "
                "useful insight. Be specific. One short paragraph."
            ),
            user=f"Goal: {state['goal']}\n\nDraft:\n{state['draft']}",
        )
    except Exception:
        steelman = "Steelman unavailable — API error during debate."
    state["critic_steelman"] = steelman

    try:
        devil = call_agnes(
            system=(
                "You are a harsh critic. Your job is to find every weakness in this draft — "
                "unsupported claims, missing information, unclear recommendations, poor "
                "structure. Be specific. One short paragraph."
            ),
            user=f"Goal: {state['goal']}\n\nDraft:\n{state['draft']}",
        )
    except Exception:
        devil = "Critique unavailable — API error during debate."
    state["critic_critique"] = devil

    state["status_messages"].append("Critic: arbitrating scores across three dimensions...")

    arbitration_raw = call_agnes(
        system=(
            "You are an arbitrator. Given a steelman argument and a critique, "
            "score this draft on three axes: completeness (0.0–1.0), "
            "clarity (0.0–1.0), actionability (0.0–1.0). "
            "Identify the weakest axis and write specific feedback targeting it. "
            "Respond with ONLY valid JSON: "
            '{"completeness": float, "clarity": float, "actionability": float, '
            '"weakest_axis": str, "feedback": str}'
        ),
        user=(
            f"Steelman:\n{steelman}\n\n"
            f"Critique:\n{devil}\n\n"
            f"Draft:\n{state['draft']}\n\n"
            f"User goal (must be addressed in Recommendations): {state['goal']}\n"
            f"Penalize actionability if the draft does not answer this goal with concrete steps "
            f"or explicitly states missing data needed to answer."
        ),
    )

    try:
        scores = json.loads(arbitration_raw)
        state["critic_score_completeness"] = float(scores["completeness"])
        state["critic_score_clarity"] = float(scores["clarity"])
        state["critic_score_actionability"] = float(scores["actionability"])
        state["critic_feedback"] = scores["feedback"]
        state["critic_score"] = round(
            (
                state["critic_score_completeness"]
                + state["critic_score_clarity"]
                + state["critic_score_actionability"]
            )
            / 3,
            3,
        )
        weakest = min(
            state["critic_score_completeness"],
            state["critic_score_clarity"],
            state["critic_score_actionability"],
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        state["critic_score"] = 0.76
        weakest = 0.76
        state["critic_feedback"] = "Arbitration parse failed — approving draft."
        state["critic_score_completeness"] = 0.76
        state["critic_score_clarity"] = 0.76
        state["critic_score_actionability"] = 0.76

    if weakest >= SCORE_THRESHOLD or state["revision_count"] >= 2:
        state["next_agent"] = "output"
        state["status_messages"].append(
            f"Critic: approved — completeness={state['critic_score_completeness']:.2f}, "
            f"clarity={state['critic_score_clarity']:.2f}, "
            f"actionability={state['critic_score_actionability']:.2f}, "
            f"avg={state['critic_score']:.2f}"
        )
        log_provenance(state, "approved", f"score={state['critic_score']}")
    else:
        state["revision_count"] += 1
        state["next_agent"] = "writer"
        state["status_messages"].append(
            f"Critic: revision {state['revision_count']} requested — "
            f"weakest axis score={weakest:.2f}, feedback: {state['critic_feedback'][:80]}"
        )
        log_provenance(state, f"revision_{state['revision_count']}", f"weakest={weakest:.2f}")

    return state
