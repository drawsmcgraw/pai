import io
import os
import re
import json
import httpx
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pypdf import PdfReader

app = FastAPI(title="Kronk Orchestrator")

LLM_SERVICE_URL = os.getenv("LLM_SERVICE_URL", "http://localhost:8002")
TOOL_SERVICE_URL = os.getenv("TOOL_SERVICE_URL", "http://localhost:8003")
MODEL_NAME = os.getenv("MODEL_NAME", "qwen3:14b")
DEFAULT_LOCATION = os.getenv("LOCATION", "Laurel, MD")
SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "You are Kronk, a helpful home assistant. Be direct and concise. "
    "Do not use action text, emotes, or filler expressions like *winks*, *laughs*, or *grins*. "
    "Do not use theatrical language. Answer questions factually and helpfully.",
)

# In-memory conversation history (wiped on restart)
history: list[dict] = []

# Uploaded file contexts — injected as system messages on every request
file_contexts: list[dict] = []  # [{"name": str, "content": str, "tokens": int}]

# Rough token estimate: 1 token ≈ 4 characters
TOKEN_WARNING_THRESHOLD = 2000


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)

# ── Intent detection ──────────────────────────────────────────────────────────

WEATHER_KEYWORDS = re.compile(
    r'\b(weather|temperature|temp|forecast|raining|snowing|sunny|cloudy|'
    r'cold|hot|humid|humidity|wind|windy|outside|degrees|warm|chilly|freezing)\b',
    re.IGNORECASE,
)

LOCATION_PATTERN = re.compile(
    r'\bin\s+([A-Za-z][A-Za-z\s,\.]+?)(?:\?|\.|!|$)',
    re.IGNORECASE,
)

SEARCH_PATTERN = re.compile(
    r'\b(?:search for|look up|lookup|find|search)\s+(.+?)(?:\?|$)',
    re.IGNORECASE,
)

URL_PATTERN = re.compile(
    r'https?://[^\s<>"\']+',
    re.IGNORECASE,
)

LIST_ADD_PATTERN = re.compile(
    r'\badd\s+(.+?)\s+to\s+(?:the\s+)?(?:shopping\s+)?list\b',
    re.IGNORECASE,
)

LIST_REMOVE_PATTERN = re.compile(
    r'\bremove\s+(.+?)\s+from\s+(?:the\s+)?(?:shopping\s+)?list\b',
    re.IGNORECASE,
)

LIST_VIEW_PATTERN = re.compile(
    r'\b(?:what(?:\'s|\s+is)\s+on|show|read|check|what\s+do\s+(?:i|we)\s+need)\b.{0,30}(?:shopping\s+)?list\b',
    re.IGNORECASE,
)

LIST_CLEAR_PATTERN = re.compile(
    r'\bclear\s+(?:the\s+)?(?:shopping\s+)?list\b',
    re.IGNORECASE,
)


def detect_weather_intent(text: str) -> bool:
    return bool(WEATHER_KEYWORDS.search(text))


def extract_location(text: str) -> str:
    """Extract 'in [location]' from message, fall back to default."""
    match = LOCATION_PATTERN.search(text)
    if match:
        return match.group(1).strip().rstrip(',')
    return DEFAULT_LOCATION


def detect_search_intent(text: str) -> str | None:
    """Return search query if explicit search trigger found, else None."""
    match = SEARCH_PATTERN.search(text)
    if match:
        return match.group(1).strip().rstrip('?. ')
    return None


def extract_url(text: str) -> str | None:
    """Return first URL found in message, else None."""
    match = URL_PATTERN.search(text)
    return match.group(0) if match else None


def parse_list_items(text: str) -> list[str]:
    """Split 'milk, eggs, and bread' into ['milk', 'eggs', 'bread']."""
    text = re.sub(r'\band\b', ',', text, flags=re.IGNORECASE)
    return [i.strip().strip('.') for i in text.split(',') if i.strip()]


def detect_list_add(text: str) -> list[str] | None:
    match = LIST_ADD_PATTERN.search(text)
    if match:
        return parse_list_items(match.group(1))
    return None


def detect_list_remove(text: str) -> str | None:
    match = LIST_REMOVE_PATTERN.search(text)
    return match.group(1).strip() if match else None


def detect_list_view(text: str) -> bool:
    return bool(LIST_VIEW_PATTERN.search(text))


def detect_list_clear(text: str) -> bool:
    return bool(LIST_CLEAR_PATTERN.search(text))


# ── Routes ────────────────────────────────────────────────────────────────────

class MessageRequest(BaseModel):
    text: str
    model: str | None = None


@app.get("/", response_class=HTMLResponse)
async def root():
    with open("/app/static/index.html") as f:
        return f.read()


@app.get("/services", response_class=HTMLResponse)
async def services():
    with open("/app/static/services.html") as f:
        return f.read()


@app.post("/message")
async def message(req: MessageRequest):
    history.append({"role": "user", "content": req.text})
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    model = req.model or MODEL_NAME

    async def stream():
        import time
        assistant_reply = []
        first_token = True
        t_start = time.monotonic()
        # Inject uploaded file contents as system messages
        for fc in file_contexts:
            messages.append({
                "role": "system",
                "content": f"[Attached file: {fc['name']}]\n{fc['content']}",
            })
        stages = []
        t_first_token = None

        try:
            # ── Tool: weather ──────────────────────────────────────────────
            if detect_weather_intent(req.text):
                yield f"data: {json.dumps({'stage': 'fetching_weather'})}\n\n"
                location = extract_location(req.text)
                weather_ok = False
                t0 = time.monotonic()
                try:
                    async with httpx.AsyncClient(timeout=10) as client:
                        resp = await client.get(
                            f"{TOOL_SERVICE_URL}/weather",
                            params={"location": location},
                        )
                        if resp.status_code == 200:
                            wx = resp.json()
                            tool_context = (
                                f"[TOOL: weather — live NWS data]\n\n"
                                f"Weather data for {wx['location']}:\n\n"
                                f"{wx['summary']}\n\n"
                                f"Use this data to answer the user's weather question. "
                                f"Be concise — pick the relevant time period and summarize it."
                            )
                            messages.append({"role": "system", "content": tool_context})
                            weather_ok = True
                except Exception:
                    pass
                stages.append({"tool": "weather", "s": round(time.monotonic() - t0, 2), "ok": weather_ok})

                if not weather_ok:
                    # NWS doesn't cover this location — fall back to web search
                    yield f"data: {json.dumps({'stage': 'fetching_search'})}\n\n"
                    search_query = f"current weather {location}"
                    search_ok = False
                    t0 = time.monotonic()
                    try:
                        async with httpx.AsyncClient(timeout=15) as client:
                            resp = await client.get(
                                f"{TOOL_SERVICE_URL}/search",
                                params={"q": search_query, "count": 5},
                            )
                            if resp.status_code == 200:
                                sr = resp.json()
                                snippets = "\n\n".join(
                                    f"[{r['title']}]({r['url']})\n{r['snippet']}"
                                    for r in sr["results"]
                                )
                                tool_context = (
                                    f"[TOOL: weather — FAILED, no NWS coverage for this location]\n"
                                    f"[TOOL: search — web results only, not a live weather feed]\n\n"
                                    f"Web search results for \"{search_query}\":\n\n{snippets}\n\n"
                                    f"Use these results to answer the user's weather question. "
                                    f"You MUST note that this is from web search, not a dedicated weather service."
                                )
                                messages.append({"role": "system", "content": tool_context})
                                search_ok = True
                    except Exception:
                        pass
                    stages.append({"tool": "search", "s": round(time.monotonic() - t0, 2), "ok": search_ok})

                    if not search_ok:
                        messages.append({
                            "role": "system",
                            "content": (
                                f"[TOOL: weather — FAILED]\n"
                                f"[TOOL: search — FAILED]\n\n"
                                f"You MUST NOT answer the weather question from training data or make any estimates. "
                                f"Tell the user that live weather data for {location} is unavailable right now "
                                f"and suggest they check a weather site directly."
                            ),
                        })

            # ── Tool: web search ───────────────────────────────────────────
            elif search_query := detect_search_intent(req.text):
                yield f"data: {json.dumps({'stage': 'fetching_search'})}\n\n"
                search_ok = False
                t0 = time.monotonic()
                try:
                    async with httpx.AsyncClient(timeout=15) as client:
                        resp = await client.get(
                            f"{TOOL_SERVICE_URL}/search",
                            params={"q": search_query, "count": 5},
                        )
                        if resp.status_code == 200:
                            sr = resp.json()
                            snippets = "\n\n".join(
                                f"[{r['title']}]({r['url']})\n{r['snippet']}"
                                for r in sr["results"]
                            )
                            tool_context = (
                                f"[TOOL: search — live web results]\n\n"
                                f"Web search results for \"{search_query}\":\n\n{snippets}\n\n"
                                f"Use these results to answer the user's question. "
                                f"Cite sources by title where relevant."
                            )
                            messages.append({"role": "system", "content": tool_context})
                            search_ok = True
                except Exception:
                    pass
                stages.append({"tool": "search", "s": round(time.monotonic() - t0, 2), "ok": search_ok})
                if not search_ok:
                    messages.append({
                        "role": "system",
                        "content": (
                            f"[TOOL: search — FAILED]\n\n"
                            f"You MUST NOT answer the question from training data. "
                            f"Tell the user the web search failed and you cannot provide current information."
                        ),
                    })

            # ── Tool: URL fetch ────────────────────────────────────────────
            elif url := extract_url(req.text):
                yield f"data: {json.dumps({'stage': 'fetching_url'})}\n\n"
                fetch_ok = False
                t0 = time.monotonic()
                try:
                    async with httpx.AsyncClient(timeout=15) as client:
                        resp = await client.get(
                            f"{TOOL_SERVICE_URL}/fetch",
                            params={"url": url},
                        )
                        if resp.status_code == 200:
                            page = resp.json()
                            tool_context = (
                                f"[TOOL: fetch — live page content from {url}]\n\n"
                                f"{page['text']}\n\n"
                                f"Use this content to answer the user's question."
                            )
                            messages.append({"role": "system", "content": tool_context})
                            fetch_ok = True
                except Exception:
                    pass
                stages.append({"tool": "url", "s": round(time.monotonic() - t0, 2), "ok": fetch_ok})
                if not fetch_ok:
                    messages.append({
                        "role": "system",
                        "content": (
                            f"[TOOL: fetch — FAILED for {url}]\n\n"
                            f"You MUST NOT speculate about the page content. "
                            f"Tell the user the page could not be retrieved."
                        ),
                    })

            # ── Tool: shopping list ────────────────────────────────────────
            elif items := detect_list_add(req.text):
                yield f"data: {json.dumps({'stage': 'fetching'})}\n\n"
                list_ok = False
                t0 = time.monotonic()
                try:
                    async with httpx.AsyncClient(timeout=5) as client:
                        resp = await client.post(
                            f"{TOOL_SERVICE_URL}/shopping_list",
                            json={"items": items},
                        )
                        if resp.status_code == 200:
                            result = resp.json()
                            added = result.get("added", items)
                            tool_context = (
                                f"Added to shopping list: {', '.join(added)}. "
                                f"Confirm briefly — do not repeat the full list."
                            )
                            messages.append({"role": "system", "content": tool_context})
                            list_ok = True
                except Exception:
                    pass
                stages.append({"tool": "list", "s": round(time.monotonic() - t0, 2), "ok": list_ok})

            elif item := detect_list_remove(req.text):
                yield f"data: {json.dumps({'stage': 'fetching'})}\n\n"
                list_ok = False
                t0 = time.monotonic()
                try:
                    async with httpx.AsyncClient(timeout=5) as client:
                        resp = await client.delete(
                            f"{TOOL_SERVICE_URL}/shopping_list/{item}",
                        )
                        if resp.status_code == 200:
                            tool_context = (
                                f"Removed '{item}' from the shopping list. "
                                f"Confirm briefly — do not repeat the full list."
                            )
                            messages.append({"role": "system", "content": tool_context})
                            list_ok = True
                        elif resp.status_code == 404:
                            messages.append({"role": "system", "content": f"'{item}' was not found on the shopping list."})
                            list_ok = True  # 404 is a valid response, not a service failure
                except Exception:
                    pass
                stages.append({"tool": "list", "s": round(time.monotonic() - t0, 2), "ok": list_ok})

            elif detect_list_clear(req.text):
                yield f"data: {json.dumps({'stage': 'fetching'})}\n\n"
                list_ok = False
                t0 = time.monotonic()
                try:
                    async with httpx.AsyncClient(timeout=5) as client:
                        await client.delete(f"{TOOL_SERVICE_URL}/shopping_list/clear")
                        messages.append({"role": "system", "content": "Shopping list cleared. Confirm briefly."})
                        list_ok = True
                except Exception:
                    pass
                stages.append({"tool": "list", "s": round(time.monotonic() - t0, 2), "ok": list_ok})

            elif detect_list_view(req.text):
                yield f"data: {json.dumps({'stage': 'fetching'})}\n\n"
                list_ok = False
                t0 = time.monotonic()
                try:
                    async with httpx.AsyncClient(timeout=5) as client:
                        resp = await client.get(f"{TOOL_SERVICE_URL}/shopping_list")
                        if resp.status_code == 200:
                            result = resp.json()
                            all_items = result.get("items", [])
                            if all_items:
                                tool_context = f"Current shopping list ({len(all_items)} items): {', '.join(all_items)}."
                            else:
                                tool_context = "The shopping list is empty."
                            messages.append({"role": "system", "content": tool_context})
                            list_ok = True
                except Exception:
                    pass
                stages.append({"tool": "list", "s": round(time.monotonic() - t0, 2), "ok": list_ok})

            # ── LLM ────────────────────────────────────────────────────────
            messages.extend(history)
            t_llm_start = time.monotonic()
            yield f"data: {json.dumps({'stage': 'waiting'})}\n\n"

            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream(
                    "POST",
                    f"{LLM_SERVICE_URL}/chat",
                    json={"messages": messages, "model": model},
                ) as resp:
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        payload = line[len("data:"):].strip()
                        if payload == "[DONE]":
                            t_done = time.monotonic()
                            timing = {}
                            if stages:
                                timing["stages"] = stages
                            if t_first_token is not None:
                                timing["ttft_s"] = round(t_first_token - t_llm_start, 2)
                                timing["generation_s"] = round(t_done - t_first_token, 2)
                            yield f"data: {json.dumps({'timing': timing})}\n\n"
                            yield "data: [DONE]\n\n"
                            break
                        try:
                            data = json.loads(payload)
                            token = data.get("token", "")
                            if token:
                                if first_token:
                                    t_first_token = time.monotonic()
                                    yield f"data: {json.dumps({'stage': 'generating'})}\n\n"
                                    first_token = False
                                assistant_reply.append(token)
                                yield f"data: {json.dumps({'token': token})}\n\n"
                        except json.JSONDecodeError:
                            continue

        finally:
            if assistant_reply:
                history.append({"role": "assistant", "content": "".join(assistant_reply)})

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/history")
async def get_history():
    return {"history": history}


@app.delete("/history")
async def clear_history():
    history.clear()
    file_contexts.clear()
    return {"status": "cleared"}


@app.post("/files")
async def upload_file(file: UploadFile = File(...)):
    data = await file.read()
    name = file.filename or "upload"

    if name.lower().endswith(".pdf"):
        try:
            reader = PdfReader(io.BytesIO(data))
            content = "\n\n".join(
                page.extract_text() or "" for page in reader.pages
            ).strip()
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"Could not parse PDF: {e}")
    else:
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(status_code=422, detail="File must be UTF-8 text or a PDF")

    if not content:
        raise HTTPException(status_code=422, detail="No text could be extracted from the file")

    tokens = estimate_tokens(content)
    # Replace if same filename already uploaded
    for i, fc in enumerate(file_contexts):
        if fc["name"] == name:
            file_contexts[i] = {"name": name, "content": content, "tokens": tokens}
            break
    else:
        file_contexts.append({"name": name, "content": content, "tokens": tokens})

    total_tokens = sum(fc["tokens"] for fc in file_contexts)
    return {
        "name": name,
        "tokens": tokens,
        "total_tokens": total_tokens,
        "warning": total_tokens > TOKEN_WARNING_THRESHOLD,
    }


@app.get("/files")
async def list_files():
    total_tokens = sum(fc["tokens"] for fc in file_contexts)
    return {
        "files": [{"name": fc["name"], "tokens": fc["tokens"]} for fc in file_contexts],
        "total_tokens": total_tokens,
        "warning": total_tokens > TOKEN_WARNING_THRESHOLD,
    }


@app.delete("/files/{filename}")
async def delete_file(filename: str):
    for i, fc in enumerate(file_contexts):
        if fc["name"] == filename:
            file_contexts.pop(i)
            return {"status": "removed"}
    raise HTTPException(status_code=404, detail="File not found")


@app.get("/shopping_list", response_class=HTMLResponse)
async def shopping_list_page():
    import datetime
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{TOOL_SERVICE_URL}/shopping_list")
            data = resp.json() if resp.status_code == 200 else {"items": [], "updated_at": None}
    except Exception:
        data = {"items": [], "updated_at": None}

    items = data.get("items", [])
    updated_at = data.get("updated_at")
    if updated_at:
        dt = datetime.datetime.fromtimestamp(updated_at)
        updated_str = dt.strftime("%-I:%M %p, %b %-d")
    else:
        updated_str = "never"

    rows = "".join(f'<li class="item">{item}</li>' for item in items) if items else '<li class="empty">Nothing on the list.</li>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="30">
<title>Shopping List</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  :root {{
    --bg: #0f1117;
    --bg2: #171b24;
    --border: #2a3044;
    --text: #e8eaf0;
    --text2: #9ba3bc;
    --text3: #5c6480;
    --green: #1fa876;
  }}
  body {{
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, 'Segoe UI', system-ui, sans-serif;
    max-width: 480px;
    margin: 0 auto;
    padding: 1.5rem 1.25rem;
  }}
  header {{
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 1.5rem;
    padding-bottom: 1rem;
    border-bottom: 1px solid var(--border);
  }}
  .dot {{
    width: 8px; height: 8px;
    background: var(--green);
    border-radius: 50%;
    flex-shrink: 0;
  }}
  h1 {{ font-size: 18px; font-weight: 600; }}
  .updated {{
    font-size: 12px;
    color: var(--text3);
    margin-left: auto;
  }}
  ul {{ list-style: none; display: flex; flex-direction: column; gap: 2px; }}
  .item {{
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 0.75rem 1rem;
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 8px;
    font-size: 16px;
    line-height: 1.4;
  }}
  .item::before {{
    content: '';
    width: 18px; height: 18px;
    border: 1.5px solid var(--border);
    border-radius: 4px;
    flex-shrink: 0;
  }}
  .empty {{
    padding: 1rem;
    color: var(--text3);
    font-size: 15px;
    text-align: center;
  }}
  footer {{
    margin-top: 1.5rem;
    font-size: 11px;
    color: var(--text3);
    text-align: center;
  }}
</style>
</head>
<body>
<header>
  <div class="dot"></div>
  <h1>Shopping List</h1>
  <span class="updated">updated {updated_str}</span>
</header>
<ul>{rows}</ul>
<footer>auto-refreshes every 30s &nbsp;·&nbsp; add items by talking to Kronk</footer>
</body>
</html>"""


@app.get("/health")
async def health():
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            llm = await client.get(f"{LLM_SERVICE_URL}/health")
            tool = await client.get(f"{TOOL_SERVICE_URL}/health")
            return {"status": "ok", "llm_service": llm.json(), "tool_service": tool.json()}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


# Voice stubs
@app.post("/stt/transcribe")
async def stt_transcribe():
    return {"status": "not_implemented"}


@app.post("/tts/speak")
async def tts_speak():
    return {"status": "not_implemented"}


app.mount("/static", StaticFiles(directory="/app/static"), name="static")
