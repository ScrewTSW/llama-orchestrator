#!/usr/bin/env python3
"""Dump raw x_progress frames with arrival times.

Use when phase_probe.py says a phase is wrong and you need to see the actual
payloads. The three emitters in router.py send *different fields* - this makes
that visible:

  keepalive_loop      loading: true            (never `state`)
  prompt_eval_loop    state, prompt_total      (never `loading`)
  streaming proxy     gen, tok_s, cached       (neither)

Usage: python3 raw_probe.py [short|long|miss]
"""
import json
import sys
import time
import urllib.request

ORCH = "http://127.0.0.1:58108"
MODEL = "Qwen3.8-9B-Q6_K"
WATCH = ("loading", "state", "gen", "prompt", "prompt_total", "tok_s", "cached")


def make_prompt(shape: str) -> str:
    if shape == "short":
        return "Say hello."
    if shape == "long":
        return "Summarize: " + ("The quick brown fox jumps over the lazy dog. " * 4000)
    nonce = int(time.time() * 1000)
    return "Summarize: " + " ".join(
        f"entry{nonce}x{i} distinct filler phrase kappa lambda mu nu." for i in range(6000)
    )


def main() -> None:
    shape = sys.argv[1] if len(sys.argv) > 1 else "miss"
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": make_prompt(shape)}],
        "stream": True,
        "max_tokens": 16,
    }).encode()
    req = urllib.request.Request(ORCH + "/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.monotonic()
    n = 0
    with urllib.request.urlopen(req, timeout=900) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data: "):
                continue
            payload = line[6:]
            if payload == "[DONE]":
                break
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            dt = time.monotonic() - t0
            xp = obj.get("x_progress")
            if xp is not None:
                n += 1
                shown = {k: xp[k] for k in WATCH if k in xp}
                print(f"{dt:7.2f}s  frame#{n:<4} {shown}")
            else:
                delta = (obj.get("choices") or [{}])[0].get("delta") or {}
                if delta.get("content") or delta.get("reasoning_content"):
                    print(f"{dt:7.2f}s  <token>")
    print(f"\ntotal x_progress frames: {n}")


if __name__ == "__main__":
    main()
