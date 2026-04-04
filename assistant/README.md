# Kronk AI Server — Build Notes & Model Analysis

**Last updated:** 2026-04-03 (health service, Infisical, Garmin auth)
**Machine:** Framework AMD Ryzen AI 375 (hostname: kronk)
**GPU:** Radeon 8060S (GFX1151) — integrated GPU
**RAM:** 122 GB

---

## Hardware Reality

The Radeon 8060S is an integrated GPU — it has no dedicated VRAM. Instead it carves memory out of system RAM via a mechanism called GTT (Graphics Translation Table). Ollama/ROCm initially saw ~61.9 GiB of "GPU memory" — the ROCm driver default of roughly half of total system RAM.

The GTT ceiling has since been raised to **~101.6 GB** (see below).

### Raising the GTT ceiling

The default ~50% limit is a driver policy, not a hardware constraint. It can be raised via kernel boot parameters.

**What does NOT work:**
- `amdgpu.gttsize` — deprecated, throws a kernel warning, ignored on modern kernels
- `amdttm.pages_limit` — the module is named `ttm`, not `amdttm`; this parameter is silently ignored

**What works (kernel 6.17+):**
```
ttm.pages_limit=VALUE
ttm.page_pool_size=VALUE
```

Set in `/etc/default/grub`:
```
GRUB_CMDLINE_LINUX_DEFAULT="quiet splash ttm.pages_limit=26624000 ttm.page_pool_size=26624000"
```

Then `sudo update-grub` and reboot.

**Value calculation:** `([size in GB] * 1024 * 1024) / 4.096`
- 104 GB → `26624000` (what we used — results in ~101.6 GB after driver rounding)
- 108 GB → `27648000` (Jeff Geerling's tested maximum on identical silicon before segfaults)

**Verify after reboot:**
```bash
awk '{printf "%.1f GB\n", $1/1024/1024/1024}' /sys/class/drm/card1/device/mem_info_gtt_total
cat /sys/module/ttm/parameters/pages_limit
```

**Safety ceiling:** Do not exceed 108 GB (~27648000). Jeff Geerling confirmed 110 GB causes segfaults on the same Strix Halo silicon (AI Max+ 395 / Radeon 890M, GFX1151).

**GTT is dynamic:** Allocations are not permanently reserved — the OS can reclaim GTT memory when the GPU isn't using it. Raising the ceiling does not reduce available system RAM at idle.

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
- **nginx** (port 80): reverse proxy in front of the orchestrator. Required `proxy_buffering off` to pass SSE tokens through without buffering.
- **orchestrator** (port 8000): manages conversation history, serves the UI, owns all routing logic and tool calls.
- **llm_service** (port 8002): thin Ollama wrapper. Translates the orchestrator's message format into Ollama's API format and streams tokens back as SSE. Keeping this separate means swapping out the LLM backend only touches this service.
- **tool_service** (port 8003): external API integrations — weather, web search, URL fetch, shopping list.
- **health_service** (port 8004): Garmin Connect sync service with SQLite persistence and health dashboard.
- **searxng** (port 8080): self-hosted meta-search engine. Used by tool_service for web search queries.
- **infisical** (port 8200): self-hosted secrets manager (postgres + redis backed). All service credentials are stored here.

### Services directory page

A `/services` page is served by the orchestrator, listing all services with status indicators (green/red dots) that ping each service's health endpoint every 30 seconds. Provides clickable links to all web UIs from one place. Built with the same dark theme as the main chat interface.

### Tool integration pattern
Tool calls use rule-based intent detection (regex keyword matching) in the orchestrator rather than LLM-driven function calling. This avoids model capability variance — not all models handle function calling consistently, and adding an LLM round-trip just to decide which tool to call adds latency with no benefit when the intent patterns are straightforward. The orchestrator detects intent, calls the tool, injects the result as a system message, then sends the full context to the LLM. The model's job is only to format the response.

The tradeoff: regex intent detection misses ambiguous or conversational phrasings that a function-calling model would catch. The plan is to migrate to a proper agentic loop (Option B) once the tool set is stable enough to justify it.

---

## Tools

### Weather — National Weather Service (api.weather.gov)

**Why NWS over Open-Meteo:** NWS is a US government service, free, no API key, and provides significantly richer data — named forecast periods with narrative descriptions ("patchy fog before 8am"), hourly breakdowns, and active weather alerts. Open-Meteo gives a snapshot; NWS gives a story.

**Two-step flow:**
1. Geocode via Open-Meteo (NWS has no geocoder) to get lat/lon
2. `GET /points/{lat},{lon}` → NWS grid assignment → parallel fetch of hourly forecast, named periods, and alerts via `asyncio.gather()`

**US-only limitation and fallback:** NWS only covers US locations. When it returns non-200, the pipeline doesn't fail silently — it falls back to a web search for "current weather [location]". The model is told explicitly that the data came from web search, not a live feed. If both fail, the model is told to say so rather than guess.

**Keep Open-Meteo for geocoding:** NWS provides no location lookup. Open-Meteo's geocoding API is free, returns results in a consistent format, and works globally — keeping it for this step is the right call.

### Web Search — SearXNG (self-hosted)

**Why self-hosted:** Privacy was the primary driver. A home assistant that sends every query to Google or Bing defeats the point of running locally. SearXNG is a meta-search engine — it queries multiple sources on your behalf and returns aggregated results. Queries never leave the house.

**Why SearXNG over a search API:** No API key, no rate limits, no cost. The tradeoff is result quality can vary vs. a dedicated paid API, but for a home assistant context it's more than adequate.

**Snippet-only approach:** Search results are injected as title + URL + snippet, not full page content. This keeps context size small. If the model or user needs the full article, a URL can be passed to the fetch tool for a deep dive.

### URL Fetch

Fetches a URL, strips boilerplate (nav, header, footer, scripts) with BeautifulSoup, collapses whitespace, and truncates to ~1,500 tokens (~6,000 chars). This keeps the context injection from blowing up the context window on long pages.

**`verify=False` on httpx:** SSL certificate verification fails inside the container even after installing `ca-certificates` and `certifi`. The failure is an intermediate CA gap in the container environment, not a problem with the target sites. Since this is a read-only fetch for a home assistant, `verify=False` is an acceptable tradeoff. Only the fetch endpoint uses it.

### Shopping List

JSON file persistence at `/data/shopping_list.json` via a Docker volume mount. No database — a JSON file is sufficient for a personal shopping list and survives container restarts. CRUD via natural language: add, remove, view, clear.

Includes a mobile-friendly web page at `/shopping_list` (served by the orchestrator) with a dark theme and 30-second auto-refresh, so a phone can be used as a read-only view at the store without needing to talk to Kronk.

### File Upload

PDF and plain text files can be attached and injected as system messages on every request in the session. Token count is estimated and displayed per file; a warning is shown when total attached context exceeds ~2,000 tokens.

---

## Pipeline Reliability

### Hallucination guardrails

Early testing showed the model would fabricate data when tools failed. Asking about Madrid weather returned "as of my latest training data, the weather in Madrid is..." — confidently wrong. Three layers of guardrails were added, and the order matters:

1. **System prompt standing rule** — "Never fabricate real-time information. If no tool data is present, say so." This is the backstop for cases the pipeline doesn't anticipate.

2. **Directive failure messages** — When a tool fails, the pipeline injects a system message with explicit `MUST NOT answer from training data` language, not a soft suggestion. Models are better at following explicit prohibitions than inferring them from absence.

3. **Structural tool status lines** — Every tool result (success or failure) is prefixed with `[TOOL: weather — live NWS data]` or `[TOOL: weather — FAILED]`. The model always sees explicit state rather than having to infer it from context. This is the most reliable layer because it's structural, not instructional.

A single layer isn't enough. The system prompt rule is too easy to rationalize around. The failure message alone can be overridden by the model's helpful instinct. The status lines close the gap by making the tool state unambiguous in the prompt.

### Pipeline stages and timing

The timing model was initially a single slot — one `fetch_tool` variable and one timestamp. This was wrong: when the weather tool failed and search ran as a fallback, the weather attempt was overwritten and disappeared from the timing display.

Replaced with a `stages` list. Each tool attempt appends `{tool, duration_s, ok}` to the list when it completes. The timing event sends the full list. Benefits:
- Every attempt is recorded, including partial failures
- Failed stages are visually distinct (shown in red) in the UI
- New tools automatically appear in timing without any extra wiring

---

## UI

### Streaming and markdown rendering

Tokens are streamed and appended to the bubble as plain text during generation. When `[DONE]` is received, the completed text is run through a markdown renderer that converts fenced code blocks, inline code, and `[text](url)` links to HTML. Links open in a new tab with `rel="noopener noreferrer"`.

**Why render on completion, not per-token:** Running the markdown parser on a partial stream causes flickering — a half-written `[link](` gets rendered incorrectly mid-stream, then corrected. For typical response lengths, the snap to rendered markdown at the end of generation is imperceptible.

**No external library:** The renderer is ~15 lines of regex + string manipulation. It covers the patterns Kronk actually produces (code blocks, inline code, links). A full markdown library would handle more edge cases but adds an external CDN dependency for minimal practical gain.

### Stage indicators

Each tool call emits a stage event (`fetching_weather`, `fetching_search`, `fetching_url`, `fetching`) before the async work begins. The UI shows a spinner with a label so the user knows what the pipeline is doing during the fetch phase. The stage is cleared when the first token arrives.

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

## Health Service

### Garmin Connect sync

`health_service` polls Garmin Connect every 24 hours (APScheduler BackgroundScheduler inside FastAPI) and stores the results in a local SQLite database at `/data/health.db`. Tables: `daily_summary`, `sleep`, `hrv`, `body_battery`, `activities`, `sync_log`.

The sync is split into per-day calls with `time.sleep(1)` between each one to avoid triggering Garmin's rate limiting.

### Dashboard

A Chart.js dashboard at `http://kronk.local:8004` displays:
- Metric strip: steps with goal bar, sleep duration, body battery, resting HR, HRV
- Body battery curve (line chart)
- Sleep stages breakdown (stacked CSS bar)
- HRV 30-day trend with baseline band
- 7-day steps bar chart
- Recent activities list

Auto-refreshes every 5 minutes.

### Garmin authentication

Garmin authentication is non-trivial. Key lessons:

**The garth-based auth flow is dead.** The `garminconnect` library underwent a major breaking change — the old garth/OAuth/cookie login no longer works. The library now authenticates using the same mobile SSO flow as the official Garmin Connect Android app, obtaining DI OAuth Bearer tokens. The token format changed from garth's session string to `garmin_tokens.json`. Upgrading from an old pinned version to `>=0.2.25` is required.

**`curl_cffi` is essential.** The library has a strategy chain for login: portal web flow (preferred) → mobile SSO. The portal web flow uses `curl_cffi` to impersonate a real Chrome TLS fingerprint. The library's own comment: *"This is the endpoint connect.garmin.com itself uses, so Cloudflare cannot block it without breaking their own website."* Without `curl_cffi`, it falls back to plain `requests` which Cloudflare fingerprints and 429s. Add `curl_cffi>=0.7.0` to requirements.

**Cloudflare 429 on initial auth.** The SSO login flow makes ~8-10 HTTP requests in rapid succession. On a fresh IP or after several failed attempts, Cloudflare rate-limits the IP. Retry with exponential backoff (30s → 60s → 120s → 240s) via `setup_auth.py`.

**Session persistence survives container rebuilds.** The token file lives at `/data/garmin_tokens.json`, a host volume mount. The `login(tokenstore=path)` API handles load-existing / full-auth / auto-save in one call. No restart needed after initial auth.

**MFA blocks unattended auth.** If the Garmin account has MFA enabled, initial authentication is interactive and cannot be done in a background job. Ongoing syncs work fine once the token is saved — the DI refresh token handles renewal without re-authentication.

**Auth is done in a separate one-shot container.** `docker-compose.setup-auth.yml` defines a `garmin_setup` service using the health_service image with a different entrypoint. Keeps the host clean.

```bash
docker compose -f docker-compose.setup-auth.yml run --rm garmin_setup
```

---

## Secrets and Dependency Management

### Secrets: Infisical

All application credentials are stored in a self-hosted Infisical instance (postgres + redis backend, port 8200). Secrets never leave the home network.

Each service authenticates to Infisical using a machine identity with read-only access scoped to only the secrets it needs. The machine identity client ID + client secret are stored as Docker secrets (files in `./secrets/`, mounted at `/run/secrets/` inside the container) — never in environment variables.

**Why Infisical over simpler approaches:** It scales cleanly to multiple services, provides audit logging, and centralizes secret rotation. Each service gets a scoped machine identity; adding a new service is just creating a new identity in the UI.

**Why the REST API over the Infisical SDK:** The official `infisical-sdk` uses pyo3 Rust bindings — harder to audit, platform-specific wheels complicate lockfile generation. The REST API is 4 lines with httpx (already a dependency), implemented directly in `infisical.py`.

**Flow:** client ID + client secret → POST `/api/v1/auth/universal-auth/login` → short-lived access token → GET `/api/v3/secrets/raw` → dict of key/value pairs. The access token is never persisted.

**`.gitignore`:** The `./secrets/` directory is gitignored. If credentials are accidentally committed, rotate them immediately.

### Dependencies: hash pinning

Every dependency is pinned to an exact SHA-256 content hash. If a package is tampered with or swapped, the hash won't match and the build fails.

- `requirements.txt` — direct dependencies, human-maintained
- `requirements.lock` — machine-generated, every transitive dependency with hashes
- Docker builds install with `uv pip install --require-hashes -r requirements.lock`

**Generating / updating lockfiles:**
```bash
# Run inside a container to match the exact linux/amd64 environment
docker run --rm -v ./tool_service:/svc python:3.12-slim \
  bash -c "pip install uv -q && uv pip compile /svc/requirements.txt --generate-hashes -o /svc/requirements.lock"
```

Repeat for each service directory. When updating a direct dependency, regenerate the lockfile and commit both files. Never edit the lockfile by hand.

---

## What's Still on the Table

- **Garmin MFA / initial auth** — MFA on the Garmin account blocks unattended first-time authentication. Need an interactive path (run setup_auth.py while present) to get the initial token saved, after which syncs are silent. The Cloudflare 429 rate limit on initial auth is a separate issue — requires waiting ~90 minutes between attempts.
- **Agentic loop (Option B)** — The current regex intent detection is a stepping stone. The intended architecture is an LLM-driven agentic loop where the model decides which tools to call and can chain them. The regex approach was built first to get something working; the tool set is now stable enough to justify the upgrade.
- **Health data in Kronk** — Once the Garmin sync is operational, expose health data as a tool so Kronk can answer questions like "how did I sleep last night?" and "what's my HRV trend this week?"
- **Query routing** — route simple/fast queries to a small model, complex ones to a larger one; architecture already supports this via the `model` override on `/message`
- **Quantization** — a Q2/Q3 version of llama3.3:70b would be ~25-30 GB and might fit in GPU at 100% while retaining more quality than 14B models
- **Voice pipeline** — STT (Whisper.cpp), TTS (Piper), wake word (openWakeWord); stubbed in the current UI
- **More tools** — Philips Hue, calendar, home automation
- **Additional health sources** — Fitbit (for a family member), Withings scale; Infisical machine identities are already the right pattern for adding new service credentials
- **External shopping list** — publish the shopping list page externally (GitHub Pages / Cloudflare Pages) so it's accessible without being on the home network
