"""EmotionAI backend: a small, safe proxy between the website and an LLM provider.

Default provider: Groq (OpenAI-compatible API). Anthropic is also supported (LLM_PROVIDER=anthropic).

Why it exists: the browser must never hold your API key. The website calls THIS server,
and only this server knows the key (an environment variable in .env).

Endpoints
  GET  /api/health   -> {"status": "ok", "configured": true|false, "provider": "groq"}
  POST /api/chat     -> {"text": "..."}                      (stream=false)
                        text/event-stream of normalised events (stream=true)
"""
import json
import logging
import os
import re
import time
from collections import defaultdict, deque
from typing import AsyncIterator, Dict, List, Literal, Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

load_dotenv()
log = logging.getLogger("uvicorn.error")

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULTS = {
    "groq": {"model": "llama-3.3-70b-versatile", "quick": "llama-3.1-8b-instant"},
    "anthropic": {"model": "claude-sonnet-5-5", "quick": "claude-haiku-5-5"},
}


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def settings() -> Dict[str, object]:
    """Read settings on each call so tests (and redeploys) can change them."""
    provider = os.environ.get("LLM_PROVIDER", "groq").strip().lower()
    if provider not in DEFAULTS:
        provider = "groq"
    key_name = "GROQ_API_KEY" if provider == "groq" else "ANTHROPIC_API_KEY"
    d = DEFAULTS[provider]
    return {
        "provider": provider,
        "key": os.environ.get(key_name, "").strip(),
        "model": os.environ.get("CHAT_MODEL", d["model"]),
        "quick_model": os.environ.get("QUICK_MODEL", d["quick"]),
        "groq_base": os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/"),
        "origins": [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",") if o.strip()],
        "per_minute": _int("RATE_PER_MINUTE", 20),
        "per_day": _int("DAILY_CAP", 3000),
        "max_tokens": _int("MAX_TOKENS", 1024),
        "trust_proxy": os.environ.get("TRUST_PROXY", "0") == "1",
        "debug": os.environ.get("DEBUG_ERRORS", "0") == "1",
    }


app = FastAPI(title="EmotionAI backend", docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings()["origins"],  # set ALLOWED_ORIGINS to your site in production
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["content-type"],
)


class ChatRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=12000)
    tier: Literal["quick", "default"] = "default"
    stream: bool = False
    images: List[str] = Field(default_factory=list, max_length=3)  # base64 JPEG, optional

    @field_validator("images")
    @classmethod
    def _small_images(cls, v: List[str]) -> List[str]:
        for img in v:
            if len(img) > 900_000:
                raise ValueError("image too large")
        return v


# ---- tiny in-memory rate limiter (use Redis if you run several instances) ----
_hits: Dict[str, deque] = defaultdict(deque)
_day = {"stamp": time.strftime("%Y-%m-%d"), "count": 0}


def _client_ip(request: Request) -> str:
    if settings()["trust_proxy"]:
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def check_rate(request: Request) -> None:
    s = settings()
    now = time.time()
    q = _hits[_client_ip(request)]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= s["per_minute"]:
        raise HTTPException(429, "Too many requests. Please wait a minute and try again.")
    stamp = time.strftime("%Y-%m-%d")
    if _day["stamp"] != stamp:
        _day["stamp"], _day["count"] = stamp, 0
    if _day["count"] >= s["per_day"]:
        raise HTTPException(429, "The daily limit for this service has been reached. Please try again tomorrow.")
    q.append(now)
    _day["count"] += 1


# ---- automatic model fallback (Groq renames/retires models from time to time) ----
_resolved: Dict[str, str] = {}
_EXCLUDE = re.compile(r"whisper|tts|guard|orpheus|playai|embed|safeguard|moderation", re.I)
_PREFERRED = [
    "llama-3.3-70b-versatile", "llama-3.1-8b-instant",
    "meta-llama/llama-4-scout-17b-16e-instruct", "meta-llama/llama-4-maverick-17b-128e-instruct",
    "moonshotai/kimi-k2-instruct", "openai/gpt-oss-120b", "openai/gpt-oss-20b",
]


def pick_models(ids: List[str]):
    """Choose a chat model and a quick model from the models your key can use."""
    usable = [i for i in ids if not _EXCLUDE.search(i)]
    chat = next((m for m in _PREFERRED if m in usable), usable[0] if usable else None)
    quick = "llama-3.1-8b-instant" if "llama-3.1-8b-instant" in usable else chat
    return chat, quick


def _looks_like_model_error(up: str) -> bool:
    low = (up or "").lower()
    return "model" in low and any(w in low for w in ("not found", "does not exist", "decommission", "deprecat", "no access", "not have access", "terms"))


async def list_models(key: str) -> List[str]:
    s = settings()
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(s["groq_base"] + "/models", headers={"authorization": "Bearer " + key, "user-agent": "EmotionAI/1.0"})
    if r.status_code != 200:
        log.warning("Could not list models: HTTP %s: %s", r.status_code, _upstream_text(r.content))
        return []
    return [m.get("id", "") for m in (r.json().get("data") or []) if m.get("id")]


async def resolve_models(key: str) -> bool:
    chat, quick = pick_models(await list_models(key))
    if not chat:
        return False
    _resolved.update({"model": chat, "quick": quick or chat})
    log.warning("Configured model was not usable. Switched automatically to chat=%s quick=%s", chat, quick)
    return True


def _swap_model(payload: dict) -> dict:
    s = settings()
    tier = "quick" if payload.get("model") == s["quick_model"] else "model"
    return {**payload, "model": _resolved[tier]}


# ---- request building for each provider ----
def _payload(req: ChatRequest) -> dict:
    s = settings()
    model = s["quick_model"] if req.tier == "quick" else s["model"]
    if s["provider"] == "groq" and _resolved:
        model = _resolved["quick" if req.tier == "quick" else "model"]
    if s["provider"] == "groq":
        return {
            "model": model,
            "messages": [{"role": "user", "content": req.prompt}],
            "max_tokens": s["max_tokens"],
            "temperature": 0.7,
            "stream": req.stream,
        }
    content: list = []
    for img in req.images:
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img}})
    content.append({"type": "text", "text": req.prompt})
    return {"model": model, "max_tokens": s["max_tokens"], "messages": [{"role": "user", "content": content}], "stream": req.stream}


def _endpoint_and_headers(key: str):
    s = settings()
    if s["provider"] == "groq":
        return s["groq_base"] + "/chat/completions", {"authorization": "Bearer " + key, "content-type": "application/json", "user-agent": "EmotionAI/1.0"}
    return ANTHROPIC_URL, {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}


def _upstream_text(body: bytes) -> str:
    """Pull the provider's own error message out of an error response (never contains your key)."""
    try:
        obj = json.loads(body.decode("utf-8", "replace"))
        err = obj.get("error")
        return (err.get("message") if isinstance(err, dict) else str(err or ""))[:300]
    except Exception:
        return body.decode("utf-8", "replace")[:300]


def _error_for(status: int, upstream: str = "") -> HTTPException:
    if status == 429:
        msg = "The AI service is busy right now. Please try again in a moment."
    elif status in (401, 403):
        msg = "The server's AI key was rejected. Check the key in the server settings."
    elif status in (400, 404):
        msg = "The AI service refused the request. The model name in the server settings may be wrong or retired."
    else:
        msg = "The AI service could not answer right now."
    if upstream and settings()["debug"]:
        msg += f" (Provider says: {upstream})"
    return HTTPException(502 if status not in (429,) else 429, msg)


def _sse_error(message: str) -> bytes:
    return ("data: " + json.dumps({"type": "error", "error": {"message": message}}) + "\n\n").encode()


def groq_line_to_event(line: str) -> Optional[bytes]:
    """Turn one Groq (OpenAI-style) SSE line into the normalised event the website understands."""
    line = line.strip()
    if not line.startswith("data:"):
        return None
    data = line[5:].strip()
    if not data or data == "[DONE]":
        return None
    try:
        obj = json.loads(data)
    except ValueError:
        return None
    if obj.get("error"):
        return b'data: {"type":"error","error":{"message":"The AI service could not answer right now."}}\n\n'
    choices = obj.get("choices") or [{}]
    text = (choices[0].get("delta") or {}).get("content")
    if not text:
        return None
    return ("data: " + json.dumps({"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}) + "\n\n").encode()


# ---- upstream calls (separate functions so tests can replace them) ----
async def upstream_json(payload: dict, key: str, _retried: bool = False) -> str:
    url, headers = _endpoint_and_headers(key)
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as e:
        log.warning("Could not reach the AI provider: %s", type(e).__name__)
        raise HTTPException(502, "Could not reach the AI service. Check the server's internet connection.")
    if r.status_code != 200:
        up = _upstream_text(r.content)
        log.warning("AI provider returned HTTP %s: %s", r.status_code, up)
        if (not _retried and settings()["provider"] == "groq" and r.status_code in (400, 404)
                and _looks_like_model_error(up) and await resolve_models(key)):
            return await upstream_json(_swap_model(payload), key, True)
        raise _error_for(r.status_code, up)
    data = r.json()
    if settings()["provider"] == "groq":
        return ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


async def upstream_stream(payload: dict, key: str, _retried: bool = False) -> AsyncIterator[bytes]:
    url, headers = _endpoint_and_headers(key)
    groq = settings()["provider"] == "groq"
    try:
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as r:
                if r.status_code != 200:
                    up = _upstream_text(await r.aread())
                    log.warning("AI provider returned HTTP %s: %s", r.status_code, up)
                    if (not _retried and groq and r.status_code in (400, 404)
                            and _looks_like_model_error(up) and await resolve_models(key)):
                        async for ev in upstream_stream(_swap_model(payload), key, True):
                            yield ev
                        return
                    yield _sse_error(_error_for(r.status_code, up).detail)
                    return
                if groq:
                    async for line in r.aiter_lines():
                        ev = groq_line_to_event(line)
                        if ev:
                            yield ev
                else:
                    async for chunk in r.aiter_raw():
                        yield chunk
    except httpx.HTTPError as e:
        log.warning("Could not reach the AI provider: %s", type(e).__name__)
        yield _sse_error("Could not reach the AI service. Check the server's internet connection.")


@app.get("/api/health")
async def health() -> dict:
    s = settings()
    return {"status": "ok", "configured": bool(s["key"]), "provider": s["provider"]}


@app.get("/api/diagnose")
async def diagnose(request: Request) -> dict:
    """Sends one tiny test message so you can see exactly why the AI is not answering."""
    check_rate(request)
    s = settings()
    if not s["key"]:
        return {"ok": False, "stage": "config", "message": "No API key found. Check backend/.env, save it, and restart the server."}
    req = ChatRequest(prompt="Reply with the single word OK.", tier="quick")
    try:
        text = await upstream_json(_payload(req), s["key"])
        return {"ok": True, "provider": s["provider"], "model": _resolved.get("quick") or s["quick_model"],
                "auto_selected": dict(_resolved) or None, "reply": text.strip()[:40]}
    except HTTPException as e:
        return {"ok": False, "stage": "provider", "status": e.status_code, "message": e.detail,
                "tip": "Set DEBUG_ERRORS=1 in .env and restart to see the provider's own message."}


@app.get("/api/models")
async def models(request: Request) -> dict:
    """Lists the models your Groq key can use. Only available while DEBUG_ERRORS=1."""
    check_rate(request)
    s = settings()
    if not s["debug"]:
        raise HTTPException(404, "Not found")
    if not s["key"] or s["provider"] != "groq":
        return {"ok": False, "message": "Needs a Groq key in backend/.env."}
    ids = await list_models(s["key"])
    chat, quick = pick_models(ids)
    return {"ok": bool(ids), "count": len(ids), "models": ids, "would_pick": {"chat": chat, "quick": quick}}


@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request):
    check_rate(request)
    s = settings()
    if not s["key"]:
        raise HTTPException(503, "The server has no API key configured.")
    if req.images and s["provider"] == "groq":
        raise HTTPException(400, "Image analysis is not enabled for this provider. Expression analysis runs on your device instead.")
    payload = _payload(req)
    if req.stream:
        return StreamingResponse(upstream_stream(payload, s["key"]), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store"})
    return {"text": await upstream_json(payload, s["key"])}