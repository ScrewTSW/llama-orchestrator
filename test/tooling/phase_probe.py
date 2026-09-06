#!/usr/bin/env python3
"""Show the phase timeline the Continue toolbar derives from x_progress.

Reproduces the "stuck on Working" class of bugs. Three prompt shapes matter and
they fail differently, so test all of them:

  short      trivial prompt, eval is instant
  long       long prompt, but repeated text -> llama.cpp KV cache may hit
  miss       long prompt of unique text -> guaranteed cache miss, slow eval

The `miss` shape is the one that exposes stalls: prompt eval can run 30s+ while
/slots reports nothing at all.

Usage:
  python3 phase_probe.py [short|long|miss|all] [--cold] [--repeat N]

  --cold   unload the model first, so the load phase is exercised
  --repeat send the same shape N times back-to-back (warm path regression)
"""
import argparse
import json
import time
import urllib.request

ORCH = "http://127.0.0.1:58108"
MODEL = "Qwen3.8-9B-Q6_K"


def make_prompt(shape: str) -> str:
    if shape == "short":
        return "Say hello."
    if shape == "long":
        # Repeated text: llama.cpp may serve this from KV cache on a re-run.
        return "Summarize: " + ("The quick brown fox jumps over the lazy dog. " * 4000)
    if shape == "miss":
        # Unique tokens defeat the KV cache, forcing a full eval every time.
        nonce = int(time.time() * 1000)
        return "Summarize: " + " ".join(
            f"entry{nonce}x{i} distinct filler phrase kappa lambda mu nu."
            for i in range(6000)
        )
    raise ValueError(f"unknown shape: {shape}")


def derive(xp: dict | None) -> str:
    """Mirror of derivePhaseLabel() in StreamingToolbar.tsx.

    Keep in sync: if the client gains a phase, add it here or this probe will
    report a phase timeline the UI does not actually show.
    """
    if not xp:
        return "Working"
    if xp.get("loading"):
        return "Loading model"
    if xp.get("state") == "prompt eval":
        return "Reading prompt"
    if xp.get("state") == "generating":
        return "Generating"
    if (xp.get("gen") or 0) > 0:
        return "Generating"
    return "Working"


def unload() -> None:
    body = json.dumps({"model": MODEL, "unload": True}).encode()
    req = urllib.request.Request(ORCH + "/orchestrator/cancel", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=30).read()
    except Exception as e:
        print(f"  (unload failed: {e})")
    time.sleep(3)


def run(label: str, prompt: str) -> list[tuple[float, str]]:
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "max_tokens": 16,
    }).encode()
    req = urllib.request.Request(ORCH + "/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.monotonic()
    timeline: list[tuple[float, str]] = []
    last = None
    first_token = None
    frames = 0

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
                frames += 1
                phase = derive(xp)
                if phase != last:
                    timeline.append((dt, phase))
                    last = phase
                continue
            delta = (obj.get("choices") or [{}])[0].get("delta") or {}
            if (delta.get("content") or delta.get("reasoning_content")) and first_token is None:
                first_token = dt

    print(f"\n=== {label} ===")
    if not timeline:
        print("  FAIL: no x_progress frames - toolbar shows 'Working' for the whole request")
    for dt, phase in timeline:
        print(f"  {dt:7.2f}s  {phase}")
    print(f"  first token: {first_token:.2f}s" if first_token else "  first token: n/a")
    print(f"  x_progress frames: {frames}")

    # The symptom being guarded against: silence before the first phase.
    if timeline and timeline[0][0] > 0.5:
        print(f"  FAIL: {timeline[0][0]:.2f}s of dead air before the first phase")
    elif timeline:
        print(f"  ok: first phase at {timeline[0][0]:.2f}s")
    return timeline


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("shape", nargs="?", default="all",
                    choices=["short", "long", "miss", "all"])
    ap.add_argument("--cold", action="store_true", help="unload the model first")
    ap.add_argument("--repeat", type=int, default=1)
    args = ap.parse_args()

    shapes = ["short", "long", "miss"] if args.shape == "all" else [args.shape]
    for shape in shapes:
        if args.cold:
            print(f"\n--- unloading before '{shape}' ---")
            unload()
        for i in range(args.repeat):
            tag = f"{shape} #{i + 1}" if args.repeat > 1 else shape
            run(tag, make_prompt(shape))


if __name__ == "__main__":
    main()
