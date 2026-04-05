# TinyFish — optional future fetch backend

**Status:** Documentation only. There is **no** TinyFish API key or runtime dependency in this repository.

## Why this exists

Some sites still return empty or bot-challenge HTML after plain **httpx** and optional **Playwright** (`agents/research.py` `fetch_page`). A vendor-specific render service (e.g. TinyFish) can be an enterprise option when self-hosted browsers are not enough.

## Integration sketch (not implemented)

1. Add a function such as `fetch_via_tinyfish(url: str) -> str` in a small module, behind env flags (e.g. `TINYFISH_API_KEY`, `TINYFISH_API_BASE`).
2. In `fetch_page`, **after** the existing httpx retries (and after optional Playwright when `USE_PLAYWRIGHT_FETCH=1`), if text is still short or boilerplate-heavy, call `fetch_via_tinyfish(url)` and merge the result using the same length / marker heuristics as today.
3. Keep behaviour **lazy**: no import or network call unless credentials and opt-in are set.
4. Cap cost per run (max URLs, budget) at the orchestration layer if the vendor charges per request.

## Related code

- `agents/research.py` — `fetch_page(url)` is the single place HTTP text enters the pipeline before `_mark_source_signal` runs on each `raw_sources` item.

## Before implementing

Resolve vendor details: base URL, authentication, pricing, and allowed retention/redaction policy. Do not commit secrets; extend `.env.example` when the integration is real.
