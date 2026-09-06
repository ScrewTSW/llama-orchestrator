# llama.cpp Model Orchestrator

A lightweight proxy that dynamically loads and unloads `llama-server` instances behind a
single OpenAI-compatible endpoint, for single-GPU setups where multiple tools (Perplexica,
Continue, Claude Code, SillyTavern) share one machine and one GPU.

Send a request with a `model` field — the model loads on demand, gets evicted when idle,
and your clients never know any of it happened.

## Features

- **Transparent model loading** — clients just send requests; models load on demand from the `model` field
- **VRAM-aware auto-configuration** — per-model GPU layer count, context size and KV placement, computed from GGUF metadata
- **OpenAI-compatible API** — drop-in replacement for OpenAI endpoints, incl. the Responses API
- **Anthropic Messages API** — `/v1/messages` proxied for Anthropic-compatible clients
- **KV cache quantization** — `q8_0`/`q4_0` split for aggressive memory savings with near-lossless quality
- **KV cache placement** — `auto`/`vram`/`ram` modes; partial-offload models keep KV in system RAM
- **KV cache persistence** — session state survives load/unload cycles via `--slot-save-path`
- **Prompt caching** — `--cache-reuse` for agentic clients that repeat system prompts
- **Context shifting** — `--context-shift` (on by default) prevents hard failures on long conversations
- **Idle timeout reaper** — automatically unloads models after configurable inactivity
- **Streaming support** — full SSE streaming proxy for chat completions, with mid-stream OOM/failure detection
- **Request cancellation** — `POST /orchestrator/cancel` aborts a running generation (e.g. llama.cpp `--abort-all` slot kill)
- **OOM surfacing** — VRAM exhaustion at load time or mid-generation returns a structured error, not a hung request
- **Embeddings** — opt-in per model via `embeddings: true`
- **CLI tools** — `--info`, `--autoconf`, `--register` for model analysis and config generation

## Repository Layout

```
orchestrator.py      Core service: model lifecycle, VRAM planning, health checks, reaper
router.py            OpenAI-compatible HTTP layer, streaming proxy, KV-cache persistence
cache.py             KVCacheManager (slot state save/restore via the llama-server /slots API)
analyzer.py          GGUF metadata reader + heuristic VRAM/context analyzer
config.example.yaml  Example configuration - copy to config.yaml (which is git-ignored)
llama-orchestrator.service   systemd unit template
templates/*.jinja    Chat templates for models that need one
test/tooling/        Diagnostic probes (phase timing, raw slot watch)
```

## Quick Start

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# Run directly (creates a default config.yaml if missing)
venv/bin/python3 orchestrator.py

# Or with an explicit config
venv/bin/python3 orchestrator.py /path/to/config.yaml
```

The service listens on `http://localhost:58108` by default.

### Prerequisites

| Requirement | Notes |
|---|---|
| `llama-server` binary | **[ik_llama.cpp](https://github.com/ikawrakow/ik_llama.cpp) is the preferred build** — it is what this orchestrator was developed and tested against, and it ships the flags this project relies on (`--fit-target`, `--context-shift`, `--slot-save-path`, `--cache-reuse`, `/v1/messages`). Upstream llama.cpp works, but lagging builds may lack some of these; unsupported flags cause load failures, so match your binary to the flag set. |
| NVIDIA GPU + drivers | VRAM is read via NVML (`nvidia-smi`); the GPU must be visible to the service user |
| Python 3.10+ | `aiohttp`, `pyyaml` (see `requirements.txt`) |
| `model_dir` | A directory scanned recursively for `.gguf` files |

`ik_llama.cpp` is a feature-rich fork of llama.cpp; the `llama_server_bin` config entry
points at whichever binary you built. In the reference deployment it is:

```
/opt/ik_llama.cpp/build/bin/llama-server
```

## How It Works

Clients (Continue, Perplexica, etc.) send requests to the orchestrator with a `model` field, just like they would to OpenAI. The orchestrator:

1. Extracts the `model` field from the request body
2. Fuzzy-matches it to a `.gguf` file in `model_dir`
3. If the model isn't loaded, spawns a `llama-server` instance (evicting the least-recently-used model if at `max_loaded_models`)
4. Waits for the llama-server health check to pass (scans the log for OOM/CUDA failure signatures)
5. Proxies the request (including streaming) to that instance
6. Tracks activity; after `idle_timeout` seconds the reaper unloads the model (saving KV state first)

The **first request** to a cold model takes as long as model load time (often 10–60s). Subsequent requests are instant.

### VRAM-Aware Auto-Configuration

When `ngl: -1` and `ctx_size: 0` (defaults), the planner:

- Reads GGUF metadata (layer count, KV heads, head dim, max context, embedding dim)
- Reads GPU VRAM via NVML
- Computes per-model: optimal GPU layers, safe context size, KV placement
- **Full-offload models:** all layers on GPU; remaining VRAM filled with KV cache. A *configured* `ctx_size` is always respected — if the KV doesn't fit, it spills to system RAM with a warning instead of being silently shrunk.
- **Partial-offload models:** maximizes GPU layers, keeps KV in system RAM so context can grow toward the model's native limit

Hybrid (SSM + attention) models get special handling: their small KV footprint means `parallel: 1` is forced and the KV split between GPU/CPU is computed per cache type.

### KV Cache Placement (`kv_location`)

| Value | Behavior |
|-------|----------|
| `auto` | VRAM for full-offload, RAM for partial-offload |
| `vram` | Always VRAM (fastest attention, limits context) |
| `ram` | Always system RAM (maximizes context toward model native limit) |

When `ram` is requested but model + full KV fits in VRAM, the orchestrator logs a warning and keeps KV in VRAM — no reason to slow attention down when everything fits.

### Per-Model Overrides

Generate with `--autoconf`/`--register`, or hand-edit `model_overrides`:

```yaml
model_overrides:
  "Some-9B.Q6_K":
    ctx_size: 65536
    kv_location: vram
  "Big-27B.Q5_K_M":
    ngl: 44
    kv_location: ram
```

## CLI Tools

All three use the same heuristic engine (GGUF metadata + NVML):

```bash
# All discovered models with VRAM analysis
python3 orchestrator.py --info

# Detailed single-model analysis with context-tier breakdown
python3 orchestrator.py --info "Qwen3.5-9B"

# Compute optimal overrides and append them to config.yaml
python3 orchestrator.py --autoconf "27B-Claude"

# Register a new model (full path or fuzzy name)
python3 orchestrator.py --register /path/to/model.gguf
```

## Configuration

Copy `config.example.yaml` to `config.yaml` and edit it (a minimal one is
auto-created on first launch if missing). Your local `config.yaml` is git-ignored;
`config.example.yaml` is the tracked copy that holds the defaults.

```yaml
host: "0.0.0.0"
port: 58108
llama_server_bin: "/opt/ik_llama.cpp/build/bin/llama-server"
model_dir: "/path/to/models"      # scanned recursively for *.gguf
kv_cache_dir: "/tmp/llama_kv_cache"
internal_port_start: 58120        # llama-server instances take ports in
internal_port_end: 58199          # [start, end]
idle_timeout: 300                 # seconds before unloading idle models
health_timeout: 300               # max seconds to wait for server readiness
max_loaded_models: 1              # concurrent models (1 for single GPU)
default_max_tokens: "ctx/4"       # or a plain integer
gpu:
  overhead_mb: 1024               # reserved for driver/desktop etc.
  safety_margin_mb: 512           # extra safety buffer
  # total_mb: 24000               # override auto-detected VRAM

defaults:                          # applied to every model unless overridden
  ngl: -1                         # -1 = auto-calculate
  ctx_size: 0                     # 0 = auto-maximize per model
  kv_location: auto               # auto | vram | ram
  flash_attn: true
  cache_type_k: q8_0              # KV quantization (K)
  cache_type_v: q4_0              # KV quantization (V)
  cache_reuse: 256                # prompt-cache reuse threshold (tokens)
  context_shift: true             # --context-shift on (default true)
  fit_target: 256                 # --fit-target headroom in MB
  embeddings: false               # opt-in /v1/embeddings

model_overrides: {}               # see above
```

Per-model override keys (in `defaults` or `model_overrides`): `ngl`, `ctx_size`,
`n_gpu_layers_kv`, `kv_location`, `flash_attn`, `cache_type_k`, `cache_type_v`,
`cache_reuse`, `context_shift`, `fit_target`, `parallel`, `threads`, `no_mmap`,
`embeddings`, `rope_scaling`, `reasoning_format`, `reasoning`, `reasoning_budget`,
`chat_template_kwargs`, `jinja`, `chat_template_file`, `stop`, `min_max_tokens`,
`merge_roles`, `health_timeout`.

### Model Name Resolution

For a file `/path/models/publisher/MyModel-9B.Q5_K_M.gguf`, any of these match (priority order):

1. Exact stem: `MyModel-9B.Q5_K_M`
2. Stem without quant suffix: `MyModel-9B`
3. Substring: `MyModel`
## API

### Management

| Route | Method | Purpose |
|---|---|---|
| `/health` | GET | Orchestrator health check |
| `/v1/models` | GET | OpenAI model list |
| `/models` | * | llama.cpp model list |
| `/orchestrator/status` | GET | Loaded models, PIDs, idle times |
| `/orchestrator/load` | POST | Preload a model: `{"model": "name"}` |
| `/orchestrator/unload` | POST | Unload a model: `{"model": "name"}` |
| `/orchestrator/cancel` | POST | Abort the current generation on a model |

### Inference (proxied to llama-server)

| Route | Notes |
|---|---|
| `/v1/{path:.*}` | OpenAI catch-all: `/v1/chat/completions`, `/v1/embeddings`, `/v1/responses`, `/v1/messages`, … |
| `/completion`, `/chat/completions` | llama.cpp native text completion |
| `/responses` | OpenAI Responses API |
| `/embedding`, `/tokenize`, `/detokenize` | token/embedding helpers |
| `/props` | server properties (context size, samplers) |
| `/slots`, `/slots/{id}` | slot state |

All inference endpoints auto-load the model from the request's `model` field — no
explicit load/unload needed. Native endpoints without a `model` field fall back to the
currently loaded model (when exactly one is active) or `default_model` in config.

### OpenAI Compatibility

| Feature | Status |
|---------|--------|
| Chat Completions + SSE streaming | Supported |
| Tool / function calling | Supported (by upstream llama-server) |
| Structured output (`response_format: json_schema`) | Supported |
| Responses API (`/v1/responses`) | Supported |
| Embeddings (`/v1/embeddings`) | Supported, opt-in (`embeddings: true`) |
| Anthropic Messages API (`/v1/messages`) | Supported (ik_llama.cpp) |
| Vision / multimodal, audio transcription | Supported (with mmproj / whisper model) |
| API key | Accepted but not enforced — use `not-needed` |

## Client Configuration

All clients point at `http://localhost:58108/v1` (OpenAI mode) or `http://localhost:58108`
(llama.cpp native / Anthropic mode) with any API key.

### Continue (VS Code / VSCodium)

```yaml
models:
  - name: Qwen 9B (Local)
    provider: openai
    apiBase: http://localhost:58108/v1
    apiKey: not-needed
    model: MyModel-9B.Q5_K_M      # any fuzzy match works
    roles: [chat, edit, apply]
    capabilities: [tool_use]
```

Switching models in the UI triggers auto evict/load on the orchestrator side.

### Perplexica

Settings → add an **OpenAI** provider: Base URL `http://localhost:58108/v1` (or
`http://host.docker.internal:58108/v1` from Docker), API key `not-needed`, then add your
model name as a custom chat model.

### Claude Code (Anthropic mode)

```bash
export ANTHROPIC_BASE_URL=http://localhost:58108
export ANTHROPIC_API_KEY=not-needed
```

Claude Code is built for the Anthropic API; local models need solid tool-use support for
agentic features. Best for experimentation or privacy-sensitive setups — many people keep
Claude Code on the real API and use local models for secondary tasks.

### SillyTavern

- **Chat Completion mode** → Custom OpenAI endpoint `http://localhost:58108/v1`, key `not-needed`. Model list auto-populates from `/v1/models`.
- **Text Completion mode** → llama.cpp source, Server URL `http://localhost:58108`. Gives access to more samplers (`min_p`, `dry`, `xtc`, `nsigma`). The model resolves to the one currently loaded (or `default_model`).

## systemd Service

`llama-orchestrator.service` is a template — set `WorkingDirectory`/`ExecStart` paths and
the service user (must own the orchestrator dir, `kv_cache_dir`, and be able to read
`model_dir` and `/dev/nvidia*`). Then:

```bash
sudo cp llama-orchestrator.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now llama-orchestrator

sudo systemctl status llama-orchestrator
sudo journalctl -u llama-orchestrator -f
```

## Troubleshooting

- **Model not found** — `GET /v1/models` lists exact IDs; fuzzy matching covers the rest.
- **Model fails to load** — `GET /orchestrator/status` + `journalctl`. The health-check
  log scanner surfaces the actual reason (OOM, CUDA errors, unknown flags) to the client
  as a 503. Common causes: model too large for VRAM, unsupported flags on an old
  `llama-server` build, unwritable `kv_cache_dir`, port conflict.
- **VRAM out during generation** — returns a structured 502 / SSE error frame with the
  upstream text; lower `ctx_size`, drop `fit_target`, or enable `context_shift`.
- **Slow first request** — expected (model load). Preload with `POST /orchestrator/load`.
- **Model keeps unloading** — raise `idle_timeout`.
- **Unknown argument errors** — your `llama-server` predates a flag (e.g. `--fit-target`).
  Build a newer [ik_llama.cpp](https://github.com/ikawrakow/ik_llama.cpp) or set the
  offending config key to `false`/null to skip the flag.
- **VRAM analysis** — `python3 orchestrator.py --info` (all models) or `--info "name"`.

## License

[GNU General Public License v3.0](LICENSE) — see [LICENSE](LICENSE) for the full text.
