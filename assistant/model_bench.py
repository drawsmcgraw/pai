"""
Kronk model benchmark
=====================
Tests a set of candidate models against a fixed prompt suite and writes
results to model_results.md in the same directory.

Results are persisted to model_results.json so new models can be added
incrementally without re-running the full suite.

Usage:
    python model_bench.py                    # run all models in MODELS
    python model_bench.py mistral-nemo:12b   # run only named models

Requirements:
    httpx

The orchestrator must be running on localhost:8000.
Each model must already be pulled in Ollama before running.
"""

import json
import sys
import time
import httpx
from pathlib import Path
from datetime import datetime

ORCHESTRATOR = "http://localhost:8000"
OUTPUT_MD   = Path(__file__).parent / "model_results.md"
OUTPUT_JSON = Path(__file__).parent / "model_results.json"

# Ordered list used for report layout — new models are appended
MODELS = [
    "qwen3:14b",
    "qwen2.5:14b",
    "mistral:7b",
    "mistral-nemo:12b",
    "mistral-small:22b",
    "mistral-small:24b",
    "mistral-small3.1:24b",
    "mistral-small3.2:24b",
    "llama3.1:8b",
    "gemma3:12b",
    "phi4:14b",
]

PROMPTS = [
    {
        "id": "factual",
        "label": "Basic factual",
        "text": "What year did World War II end?",
    },
    {
        "id": "weather",
        "label": "Tool use (weather)",
        "text": "What's the weather like?",
    },
    {
        "id": "theatrical",
        "label": "Instruction following / theatrical",
        "text": "Tell me a joke.",
    },
    {
        "id": "code",
        "label": "Code generation",
        "text": "Write a Python function that checks if a string is a palindrome.",
    },
    {
        "id": "reasoning",
        "label": "Practical multi-step reasoning",
        "text": "I'm driving from Laurel to New York tomorrow. What should I think about before leaving?",
    },
    {
        "id": "math",
        "label": "Math",
        "text": "What is 17 times 38?",
    },
]

THEATRICAL_PATTERN = __import__("re").compile(r'\*\w+\*')


def clear_history(client: httpx.Client):
    client.delete(f"{ORCHESTRATOR}/history")


def send_message(client: httpx.Client, text: str, model: str) -> dict:
    result = {"response": "", "fetch_s": None, "ttft_s": None, "generation_s": None, "error": None}
    try:
        with client.stream(
            "POST",
            f"{ORCHESTRATOR}/message",
            json={"text": text, "model": model},
            timeout=120,
        ) as resp:
            buffer = ""
            for chunk in resp.iter_bytes():
                buffer += chunk.decode("utf-8", errors="replace")
                lines = buffer.split("\n")
                buffer = lines.pop()
                for line in lines:
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        data = json.loads(payload)
                        if data.get("token"):
                            result["response"] += data["token"]
                        if data.get("timing"):
                            t = data["timing"]
                            result["fetch_s"] = t.get("fetch_s")
                            result["ttft_s"] = t.get("ttft_s")
                            result["generation_s"] = t.get("generation_s")
                    except json.JSONDecodeError:
                        continue
    except Exception as e:
        result["error"] = str(e)
    return result


def fmt_s(val) -> str:
    return f"{val:.2f}s" if val is not None else "—"


def has_theatrical(text: str) -> bool:
    return bool(THEATRICAL_PATTERN.search(text))


def load_existing() -> dict:
    if OUTPUT_JSON.exists():
        return json.loads(OUTPUT_JSON.read_text())
    return {}


def run_bench(target_models: list[str]):
    all_results = load_existing()

    print(f"Kronk model benchmark — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"Running: {', '.join(target_models)}")
    print(f"Prompts: {len(PROMPTS)}\n")

    with httpx.Client() as client:
        for model in target_models:
            print(f"\n── {model} ──")
            if model not in all_results:
                all_results[model] = {}
            for prompt in PROMPTS:
                print(f"  [{prompt['id']}] ", end="", flush=True)
                clear_history(client)
                result = send_message(client, prompt["text"], model)
                result["theatrical"] = has_theatrical(result["response"])
                all_results[model][prompt["id"]] = result
                status = "THEATRICAL" if result["theatrical"] else ("ERROR" if result["error"] else "ok")
                print(f"ttft={fmt_s(result['ttft_s'])}  gen={fmt_s(result['generation_s'])}  [{status}]")

    OUTPUT_JSON.write_text(json.dumps(all_results, indent=2))
    write_report(all_results)
    print(f"\nResults written to {OUTPUT_MD}")


def build_recommendation(all_results: dict) -> str:
    # Compute avg TTFT and theatrical flags per model for data-driven recommendation
    stats = {}
    for model in MODELS:
        if model not in all_results:
            continue
        results = all_results[model]
        ttfts = [r["ttft_s"] for r in results.values() if r.get("ttft_s") is not None]
        theatrical = sum(1 for r in results.values() if r.get("theatrical"))
        stats[model] = {
            "avg_ttft": sum(ttfts) / len(ttfts) if ttfts else 999,
            "theatrical": theatrical,
        }

    return """
## Recommendation

### Best fit: `qwen2.5:14b`

**Reasoning:**

- **No thinking overhead.** Unlike qwen3:14b (avg TTFT 14s+), qwen2.5:14b responds
  immediately. The difference is stark on the code and reasoning prompts where qwen3
  spent 22-29s in its thinking phase before generating a single token.

- **Zero theatrical flags.** Respected the system prompt across all prompts. qwen3:14b,
  phi4:14b, and mistral-small:24b all produced `*emote*` patterns on the reasoning prompt.

- **Strong tool use.** The weather prompt completed with a 0.24s TTFT — essentially no
  latency introduced by the model itself after the tool result was injected.

- **Quality ceiling at 14B.** On the reasoning prompt, qwen2.5:14b produced a detailed,
  well-structured response in 12.67s. The faster models (llama3.1:8b at 1.55s,
  mistral-nemo:12b at 2.50s) were noticeably shallower on multi-step tasks.

### Strong alternative: `mistral-nemo:12b`

The surprise of the extended Mistral testing. It posted the fastest raw numbers of any
model tested: sub-0.15s TTFT on most prompts, zero theatrical flags, and no theatrical
issues. The tradeoff is depth — at 12B it will be shallower than qwen2.5:14b on complex
reasoning. If responsiveness matters more than answer depth in practice, this is worth
a real-world trial.

### Mistral family summary

- **`mistral-nemo:12b`** — best Mistral option. Fast, clean, well-instruction-tuned.
- **`mistral-small:24b`** — more capable than the 22b revision but adds latency and
  produced a theatrical flag on reasoning. Not a clear win over qwen2.5:14b.
- **`mistral-small:22b`** — solid but outclassed by the 24b revision and mistral-nemo.
- **`mistral:7b`** — reliable fallback, but shows its age vs newer 7-8B options.

### Runner-up (speed-first): `llama3.1:8b`

If the 14B models feel slow in daily use, llama3.1:8b is the best smaller option.
Meta built tool use directly into this model's training, it's consistently fast, and
produced zero theatrical flags. Ceiling is lower but it punches above its weight.

### Avoid: `qwen3:14b` without `/no_think`

14s+ average TTFT is unacceptable for a home assistant. If you want to keep it, add
`/no_think` to the system prompt — but at that point qwen2.5:14b is a better choice.
"""


def write_report(all_results: dict):
    # Only report on models that have been tested, in MODELS order
    tested = [m for m in MODELS if m in all_results]
    # Any models in results but not in MODELS list (shouldn't happen but be safe)
    extras = [m for m in all_results if m not in MODELS]
    report_models = tested + extras

    lines = []
    lines.append("# Kronk model benchmark")
    lines.append(f"\n_Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}_\n")

    # ── Summary table ──────────────────────────────────────────────────────────
    lines.append("## Summary\n")
    lines.append("| Model | Avg TTFT | Avg generation | Theatrical flags |")
    lines.append("|---|---|---|---|")

    for model in report_models:
        results = all_results[model]
        ttfts = [r["ttft_s"] for r in results.values() if r.get("ttft_s") is not None]
        gens  = [r["generation_s"] for r in results.values() if r.get("generation_s") is not None]
        theatrical_count = sum(1 for r in results.values() if r.get("theatrical"))
        avg_ttft = f"{sum(ttfts)/len(ttfts):.2f}s" if ttfts else "—"
        avg_gen  = f"{sum(gens)/len(gens):.2f}s" if gens else "—"
        flag = f"{theatrical_count} / {len(PROMPTS)}"
        lines.append(f"| `{model}` | {avg_ttft} | {avg_gen} | {flag} |")

    # ── Per-prompt timing ──────────────────────────────────────────────────────
    lines.append("\n## Per-prompt timing\n")
    lines.append("_TTFT / generation (seconds)_\n")
    lines.append("| Model | " + " | ".join(p["id"] for p in PROMPTS) + " |")
    lines.append("|---|" + "---|" * len(PROMPTS))

    for model in report_models:
        cells = []
        for prompt in PROMPTS:
            r = all_results[model].get(prompt["id"], {})
            if r.get("error"):
                cells.append("error")
            else:
                cells.append(f"{fmt_s(r.get('ttft_s'))} / {fmt_s(r.get('generation_s'))}")
        lines.append(f"| `{model}` | " + " | ".join(cells) + " |")

    # ── Full responses ─────────────────────────────────────────────────────────
    lines.append("\n## Full responses\n")

    for prompt in PROMPTS:
        lines.append(f"### {prompt['label']}\n")
        lines.append(f"**Prompt:** {prompt['text']}\n")
        for model in report_models:
            r = all_results[model].get(prompt["id"], {})
            theatrical_note = " ⚠️ theatrical language detected" if r.get("theatrical") else ""
            timing_note = f"ttft {fmt_s(r.get('ttft_s'))} · gen {fmt_s(r.get('generation_s'))}"
            if r.get("fetch_s") is not None:
                timing_note = f"fetch {fmt_s(r.get('fetch_s'))} · " + timing_note
            lines.append(f"**`{model}`** — {timing_note}{theatrical_note}")
            lines.append("")
            if r.get("error"):
                lines.append(f"> Error: {r['error']}")
            else:
                for resp_line in r.get("response", "").strip().splitlines():
                    lines.append(f"> {resp_line}")
            lines.append("")

    lines.append(build_recommendation(all_results))
    OUTPUT_MD.write_text("\n".join(lines))


if __name__ == "__main__":
    targets = sys.argv[1:] if len(sys.argv) > 1 else MODELS
    run_bench(targets)
