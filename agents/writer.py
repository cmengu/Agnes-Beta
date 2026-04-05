# agents/writer.py — draft with numbered source list in prompt; truncation warning for SSE.
import hashlib
import os
import time

from openai import OpenAI

client = OpenAI(
    base_url=os.getenv("ZENMUX_BASE_URL", "https://zenmux.ai/api/v1"),
    api_key=os.environ["ZENMUX_API_KEY"],
)
MODEL = os.getenv("LLM_MODEL", "sapiens-ai/agnes-1.5-pro")


def _build_citation_block(raw_sources: list) -> str:
    """
    Numbered reference list for the writer prompt, capped at 10.
    The LLM uses these for inline [n] citations and a ## References section.
    """
    clean = [s for s in raw_sources if not s.get("tainted") and s.get("url")][:10]
    if not clean:
        return ""
    return "\n".join(
        f"[{i + 1}] {s.get('title', s['url'])} — {s['url']}" for i, s in enumerate(clean)
    )


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
    # ── BUDGET CHECK ──────────────────────────────────────────────────────
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

    citation_block = _build_citation_block(state.get("raw_sources", []))

    revision_context = ""
    if state.get("critic_feedback"):
        revision_context = (
            f"\n\nPrevious draft (version {state['draft_version']}):\n{state['draft']}"
            f"\n\nCritic feedback — address these specific issues:\n{state['critic_feedback']}"
        )

    citation_instruction = ""
    if citation_block:
        citation_instruction = (
            f"\n\nAvailable sources (cite inline as [1], [2], etc.):\n{citation_block}\n"
            "Cite at least one source number after each factual claim. "
            "End the report with a ## References section listing all cited sources as Markdown links."
        )

    resp = client.chat.completions.create(
        model=MODEL,
        max_tokens=1200,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a professional research writer. "
                    f"Tone and style guidance: {state['coordinator_notes']} "
                    "Always produce a Markdown report with exactly three sections: "
                    "## Executive Summary, ## Findings, ## Recommendations. "
                    "Follow with ## References if sources were provided."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Research summary:\n{state['research_summary']}"
                    f"{citation_instruction}"
                    f"{revision_context}"
                ),
            },
        ],
    )

    choice = resp.choices[0]
    state["draft"] = (choice.message.content or "").strip()
    state["draft_version"] = v

    if choice.finish_reason == "length":
        state["status_messages"].append(
            f"Writer: WARNING — draft v{v} truncated at token limit. "
            "Critic may request revision."
        )

    state["next_agent"] = "critic"
    n_cited = len(citation_block.splitlines()) if citation_block else 0
    state["status_messages"].append(
        f"Writer: draft {v} complete ({n_cited} sources available for citation)."
    )
    log_provenance(state, f"wrote_draft_v{v}", f"{len(state['draft'])} chars")
    return state
