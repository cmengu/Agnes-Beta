# agents/output.py — quality badge, body, provenance footer; save_run.
from memory import save_run


def _quality_badge(state: dict) -> str:
    conf = state.get("research_confidence", 0.0)
    score = state.get("critic_score", 0.0)
    revisions = state.get("revision_count", 0)
    n_sources = len([s for s in state.get("raw_sources", []) if not s.get("tainted")])
    skill_tag = " · ⚡ skill reused" if state.get("skill_reused") else ""
    return (
        f"> **AgnesOps Quality Signal** — "
        f"Research confidence: `{conf:.0%}` · "
        f"Critic score: `{score:.2f}` · "
        f"Revisions: `{revisions}` · "
        f"Clean sources: `{n_sources}`"
        f"{skill_tag}\n\n"
    )


def output_agent(state: dict):
    draft = state.get("draft", "")
    error = state.get("error")

    if error and not draft:
        body = f"⚠️ Run terminated early: {error}"
    elif state.get("budget_exhausted") and draft:
        body = f"⚠️ Budget exhausted — partial report below:\n\n{draft}"
    else:
        body = draft or ""

    badge = _quality_badge(state)
    footer = (
        f"\n\n---\n*Run: `{state.get('session_id', '?')}` · "
        f"{len(state.get('session_log', []))} agent actions · "
        f"critic={state.get('critic_score', 0):.2f} · "
        f"confidence={state.get('research_confidence', 0):.2f} · "
        f"revisions={state.get('revision_count', 0)}*"
    )

    state["final_output"] = badge + body + footer

    try:
        save_run(state["user_id"], state["goal"], state)
    except Exception:
        pass

    state["is_complete"] = True
    state["status_messages"].append("Output: report delivered.")
    return state
