# state.py — shared AgentState contract for LangGraph and all agents.
from typing import TypedDict, Optional


class AgentState(TypedDict):
    # ── INPUT ────────────────────────────────────────────────────────────────
    goal: str  # raw user request, set once, never mutated
    user_id: str  # messaging platform user identifier
    session_id: str  # UUID generated at run start
    channel: str  # "telegram" | "discord"

    # ── CONSTITUTIONAL CHECK (Coordinator) ───────────────────────────────────
    constitution_passed: bool  # True after constitutional check passes
    constitution_block_reason: str  # populated if check fails; empty string otherwise

    # ── COORDINATOR ──────────────────────────────────────────────────────────
    sub_tasks: list  # 2–4 research questions from Coordinator
    current_task_index: int  # which sub_task Research is on; starts at 0
    coordinator_notes: str  # style/tone constraints passed to Writer
    goal_type: str  # 3-word classification for skill lookup
    skill_reused: bool  # True if coordinator loaded a cached skill

    # ── HIERARCHICAL TASK NETWORK ─────────────────────────────────────────────
    micro_queries: dict  # {sub_task_index as str: [query1, query2]}

    # ── RESEARCH ─────────────────────────────────────────────────────────────
    search_queries: list  # all queries Research ran, in order
    raw_sources: list  # each: {url, title, snippet, full_content, tainted}
    research_summary: str  # synthesised output; what Writer actually reads
    research_confidence: float  # 0.0–1.0 based on source count and coverage

    # ── WRITER ───────────────────────────────────────────────────────────────
    draft: str  # current draft in Markdown; overwritten each revision
    draft_version: int  # starts at 1; increments per revision; caps at 3

    # ── CRITIC — Process Reward Model scores ──────────────────────────────────
    critic_score: float  # final arbitrated score 0.0–1.0
    critic_score_completeness: float  # per-axis PRM score
    critic_score_clarity: float  # per-axis PRM score
    critic_score_actionability: float  # per-axis PRM score (weakest axis drives routing)
    critic_feedback: str  # axis-specific revision instructions
    critic_steelman: str  # debate: arguments for the draft's quality
    critic_critique: str  # debate: arguments against the draft's quality
    revision_count: int  # number of Critic→Writer loops so far; caps at 2
    confidence_recheck_done: bool  # True after Critic has triggered one Research recheck

    # ── CONTROL FLOW ─────────────────────────────────────────────────────────
    next_agent: str  # "coordinator"|"research"|"writer"|"critic"|"output"
    is_complete: bool  # set True by output node
    final_output: str  # formatted deliverable set by output node

    # ── BUDGET MANAGEMENT ────────────────────────────────────────────────────
    steps_remaining: int  # starts at 14; every agent decrements by 1
    time_budget_s: int  # starts at 150; compared against elapsed time
    run_start_time: float  # set at graph entry via time.time()
    budget_exhausted: bool  # set True when budget runs out

    # ── PROVENANCE & CAUSAL ATTRIBUTION ──────────────────────────────────────
    session_log: list  # list of {agent, t, action, input_hash, output, next}

    # ── STATUS ───────────────────────────────────────────────────────────────
    status_messages: list  # append-only; each agent appends on start and finish
    error: Optional[str]  # set by any agent on failure; routes to output
