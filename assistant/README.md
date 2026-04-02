# Kronk AI Server — Build Notes & Model Analysis

**Last updated:** 2026-04-02
**Machine:** Framework AMD Ryzen AI 375 (hostname: kronk)
**GPU:** Radeon 8060S (GFX1151) — integrated GPU
**RAM:** 122 GB

---

## Hardware Reality

The Radeon 8060S is an integrated GPU — it has no dedicated VRAM. Instead it carves memory out of system RAM via a mechanism called GTT (Graphics Translation Table). Ollama/ROCm sees this as ~61.9 GiB of "GPU memory." With 122 GB total system RAM, roughly half is available to the GPU.

This is the fundamental constraint everything else flows from.

---

## GPU Backend: ROCm vs Vulkan

### What we expected
The setup guide (written March 2026) warned that ROCm had incomplete support for GFX1150/1151 and recommended Vulkan via a manually-built llama.cpp as the primary inference path. It noted that Vulkan could access ~88 GiB via GTT vs ROCm's more limited pool.

### What actually happened
Ollama 0.19.0 has ROCm support for GFX1151 out of the box. On first install, Ollama immediately detected the GPU and ran inference at 100% GPU via ROCm — no manual configuration needed.

We tried enabling Vulkan anyway (`OLLAMA_VULKAN=1` in the Ollama systemd service) to see if it would unlock more GPU memory as the guide suggested. It did not — Ollama saw the Vulkan flag but still chose ROCm as the preferred backend, and GPU memory stayed at 61.9 GiB.

**Decision:** Use ROCm. It works out of the box and Ollama prefers it. Vulkan is configured as a fallback but isn't active.

---

## Architecture Decisions

### Ollama outside Docker
Ollama runs as a systemd service on the host, not in a Docker container. This was a deliberate choice:
- Getting ROCm device passthrough into Docker requires passing `/dev/kfd`, `/dev/dri`, correct group IDs, and the right Docker capabilities — non-trivial and fragile
- Ollama was already running and verified working with GPU before we started the application layer
- The ollama model store at `/usr/share/ollama/.ollama/models` is root-owned — bind-mounting it into a container requires sudo

The two application containers (`llm_service`, `orchestrator`) use `network_mode: host` so they can reach Ollama on `localhost:11434`.

### network_mode: host
We initially tried using Docker's `host-gateway` feature to let containers reach the host's localhost. It resolved to `172.17.0.1` but Ollama only listens on `127.0.0.1`, so connections were refused. Switching to `network_mode: host` for both containers means they share the host network stack entirely and can reach Ollama directly.

Tradeoff: `network_mode: host` only works on Linux (not Mac/Windows Docker Desktop). Fine for this machine, worth noting if the stack ever moves.

### Service design
- **llm_service** (port 8002): thin Ollama wrapper. Translates the orchestrator's message format into Ollama's API format and streams tokens back as SSE. Keeping this separate means swapping out the LLM backend only touches this service.
- **orchestrator** (port 8000): manages conversation history, serves the UI, owns all routing logic and tool calls.
- **tool_service** (port 8003): external API integrations. Currently weather via Open-Meteo (geocoding + forecast, no API key required).

### Tool integration pattern
Tool calls use rule-based intent detection (regex keyword matching) in the orchestrator rather than LLM-driven function calling. This avoids model capability variance — not all models handle function calling consistently. The orchestrator detects intent, calls the tool, injects the result as a system message, then sends the full context to the LLM. The model's job is only to format the response.

---

## Context Window

The Ollama default context of 131,072 tokens is designed for document analysis workloads. For a home assistant with conversational history, 8,192 tokens is more than enough (~6,000 words of conversation).

Reducing context has two benefits:
1. Smaller KV cache → smaller loaded model size → more fits on GPU
2. Faster prefill on long conversations

**Decision:** Set `num_ctx: 8192` in all Ollama API calls. Configured in `llm_service/main.py`.

---

## Model History

### Initial: llama3.3:70B
- Default context (131K tokens): loaded at **102 GB**, 38% CPU / 62% GPU split, ~2.2 t/s
- Reduced context (8K tokens): loaded at **45 GB**, 100% GPU, still ~2.2 t/s
- The speed didn't improve because the 70B parameter count is the bottleneck, not GPU utilization. This is the hardware ceiling for this model.

### Second: llama3.2:3B
- Loaded at **3.4 GB**, 100% GPU, ~10 t/s — 5x faster
- Drawback: childish responses (*winks*, *giggles*) despite system prompt prohibition
- Went off-topic on unrelated subjects (MagicMirror question turned into Starcraft tangent)

### Third: qwen3:14B (current as of 2026-04-01)
- Loaded at ~9 GB, 100% GPU
- Key issue: thinking mode — model internally reasons using `<think>...</think>` tokens before responding, producing 8-29s TTFT depending on prompt complexity
- Quality is good but responsiveness is poor for a home assistant
- Theatrical flag on reasoning prompt despite explicit system prompt prohibition

---

## Model Benchmark (2026-04-02)

11 models tested against 6 prompts covering factual Q&A, tool use (weather), instruction following, code generation, multi-step reasoning, and math.

Raw benchmark data and full responses: `model_results.md` (auto-generated, do not edit manually).

### Summary

| Model | Avg TTFT | Avg generation | Theatrical flags |
|---|---|---|---|
| `qwen3:14b` | 14.73s | 3.04s | 1 / 6 |
| `qwen2.5:14b` | 0.66s | 3.86s | 0 / 6 |
| `mistral:7b` | 0.32s | 2.47s | 0 / 6 |
| `mistral-nemo:12b` | 0.60s | 0.81s | 0 / 6 |
| `mistral-small:22b` | 0.72s | 3.71s | 0 / 6 |
| `mistral-small:24b` | 0.80s | 7.01s | 1 / 6 |
| `mistral-small3.1:24b` | 1.43s | 5.46s | 1 / 6 |
| `mistral-small3.2:24b` | 1.10s | 3.11s | 0 / 6 |
| `llama3.1:8b` | 0.36s | 1.32s | 0 / 6 |
| `gemma3:12b` | 0.59s | 0.77s | 0 / 6 |
| `phi4:14b` | 0.49s | 4.08s | 1 / 6 |

### Per-prompt timing (TTFT / generation, seconds)

| Model | factual | weather | theatrical | code | reasoning | math |
|---|---|---|---|---|---|---|
| `qwen3:14b` | 8.96 / 2.93 | 6.05 / 2.07 | 7.32 / 0.67 | 22.65 / 1.00 | 28.71 / 10.96 | 14.71 / 0.58 |
| `qwen2.5:14b` | 3.01 / 0.48 | 0.24 / 1.62 | 0.13 / 0.57 | 0.22 / 7.69 | 0.22 / 12.67 | 0.14 / 0.13 |
| `mistral:7b` | 1.46 / 1.51 | 0.15 / 1.00 | 0.07 / 0.64 | 0.06 / 3.92 | 0.11 / 7.38 | 0.06 / 0.36 |
| `mistral-nemo:12b` | 2.62 / 0.38 | 0.35 / 0.75 | 0.13 / 0.41 | 0.14 / 0.75 | 0.20 / 2.50 | 0.14 / 0.10 |
| `mistral-small:22b` | 2.68 / 1.05 | 0.78 / 2.66 | 0.10 / 0.79 | 0.33 / 1.87 | 0.30 / 15.68 | 0.15 / 0.24 |
| `mistral-small:24b` | 3.38 / 3.80 | 0.34 / 2.61 | 0.22 / 0.83 | 0.21 / 12.65 | 0.40 / 21.98 | 0.25 / 0.21 |
| `mistral-small3.1:24b` | 6.99 / 4.00 | 0.34 / 2.77 | 0.26 / 0.90 | 0.25 / 5.56 | 0.44 / 18.36 | 0.30 / 1.16 |
| `mistral-small3.2:24b` | 5.13 / 1.35 | 0.33 / 2.54 | 0.21 / 0.87 | 0.22 / 4.61 | 0.43 / 9.09 | 0.29 / 0.20 |
| `llama3.1:8b` | 1.45 / 0.84 | 0.19 / 0.82 | 0.11 / 0.38 | 0.17 / 4.02 | 0.16 / 1.55 | 0.11 / 0.34 |
| `gemma3:12b` | 2.28 / 0.20 | 0.29 / 0.90 | 0.20 / 0.62 | 0.27 / 1.80 | 0.27 / 0.94 | 0.23 / 0.16 |
| `phi4:14b` | 1.95 / 0.39 | 0.27 / 1.23 | 0.19 / 1.13 | 0.18 / 6.37 | 0.22 / 15.02 | 0.14 / 0.34 |

---

## Model Recommendation

### Best fit: `qwen2.5:14b`

- **No thinking overhead.** Unlike qwen3:14b (avg TTFT 14s+), qwen2.5:14b responds immediately. The difference is stark on the code and reasoning prompts where qwen3 spent 22-29s in its thinking phase before generating a single token.
- **Zero theatrical flags.** Respected the system prompt across all prompts. qwen3:14b, phi4:14b, mistral-small:24b, and mistral-small3.1:24b all produced `*emote*` patterns on the reasoning prompt despite explicit prohibition.
- **Strong tool use.** Weather prompt completed with 0.24s TTFT — essentially no latency introduced by the model after the tool result was injected.
- **Quality ceiling at 14B.** On the reasoning prompt, qwen2.5:14b produced a detailed, well-structured response. Faster models (llama3.1:8b at 1.55s gen, mistral-nemo:12b at 2.50s) were noticeably shallower.

To switch: change `MODEL_NAME=qwen2.5:14b` in `docker-compose.yml`.

### Strong alternative: `mistral-nemo:12b`

The standout of the Mistral testing. Sub-0.15s TTFT on most prompts, zero theatrical flags, fast generation. Tradeoff: at 12B, reasoning depth is lower than qwen2.5:14b on complex multi-step prompts. Worth a real-world trial if responsiveness matters more than answer depth.

### Mistral family summary

| Model | Verdict |
|---|---|
| `mistral-small3.2:24b` | Best Mistral overall. No theatrical flags, faster reasoning gen than 3.1. High factual TTFT (5s) is a cold-cache artifact. |
| `mistral-nemo:12b` | Best Mistral for speed. Fastest TTFT in the field, zero flags, good for assistant workloads. |
| `mistral-small:22b` | Solid but outclassed by 3.2 revision and mistral-nemo. |
| `mistral-small:24b` / `3.1:24b` | Theatrical flags, slower or no clear quality advantage. Not recommended. |
| `mistral:7b` | Reliable fallback, shows its age vs newer 7-8B options. |

### Runner-up (speed-first): `llama3.1:8b`

If 14B feels slow in daily use, llama3.1:8b is the best smaller option. Meta built tool use directly into this model's training, it's consistently fast, and produced zero theatrical flags. Ceiling is lower but it punches above its weight.

### Avoid: `qwen3:14b` without `/no_think`

14s+ average TTFT is unacceptable for a home assistant. If you want to keep it, append `/no_think` to the system prompt — but at that point qwen2.5:14b is a better choice.

---

## What's Still on the Table

- **Switch to qwen2.5:14b** — one-line change in `docker-compose.yml`, benchmark strongly supports it
- **Query routing** — route simple/fast queries to a small model, complex ones to a larger one; architecture already supports this via the `model` override on `/message`
- **Quantization** — a Q2/Q3 version of llama3.3:70b would be ~25-30 GB and might fit in GPU at 100% while retaining more quality than 14B models
- **File context** — allow attaching files to prompts for additional context (planned)
- **Voice pipeline** — STT (Whisper.cpp), TTS (Piper), wake word (openWakeWord); stubbed in the current UI
- **More tools** — Philips Hue, calendar, home automation
