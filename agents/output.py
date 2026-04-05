# agents/output.py — terminal node: format deliverable, provenance footer, save_run.
from memory import save_run


def output_agent(state: dict):
    """
    Terminal graph node. Formats deliverable, saves run, appends provenance.
    OpenClaw / clients can poll GET or consume /run/stream for live status_messages.
    """
    draft = state.get("draft", "")
    error = state.get("error")

    if error and not draft:
        state["final_output"] = f"⚠️ Run terminated early: {error}"
    elif state.get("budget_exhausted") and draft:
        state["final_output"] = f"⚠️ Budget exhausted — partial report below:\n\n{draft}"
    else:
        state["final_output"] = draft or ""

    state["final_output"] += (
        f"\n\n---\n*Run: `{state.get('session_id', '?')}` · "
        f"{len(state.get('session_log', []))} agent actions · "
        f"critic={state.get('critic_score', 0):.2f} · "
        f"confidence={state.get('research_confidence', 0):.2f} · "
        f"revisions={state.get('revision_count', 0)}*"
    )

    try:
        save_run(state["user_id"], state["goal"], state)
    except Exception:
        pass

    state["is_complete"] = True
    state["status_messages"].append("Output: report delivered.")
    return state
