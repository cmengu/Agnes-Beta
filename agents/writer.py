# agents/writer.py — turns research_summary into structured Markdown draft.
import hashlib
import os
import time

from openai import OpenAI

client = OpenAI(
    base_url=os.getenv("ZENMUX_BASE_URL", "https://zenmux.ai/api/v1"),
    api_key=os.environ["ZENMUX_API_KEY"],
)
MODEL = os.getenv("LLM_MODEL", "sapiens-ai/agnes-1.5-pro")


def log_provenance(state: dict, action: str, output):
    state["session_log"].append(
        {
            "agent": "writer",
            "t": time.time(),
            "action": action,
            "input_hash": hashlib.md5(
                (state.get("research_summary") or "").encode()
            ).hexdigest()[:8],
            "output": str(output)[:120],
            "next": state.get("next_agent", ""),
        }
    )


def writer(state: dict):
    # ── BUDGET CHECK ────────────────────────────────────────────────────────
    state["steps_remaining"] -= 1
    elapsed = time.time() - state["run_start_time"]
    if state["steps_remaining"] <= 0 or elapsed > state["time_budget_s"]:
        state["budget_exhausted"] = True
        if not state["draft"]:
            state["draft"] = "_Budget exhausted before draft could be written._"
        state["next_agent"] = "output"
        return state

    v = state["draft_version"] + 1
    state["status_messages"].append(f"Writer: drafting report (version {v})...")

    revision_context = ""
    if state.get("critic_feedback"):
        revision_context = (
            f"\n\nPrevious draft (version {state['draft_version']}):\n{state['draft']}"
            f"\n\nCritic feedback — address these specific issues:\n{state['critic_feedback']}"
        )

    resp = client.chat.completions.create(
        model=MODEL,
        max_tokens=1000,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a professional research writer. "
                    f"Tone and style guidance: {state['coordinator_notes']} "
                    "Always produce a Markdown report with exactly three sections: "
                    "## Executive Summary, ## Findings, ## Recommendations."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Research summary:\n{state['research_summary']}" f"{revision_context}"
                ),
            },
        ],
    )

    state["draft"] = resp.choices[0].message.content.strip()
    state["draft_version"] = v
    state["next_agent"] = "critic"

    state["status_messages"].append(f"Writer: draft {v} complete.")
    log_provenance(state, f"wrote_draft_v{v}", f"{len(state['draft'])} chars")
    return state
