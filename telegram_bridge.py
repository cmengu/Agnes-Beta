#!/usr/bin/env python3
"""
Poll Telegram for messages and forward each text to AgnesOps, then send the reply.

- Default: uses POST /run/stream so each new status line can be texted to you in near
  real time. Set TELEGRAM_LIVE_STATUS=0 to only send one "Running…" then the final
  report via POST /run (fewer messages, also most reliable if SSE misbehaves).

Run in parallel with the API:
  Terminal 1: uvicorn server:app --host 127.0.0.1 --port 8000
  Terminal 2: python telegram_bridge.py

Requires TELEGRAM_BOT_TOKEN and AGNES_API_BASE in .env (see .env.example).

Environment:
  AGNES_RUN_TIMEOUT_SEC — max seconds waiting for /run or /run/stream (default 900).
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time

import httpx
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    print("Set TELEGRAM_BOT_TOKEN in .env", file=sys.stderr)
    sys.exit(1)

AGNES_BASE = os.environ.get("AGNES_API_BASE", "http://127.0.0.1:8000").rstrip("/")
TG = f"https://api.telegram.org/bot{TOKEN}"

CHUNK = 4000
_TIMEOUT_SEC = float(os.environ.get("AGNES_RUN_TIMEOUT_SEC", "900"))
RUN_TIMEOUT = httpx.Timeout(_TIMEOUT_SEC, connect=30.0)

LIVE_DEFAULT = os.getenv("TELEGRAM_LIVE_STATUS", "1").lower() in (
    "1",
    "true",
    "yes",
    "on",
)

# Maps internal agent status prefixes to user-visible phrases.
# Prefixes are matched with str.startswith(); first match wins.
# None = suppress the message entirely.
# NOTE: "Writer: WARNING" is handled by the special case in _user_friendly_status
# BEFORE this map is consulted — it must NOT appear here (see D1 in Architecture Overview).
_STATUS_MAP: tuple[tuple[str, str | None], ...] = (
    ("Coordinator:", "Preparing your research plan…"),
    ("Research: running", "Researching your question…"),
    ("Research: synthesising", "Synthesising findings…"),
    ("Writer:", "Writing your report…"),
    ("Critic: revision", "Refining the answer…"),
)


def _user_friendly_status(msg: str) -> str | None:
    """Return a user-friendly phrase for this internal status, or None to suppress.

    Writer: WARNING messages are forwarded verbatim before the map is consulted.
    They must NOT appear in _STATUS_MAP — see D1 in the plan Architecture Overview.
    """
    # Forward truncation warnings verbatim — they carry specific user-relevant content.
    # This check MUST remain above the _STATUS_MAP loop so that "Writer:" in the map
    # does not absorb these messages with a generic phrase.
    if msg.startswith("Writer: WARNING"):
        return msg
    for prefix, friendly in _STATUS_MAP:
        if msg.startswith(prefix):
            return friendly
    return None


def _strip_technical_metadata(text: str) -> str:
    """Remove the quality badge header and run footer from final_output.

    Preserves the body, error notices (⚠️ prefix), and the travel disclaimer blockquote.
    Input structure: badge\\n\\n[disclaimer\\n\\n]body[\\n\\n---\\n*Run:...]
    """
    # Remove quality badge (first paragraph starting with "> **AgnesOps")
    if text.startswith("> **AgnesOps"):
        end = text.find("\n\n")
        if end != -1:
            text = text[end + 2 :]
    # Remove run footer
    footer_idx = text.find("\n\n---\n*Run:")
    if footer_idx != -1:
        text = text[:footer_idx]
    return text.strip()


def _api_unreachable_message() -> str:
    return (
        f"Cannot reach AgnesOps at {AGNES_BASE} (connection refused).\n\n"
        "Fix:\n"
        "1) Terminal A — start the API:\n"
        "   uvicorn server:app --host 127.0.0.1 --port 8000\n"
        "2) Check: curl -s http://127.0.0.1:8000/health\n"
        "3) If you use another host/port, set AGNES_API_BASE in .env to match, then restart this bridge."
    )


def send_message(chat_id: int, text: str) -> None:
    """Send to Telegram; retry on 429 flood control."""
    with httpx.Client(timeout=60.0) as client:
        for i in range(0, len(text), CHUNK):
            chunk = text[i : i + CHUNK]
            for attempt in range(8):
                r = client.post(f"{TG}/sendMessage", json={"chat_id": chat_id, "text": chunk})
                try:
                    data = r.json()
                except Exception:
                    r.raise_for_status()
                    raise
                if r.status_code == 429 and data.get("error_code") == 429:
                    wait = float(data.get("parameters", {}).get("retry_after", 3))
                    print(f"Telegram 429, sleeping {wait}s", flush=True)
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                if not data.get("ok"):
                    raise RuntimeError(data.get("description", data))
                break


def _parse_sse_events(resp: httpx.Response):
    """
    Yield decoded JSON payloads from SSE frames (split on blank line).
    Handles chunks split mid-frame (iter_lines alone can miss the final event).
    """
    buf = ""
    for piece in resp.iter_text():
        buf += piece
        while True:
            sep = buf.find("\n\n")
            if sep == -1:
                break
            frame = buf[:sep]
            buf = buf[sep + 2:]
            for raw_line in frame.split("\n"):
                line = raw_line.strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].lstrip()
                if not payload:
                    continue
                try:
                    yield json.loads(payload)
                except json.JSONDecodeError as e:
                    print("SSE JSON error:", e, "|", payload[:240], file=sys.stderr)

    tail = buf.strip()
    if tail:
        for raw_line in tail.split("\n"):
            line = raw_line.strip()
            if line.startswith("data:"):
                payload = line[5:].lstrip()
                try:
                    yield json.loads(payload)
                except json.JSONDecodeError as e:
                    print("SSE tail JSON error:", e, "|", payload[:240], file=sys.stderr)


def _typing_loop(chat_id: int, stop_event: threading.Event) -> None:
    """Send typing action every ~4s until stop_event is set (daemon thread)."""
    while not stop_event.wait(4.0):
        try:
            with httpx.Client(timeout=10.0) as c:
                c.post(
                    f"{TG}/sendChatAction",
                    json={"chat_id": chat_id, "action": "typing"},
                )
        except Exception:
            pass


def run_via_stream(chat_id: int, goal: str) -> None:
    """Call /run/stream, forward status deltas to Telegram, then final_output."""
    body = {
        "goal": goal,
        "user_id": str(chat_id),
        "channel": "telegram",
    }
    send_message(chat_id, "On it…")
    _sent_statuses: set[str] = set()
    got_final = False
    _stop_typing = threading.Event()
    _typing_thread = threading.Thread(
        target=_typing_loop, args=(chat_id, _stop_typing), daemon=True
    )
    _typing_thread.start()
    try:
        last_status_t = time.time()
        try:
            with httpx.Client(timeout=RUN_TIMEOUT) as run_client:
                with run_client.stream(
                    "POST",
                    f"{AGNES_BASE}/run/stream",
                    json=body,
                ) as resp:
                    try:
                        resp.raise_for_status()
                    except httpx.HTTPStatusError as e:
                        send_message(
                            chat_id,
                            f"AgnesOps /run/stream HTTP {e.response.status_code}: {e.response.text[:500]}",
                        )
                        return

                    for evt in _parse_sse_events(resp):
                        if not isinstance(evt, dict):
                            continue
                        print(
                            str(evt)[:180] + ("…" if len(str(evt)) > 180 else ""),
                            flush=True,
                        )

                        deltas = evt.get("delta_status") or []
                        if deltas:
                            friendly_msgs = []
                            for d in deltas:
                                friendly = _user_friendly_status(str(d))
                                if friendly and friendly not in _sent_statuses:
                                    friendly_msgs.append(friendly)
                                    _sent_statuses.add(friendly)
                            if friendly_msgs:
                                send_message(chat_id, "\n".join(friendly_msgs))
                            last_status_t = time.time()
                        elif time.time() - last_status_t > 120:
                            send_message(
                                chat_id,
                                "Still running (no new status for 120s)…",
                            )
                            last_status_t = time.time()

                        if evt.get("done"):
                            got_final = True
                            if evt.get("error"):
                                err = evt["error"]
                                out = evt.get("final_output") or ""
                                text = f"{err}\n\n{out}" if out else str(err)
                            else:
                                raw = evt.get("final_output") or ""
                                text = _strip_technical_metadata(raw) or "No response received."
                                all_msgs = evt.get("status_messages") or []
                                if any("truncated at token limit" in m for m in all_msgs):
                                    text += (
                                        "\n\n⚠️ *Note: this report may be incomplete — "
                                        "the draft reached the length limit.*"
                                    )
                            send_message(chat_id, text)
                            return

        except httpx.ReadTimeout:
            send_message(
                chat_id,
                f"Read timeout after {_TIMEOUT_SEC:.0f}s — run may still be going on the server. "
                "Try TELEGRAM_LIVE_STATUS=0 or raise AGNES_RUN_TIMEOUT_SEC.",
            )
            return
        except httpx.ConnectError:
            send_message(chat_id, _api_unreachable_message())
            return
        except httpx.HTTPError as e:
            send_message(chat_id, f"HTTP error on stream: {e}")
            return

        if not got_final:
            send_message(
                chat_id,
                "Stream ended without a final `done` event (SSE parse or server issue). "
                "Check the `uvicorn` terminal for errors. For a simpler path, set "
                "TELEGRAM_LIVE_STATUS=0 in `.env` and restart this bridge — it uses a single "
                "POST /run and sends one reply when the run finishes.",
            )
    finally:
        _stop_typing.set()


def run_via_sync_post(client: httpx.Client, chat_id: int, goal: str) -> None:
    send_message(chat_id, "Running your request on AgnesOps…")
    body = {
        "goal": goal,
        "user_id": str(chat_id),
        "channel": "telegram",
    }
    try:
        run_resp = client.post(f"{AGNES_BASE}/run", json=body, timeout=RUN_TIMEOUT)
        run_resp.raise_for_status()
        data = run_resp.json()
    except httpx.ConnectError:
        send_message(chat_id, _api_unreachable_message())
        return
    except httpx.HTTPError as e:
        send_message(chat_id, f"HTTP error calling AgnesOps: {e}")
        return
    except Exception as e:
        send_message(chat_id, f"Error: {e}")
        return

    if data.get("error"):
        err = data["error"]
        body_out = data.get("final_output") or ""
        out = f"{err}\n\n{body_out}" if body_out else str(err)
    else:
        raw = data.get("final_output") or ""
        out = _strip_technical_metadata(raw) or "No response received."
        all_msgs = data.get("status_messages") or []
        if any("truncated at token limit" in m for m in all_msgs):
            out += (
                "\n\n⚠️ *Note: this report may be incomplete — "
                "the draft reached the length limit.*"
            )

    send_message(chat_id, out)


def poll_loop() -> None:
    offset = 0
    mode = "live (SSE)" if LIVE_DEFAULT else "batch (/run only)"
    print(
        f"Telegram bridge polling… API={AGNES_BASE} | status={mode} | timeout={_TIMEOUT_SEC}s",
        flush=True,
    )
    with httpx.Client(timeout=httpx.Timeout(65.0, connect=30.0)) as client:
        while True:
            try:
                r = client.get(
                    f"{TG}/getUpdates",
                    params={"timeout": 50, "offset": offset},
                )
                r.raise_for_status()
                payload = r.json()
                if not payload.get("ok"):
                    print("getUpdates:", payload, flush=True)
                    time.sleep(2)
                    continue

                for upd in payload.get("result", []):
                    offset = upd["update_id"] + 1
                    msg = upd.get("message")
                    if not msg or "text" not in msg:
                        continue

                    chat_id = msg["chat"]["id"]
                    text = (msg["text"] or "").strip()
                    if not text:
                        continue

                    if text.startswith("/"):
                        if text in ("/start", "/help"):
                            send_message(
                                chat_id,
                                "Send a research goal as a normal message.\n"
                                "Live updates: default (TELEGRAM_LIVE_STATUS=1).\n"
                                "Most reliable for long runs: TELEGRAM_LIVE_STATUS=0 (one /run only).",
                            )
                        continue

                    try:
                        if LIVE_DEFAULT:
                            run_via_stream(chat_id, text)
                        else:
                            run_via_sync_post(client, chat_id, text)
                    except Exception as e:
                        print("run error:", repr(e), file=sys.stderr)
                        try:
                            send_message(chat_id, f"Error: {e}")
                        except Exception:
                            pass

            except httpx.HTTPError as e:
                print("poll HTTP error:", e, flush=True)
                time.sleep(3)


if __name__ == "__main__":
    poll_loop()
