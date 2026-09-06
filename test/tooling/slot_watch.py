#!/usr/bin/env python3
"""Poll llama-server /slots during a prompt eval and report which fields move.

Written to answer: "can the orchestrator show real prompt-eval progress?"
Answer as of 2026-08-27: no. During a 29s eval, no numeric slot field changed;
n_prompt_tokens_processed stayed 0 and only filled in after eval completed.

Re-run this after any llama-server upgrade - if a field starts moving, the
orchestrator can finally drive a real progress bar instead of a static phase.

Usage: python3 slot_watch.py [slot_port]
  Find the port with: curl -s localhost:58108/orchestrator/status
"""
import json
import sys
import threading
import time
import urllib.request

ORCH = "http://127.0.0.1:58108"
MODEL = "Qwen3.8-9B-Q6_K"

stop = threading.Event()


def fire_request() -> None:
    """Cache-miss prompt, long enough that eval takes tens of seconds."""
    nonce = int(time.time() * 1000)
    prompt = " ".join(
        f"entry{nonce}x{i} distinct filler phrase kappa lambda mu nu." for i in range(6000)
    )
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "max_tokens": 8,
    }).encode()
    req = urllib.request.Request(ORCH + "/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=900) as r:
            for _ in r:
                pass
    except Exception as e:
        print(f"request failed: {e}")
    finally:
        stop.set()


def poll(port: int) -> None:
    t0 = time.monotonic()
    seen: dict[str, object] = {}
    while not stop.is_set():
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/slots", timeout=2) as r:
                slots = json.load(r)
            if slots:
                s = slots[0]
                nums = {k: v for k, v in s.items() if isinstance(v, (int, float)) and v}
                changed = {k: v for k, v in nums.items() if seen.get(k) != v}
                if changed:
                    print(f"{time.monotonic() - t0:7.2f}s  {changed}")
                    seen.update(nums)
        except Exception:
            pass
        time.sleep(0.5)


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 58120
    threading.Thread(target=fire_request, daemon=True).start()
    poll(port)
    print("\nOnly fields printed above changed during the run.")
    print("If n_prompt_tokens_processed is absent, live eval progress is still unavailable.")


if __name__ == "__main__":
    main()
