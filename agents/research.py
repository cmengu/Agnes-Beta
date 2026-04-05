# agents/research.py — parallel sub-task fetch (fork-join), synthesis cap, URL dedup for critic recheck.
import hashlib
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

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

BOILERPLATE_MARKERS = (
    "window.",
    "wiz_global_data",
    "datalayer",
    "gtag(",
    "cloudflare",
    "challenge-platform",
    "/cdn-cgi/",
    "navigator.",
    "function(){",
    "googletagmanager",
)


def _visible_text_ratio(text: str) -> float:
    if not text:
        return 0.0
    alnum = sum(1 for c in text if c.isalnum())
    return alnum / len(text)


def _mark_source_signal(source: dict) -> None:
    """Tag each raw source with low_signal / signal_hits from fetched HTML text (non-tainted)."""
    full = source.get("full_content") or ""
    lower = full.lower()
    # Occurrence counts across markers (density), not just “how many marker types appear once”.
    hit = sum(lower.count(m) for m in BOILERPLATE_MARKERS)
    # Min length: stubs & error pages; keep below plan doc examples (~160 chars × 3) so prose checks pass.
    low_signal = len(full) < 100 or hit >= 3 or _visible_text_ratio(full) < 0.12
    source["low_signal"] = low_signal
    source["signal_hits"] = hit


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
    text = source.get("full_content", "").lower()
    for pattern in INJECTION_PATTERNS:
        if pattern in text:
            source["tainted"] = True
            source["full_content"] = "[excluded: injection pattern detected]"
            return source
    source["tainted"] = False
    return source


def search_web(query: str):
    if SEARCH_PROVIDER == "serper":
        resp = httpx.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": SEARCH_API_KEY, "Content-Type": "application/json"},
            json={"q": query, "num": 5},
            timeout=10,
        )
        results = resp.json().get("organic", [])
        return [
            {"url": r.get("link", ""), "title": r.get("title", ""), "snippet": r.get("snippet", "")}
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
            {"url": r.get("url", ""), "title": r.get("title", ""), "snippet": r.get("content", "")}
            for r in results[:5]
        ]
    return []


def fetch_page(url: str):
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


def _fetch_subtask(idx: int, micro_queries: list):
    """Fetch all micro-queries for one sub-task. Returns (idx, list of (query, source))."""
    results = []
    for query in micro_queries:
        hits = search_web(query)
        for r in hits[:3]:
            full = fetch_page(r["url"])
            src = sanitize_source(
                {
                    "url": r["url"],
                    "title": r["title"],
                    "snippet": r["snippet"],
                    "full_content": full,
                    "tainted": False,
                }
            )
            _mark_source_signal(src)
            results.append((query, src))
    return idx, results


def log_provenance(state: dict, action: str, output):
    first_task = state["sub_tasks"][0] if state["sub_tasks"] else ""
    state["session_log"].append(
        {
            "agent": "research",
            "t": time.time(),
            "action": action,
            "input_hash": hashlib.md5(first_task.encode()).hexdigest()[:8],
            "output": str(output)[:120],
            "next": state.get("next_agent", ""),
        }
    )


def research(state: dict):
    # ── BUDGET CHECK ──────────────────────────────────────────────────────
    state["steps_remaining"] -= 1
    elapsed = time.time() - state["run_start_time"]
    if state["steps_remaining"] <= 0 or elapsed > state["time_budget_s"]:
        state["budget_exhausted"] = True
        state["error"] = "Budget exhausted at Research — partial results below."
        state["next_agent"] = "writer"
        return state

    sub_tasks = state["sub_tasks"]
    micro_queries = state["micro_queries"]

    state["status_messages"].append(
        f"Research: running {len(sub_tasks)} sub-tasks in parallel..."
    )

    seen_urls: set = {s["url"] for s in state["raw_sources"] if s.get("url")}

    workers = min(len(sub_tasks), 4)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(_fetch_subtask, idx, micro_queries.get(str(idx), [task]))
            for idx, task in enumerate(sub_tasks)
        ]
        for fut in as_completed(futures):
            idx, pairs = fut.result()
            added = 0
            for query, src in pairs:
                url = src.get("url", "")
                if url and url in seen_urls:
                    continue
                if url:
                    seen_urls.add(url)
                state["raw_sources"].append(src)
                state["search_queries"].append(query)
                added += 1
            state["status_messages"].append(
                f"Research: sub-task {idx + 1} complete — {added} sources added."
            )

    state["status_messages"].append("Research: synthesising findings across all sub-tasks...")

    clean_sources = [s for s in state["raw_sources"] if not s.get("tainted")]
    # High-signal set and domains for confidence (D6: must precede synthesis for Step 3 nudge).
    high = [
        s
        for s in state["raw_sources"]
        if not s.get("tainted") and not s.get("low_signal")
    ]
    domains = {urlparse(s["url"]).netloc.lower() for s in high if s.get("url")}
    # D1: confidence = high-signal fraction (cap 5) × domain diversity (cap 3)
    base = min(1.0, len(high) / 5.0) * min(1.0, len(domains) / 3.0)
    state["research_confidence"] = round(base, 2)

    synthesis_sources = clean_sources[:8]
    sources_text = "\n\n".join(
        f"Source ({s['url']}):\n{s['full_content'][:1500]}" for s in synthesis_sources
    )

    state["research_summary"] = call_agnes(
        system=(
            "You are a research synthesiser. Given findings from multiple sources, "
            "produce a coherent, factual research summary grouped by sub-task."
        ),
        user=f"Sub-tasks: {sub_tasks}\n\nSources:\n{sources_text}",
    )

    n_clean = len(clean_sources)
    n_tainted = len(state["raw_sources"]) - n_clean
    state["current_task_index"] = len(sub_tasks) - 1 if sub_tasks else 0

    state["status_messages"].append(
        f"Research: complete — {n_clean} clean sources ({len(high)} high-signal, "
        f"{len(domains)} domains), {n_tainted} excluded, "
        f"confidence={state['research_confidence']:.2f}"
    )

    log_provenance(
        state, "parallel_synthesis_complete", f"confidence={state['research_confidence']}"
    )
    state["next_agent"] = "writer"
    return state
