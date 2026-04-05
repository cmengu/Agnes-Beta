trigger: any message that describes a goal, task, or research request
action: POST http://localhost:8000/run
payload:
  goal: <user_message>
  user_id: <platform_user_id>
  channel: "telegram"
# Optional: clients can use POST /run/stream (text/event-stream) for live status_messages
on_start: "Got it — running constitutional check and starting your research now..."
on_status: stream each entry from status_messages as it arrives (see /run/stream SSE)
on_complete: send final_output from response body
on_error: "Something went wrong: {error}"
