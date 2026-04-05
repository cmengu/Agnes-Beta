# agents/research.py — web search, fetch, sanitisation, synthesis (Phase 3).
import hashlib
import json
import os
import re
import time

import httpx
from openai import OpenAI

client = OpenAI(
    base_url=os.getenv("ZENMUX_BASE_URL", "https://zenmux.ai/api/v1"),
    api_key=os.environ["ZENMUX_API_KEY"],
)
MODEL = os.getenv("LLM_MODEL", "sapiens-ai/agnes-1.5-pro")

SEARCH_PROVIDER = os.getenv("SEARCH_PROVIDER", "serper")
_KEY_MAP = {
    "serper": os.environ.get("SERPER_API_KEY"),
    "tavily": os.environ.get("TAVILY_API_KEY"),
    "serpapi": os.environ.get("SERPAPI_API_KEY"),
}
SEARCH_API_KEY = _KEY_MAP.get(SEARCH_PROVIDER)
if not SEARCH_API_KEY:
    raise EnvironmentError(
        f"No API key for provider '{SEARCH_PROVIDER}'. "
        "Set SERPER_API_KEY, TAVILY_API_KEY, or SERPAPI_API_KEY in .env"
    )

INJECTION_PATTERNS = [
    "ignore previous instructions",
    "you are now",
    "disregard your",
    "new task:",
    "system:",
    "forget everything",
    "override your",
    "assistant:",
    "respond only with",
    "pretend you are",
]


def call_agnes(system: str, user: str):
    resp = client.chat.completions.create(
        model=MODEL,
        max_tokens=1000,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return resp.choices[0].message.content.strip()


def sanitize_source(source: dict):
    """Prompt injection defense — strip tainted content before synthesis."""
    text = source.get("full_content", "").lower()
    for pattern in INJECTION_PATTERNS:
        if pattern in text:
            source["tainted"] = True
            source["full_content"] = "[excluded: injection pattern detected]"
            return source
    source["tainted"] = False
    return source


def search_web(query: str):
    """Call the configured search provider. Returns list of {url, title, snippet}."""
    if SEARCH_PROVIDER == "serper":
        resp = httpx.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": SEARCH_API_KEY, "Content-Type": "application/json"},
            json={"q": query, "num": 5},
            timeout=10,
        )
        results = resp.json().get("organic", [])
        return [
            {
                "url": r.get("link", ""),
                "title": r.get("title", ""),
                "snippet": r.get("snippet", ""),
            }
            for r in results[:5]
        ]
    if SEARCH_PROVIDER == "tavily":
        resp = httpx.post(
            "https://api.tavily.com/search",
            json={"api_key": SEARCH_API_KEY, "query": query, "max_results": 5},
            timeout=10,
        )
        results = resp.json().get("results", [])
        return [
            {
                "url": r.get("url", ""),
                "title": r.get("title", ""),
                "snippet": r.get("content", ""),
            }
            for r in results[:5]
        ]
    return []


def fetch_page(url: str):
    """Fetch full page text. Return empty string on failure."""
    if not url or url == "stub":
        return ""
    try:
        resp = httpx.get(
            url,
            timeout=8,
            follow_redirects=True,
            headers={"User-Agent": "AgnesOps/1.0"},
        )
        text = re.sub(r"<[^>]+>", " ", resp.text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:4000]
    except Exception:
        return ""


def log_provenance(state: dict, action: str, output):
    state["session_log"].append(
        {
            "agent": "research",
            "t": time.time(),
            "action": action,
            "input_hash": hashlib.md5(
                state["sub_tasks"][state["current_task_index"]].encode()
            ).hexdigest()[:8],
            "output": str(output)[:120],
            "next": state.get("next_agent", ""),
        }
    )


def research(state: dict):
    # ── BUDGET CHECK ────────────────────────────────────────────────────────
    state["steps_remaining"] -= 1
    elapsed = time.time() - state["run_start_time"]
    if state["steps_remaining"] <= 0 or elapsed > state["time_budget_s"]:
        state["budget_exhausted"] = True
        state["error"] = "Budget exhausted at Research — partial results below."
        state["next_agent"] = "writer"
        return state

    idx = state["current_task_index"]
    task = state["sub_tasks"][idx]
    micro = state["micro_queries"].get(str(idx), [task])

    state["status_messages"].append(f"Research: working on sub-task {idx + 1} — {task}")

    for query in micro:
        results = search_web(query)
        for r in results[:3]:
            full = fetch_page(r["url"])
            raw_source = {
                "url": r["url"],
                "title": r["title"],
                "snippet": r["snippet"],
                "full_content": full,
                "tainted": False,
            }
            raw_source = sanitize_source(raw_source)
            state["raw_sources"].append(raw_source)
            state["search_queries"].append(query)

    if idx < len(state["sub_tasks"]) - 1:
        state["current_task_index"] += 1
        state["next_agent"] = "research"
        log_provenance(state, f"completed sub-task {idx + 1}", "continuing")
        return state

    state["status_messages"].append("Research: synthesising findings across all sub-tasks...")

    clean_sources = [s for s in state["raw_sources"] if not s.get("tainted")]
    sources_text = "\n\n".join(
        f"Source ({s['url']}):\n{s['full_content']}" for s in clean_sources
    )

    state["research_summary"] = call_agnes(
        system=(
            "You are a research synthesiser. Given findings from multiple sources, "
            "produce a coherent, factual research summary grouped by sub-task."
        ),
        user=f"Sub-tasks: {state['sub_tasks']}\n\nSources:\n{sources_text}",
    )

    n_clean = len(clean_sources)
    n_tainted = len(state["raw_sources"]) - n_clean
    state["research_confidence"] = round(min(1.0, n_clean / 4.0), 2)

    state["status_messages"].append(
        f"Research: complete — {n_clean} clean sources, {n_tainted} excluded, "
        f"confidence={state['research_confidence']:.2f}"
    )

    log_provenance(state, "synthesis_complete", f"confidence={state['research_confidence']}")
    state["next_agent"] = "writer"
    return state
