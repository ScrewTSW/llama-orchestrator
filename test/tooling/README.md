# Phase telemetry probes

Manual tools for the `x_progress` SSE channel that drives the Continue
toolbar's phase label ("Loading model" / "Reading prompt" / "Generating").

They need a running orchestrator and a real model, so they are not unit tests
and are not wired into CI. Run them by hand after touching `router.py`'s
progress loops or `StreamingToolbar.tsx`.

## Scripts

| Script | Answers |
| --- | --- |
| `phase_probe.py` | What phases does the toolbar show, and when? |
| `raw_probe.py` | What are the actual frame payloads? |
| `slot_watch.py` | Does any `/slots` field move during eval? |

## Regression check after changing the progress loops

```bash
python3 phase_probe.py all --cold      # exercises load + eval + generate
python3 phase_probe.py miss --repeat 3 # warm path, back-to-back
```

Expected: every run reports its first phase at **< 0.5s**. Anything above that
is the dead-air bug — the toolbar sits on "Working".

## The two bugs these were written for

**1. `create_task` does not start the coroutine.** `asyncio.create_task` only
schedules. On a warm hit `_load_model` returns without ever yielding, so
`kl_task.cancel()` fired before the loop body ran a single line and the client
got no frame at all. Fixed by emitting the first frame **inline** in the
handler, leaving the task poll-only. If you move a first frame back inside a
loop, `phase_probe.py miss --repeat 3` will catch it.

**2. Prompt eval has no live progress.** `n_prompt_tokens_processed` from
`/slots` stays `0` for the entire eval and is only populated afterwards — see
`slot_watch.py`, which recorded no field changing across a 29s eval. So a
long cache-miss prompt legitimately shows a static "Reading prompt" for 30s.
That is a llama-server limitation, not an orchestrator bug: do not "fix" it by
faking interpolated progress. Re-run `slot_watch.py` after a llama-server
upgrade to see if a live counter has appeared.

## Prompt shapes

`short` is instant. `long` uses repeated text, so llama.cpp's KV cache often
serves it and eval finishes immediately — it will *not* reproduce eval stalls.
`miss` generates unique tokens with a timestamp nonce and always forces a full
eval. Use `miss` for anything about prompt-eval timing.
