# server.py — FastAPI: CORS, /health, /history, /skills; async /run and /run/stream (ainvoke/astream).
import json
import os
import time
import uuid

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from graph import graph
from memory import SKILL_FILE, _load, get_user_memory

app = FastAPI(title="AgnesOps")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

MODEL = os.getenv("LLM_MODEL", "sapiens-ai/agnes-1.5-pro")
SEARCH_PROVIDER = os.getenv("SEARCH_PROVIDER", "serper")


class RunRequest(BaseModel):
    goal: str
    user_id: str
    channel: str


def build_initial_state(req: RunRequest) -> dict:
    return {
        "goal": req.goal,
        "user_id": req.user_id,
        "channel": req.channel,
        "session_id": str(uuid.uuid4()),
        "run_start_time": time.time(),
        "constitution_passed": False,
        "constitution_block_reason": "",
        "sub_tasks": [],
        "current_task_index": 0,
        "coordinator_notes": "",
        "goal_type": "",
        "skill_reused": False,
        "micro_queries": {},
        "search_queries": [],
        "raw_sources": [],
        "research_summary": "",
        "research_confidence": 0.0,
        "draft": "",
        "draft_version": 0,
        "critic_score": 0.0,
        "critic_score_completeness": 0.0,
        "critic_score_clarity": 0.0,
        "critic_score_actionability": 0.0,
        "critic_feedback": "",
        "critic_steelman": "",
        "critic_critique": "",
        "revision_count": 0,
        "confidence_recheck_done": False,
        "next_agent": "coordinator",
        "is_complete": False,
        "final_output": "",
        "steps_remaining": 14,
        "time_budget_s": 150,
        "budget_exhausted": False,
        "session_log": [],
        "status_messages": [],
        "error": None,
    }


def _finalize_response(final_state: dict) -> dict:
    return {
        "status": "complete",
        "final_output": final_state.get("final_output", ""),
        "status_messages": final_state.get("status_messages", []),
        "session_log": final_state.get("session_log", []),
        "critic_score": final_state.get("critic_score", 0),
        "scores": {
            "critic": final_state.get("critic_score", 0),
            "completeness": final_state.get("critic_score_completeness", 0),
            "clarity": final_state.get("critic_score_clarity", 0),
            "actionability": final_state.get("critic_score_actionability", 0),
        },
        "budget_exhausted": final_state.get("budget_exhausted", False),
        "error": final_state.get("error"),
        "research_confidence": final_state.get("research_confidence", 0),
        "constitution_block_reason": final_state.get("constitution_block_reason", ""),
    }


@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL, "search_provider": SEARCH_PROVIDER}


@app.get("/history/{user_id}")
def history(user_id: str):
    mem = get_user_memory(user_id)
    runs = mem.get("past_runs", [])
    return {
        "user_id": user_id,
        "total_runs": len(runs),
        "runs": [
            {
                "goal": r.get("goal", ""),
                "timestamp": r.get("timestamp", 0),
                "session_id": r.get("session_id", ""),
                "critic_score": r.get("critic_score", 0),
                "research_confidence": r.get("research_confidence", 0),
                "sources_used": len(r.get("sources_used", [])),
            }
            for r in runs
        ],
    }


@app.get("/skills")
def skills():
    store = _load(SKILL_FILE)
    return {
        "total_skills": len(store),
        "skills": [
            {
                "goal_type": k,
                "sub_task_count": len(v.get("sub_tasks", [])),
                "saved_at": v.get("saved_at", 0),
                "notes": v.get("notes", ""),
            }
            for k, v in store.items()
        ],
    }


@app.post("/run")
async def run(req: RunRequest):
    initial_state = build_initial_state(req)
    final_state = await graph.ainvoke(initial_state)
    return _finalize_response(final_state)


@app.post("/run/stream")
async def run_stream(req: RunRequest):
    async def event_source():
        initial_state = build_initial_state(req)
        prev_n = 0
        last_state = initial_state
        async for last_state in graph.astream(initial_state, stream_mode="values"):
            msgs = last_state.get("status_messages") or []
            delta = msgs[prev_n:] if len(msgs) > prev_n else []
            prev_n = len(msgs)
            payload = {
                "delta_status": delta,
                "status_messages": msgs,
                "steps_remaining": last_state.get("steps_remaining"),
                "time_budget_s": last_state.get("time_budget_s"),
                "budget_exhausted": last_state.get("budget_exhausted"),
                "research_confidence": last_state.get("research_confidence"),
                "critic_score": last_state.get("critic_score"),
                "critic_score_completeness": last_state.get("critic_score_completeness"),
                "critic_score_clarity": last_state.get("critic_score_clarity"),
                "critic_score_actionability": last_state.get("critic_score_actionability"),
                "revision_count": last_state.get("revision_count"),
                "error": last_state.get("error"),
            }
            yield f"data: {json.dumps(payload, default=str)}\n\n"
        done = _finalize_response(last_state)
        yield f"data: {json.dumps({'done': True, **done}, default=str)}\n\n"

    return StreamingResponse(event_source(), media_type="text/event-stream")
