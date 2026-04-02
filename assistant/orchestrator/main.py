import os
import re
import json
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

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


def detect_weather_intent(text: str) -> bool:
    return bool(WEATHER_KEYWORDS.search(text))


def extract_location(text: str) -> str:
    """Extract 'in [location]' from message, fall back to default."""
    match = LOCATION_PATTERN.search(text)
    if match:
        return match.group(1).strip().rstrip(',')
    return DEFAULT_LOCATION


# ── Routes ────────────────────────────────────────────────────────────────────

class MessageRequest(BaseModel):
    text: str
    model: str | None = None


@app.get("/", response_class=HTMLResponse)
async def root():
    with open("/app/static/index.html") as f:
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
        t_fetch_done = None
        t_first_token = None
        fetch_happened = False

        try:
            # ── Tool: weather ──────────────────────────────────────────────
            if detect_weather_intent(req.text):
                yield f"data: {json.dumps({'stage': 'fetching'})}\n\n"
                fetch_happened = True
                location = extract_location(req.text)
                try:
                    async with httpx.AsyncClient(timeout=10) as client:
                        resp = await client.get(
                            f"{TOOL_SERVICE_URL}/weather",
                            params={"location": location},
                        )
                        if resp.status_code == 200:
                            wx = resp.json()
                            tool_context = (
                                f"Current weather for the user's location ({wx['location']}): "
                                f"{wx['summary']}. Use this data to answer the user's weather question. "
                                f"If asked about tonight or future conditions, use the current conditions "
                                f"as the best available approximation."
                            )
                            messages.append({"role": "system", "content": tool_context})
                except Exception:
                    pass  # tool failure is non-fatal — LLM will respond without data
                t_fetch_done = time.monotonic()

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
                            if fetch_happened and t_fetch_done is not None:
                                timing["fetch_s"] = round(t_fetch_done - t_start, 2)
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
    return {"status": "cleared"}


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
