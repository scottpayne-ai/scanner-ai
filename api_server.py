#!/usr/bin/env python3
"""
Scanner AI — FastAPI Backend
Polls Broadcastify Calls API, transcribes via OpenAI, detects keywords.
Streams live events to the frontend via SSE.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import AsyncGenerator

import aiohttp
import requests
import yaml
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from sse_starlette.sse import EventSourceResponse

# ── Config ──────────────────────────────────────────────────────────────────
_here = Path(__file__).parent
CONFIG_PATH = _here / "config.yaml"
if not CONFIG_PATH.exists():
    CONFIG_PATH = _here.parent / "scanner_ai" / "config.yaml"

with open(CONFIG_PATH) as f:
    config = yaml.safe_load(f)

# Allow Railway environment variable overrides for credentials
if os.environ.get("BCFY_USERNAME"):
    config["credentials"]["username"] = os.environ["BCFY_USERNAME"]
if os.environ.get("BCFY_PASSWORD"):
    config["credentials"]["password"] = os.environ["BCFY_PASSWORD"]

# OpenAI API key — from env var or config
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", config.get("openai", {}).get("api_key", ""))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("scanner_web")

# ── State ─────────────────────────────────────────────────────────────────
seen_calls: set[str] = set()
call_log: list[dict] = []          # In-memory ring buffer (last 200 calls)
call_log_by_id: dict[str, dict] = {}  # Fast lookup by call ID
MAX_LOG = 200
active_sse_queues: list[asyncio.Queue] = []
bcfy_session = requests.Session()
bcfy_logged_in = False

TALKGROUP_MAP = {tg["id"]: tg["name"] for tg in config["talkgroups"]["all"]}
PRIORITY_TG_IDS = {tg["id"] for tg in config["talkgroups"]["priority"]}

# ── Broadcastify Auth ────────────────────────────────────────────────────
def broadcastify_login():
    global bcfy_logged_in
    username = config["credentials"]["username"]
    password = config["credentials"]["password"]
    try:
        # Step 1: GET the login page to pick up any CSRF cookies
        bcfy_session.get(
            "https://www.broadcastify.com/calls/",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        # Step 2: POST credentials to the Calls login endpoint
        resp = bcfy_session.post(
            "https://www.broadcastify.com/calls/login/",
            data={"username": username, "password": password, "action": "auth", "redirect": "/calls/"},
            headers={
                "User-Agent": "Mozilla/5.0",
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": "https://www.broadcastify.com/calls/",
            },
            timeout=10,
            allow_redirects=True,
        )
        # Check we're logged in — session cookie should now be set
        if resp.status_code == 200 and "login" not in resp.url:
            bcfy_logged_in = True
            log.info("Broadcastify Calls login OK")
        else:
            # Fallback: try the main site login
            resp2 = bcfy_session.post(
                "https://www.broadcastify.com/login/",
                data={"username": username, "password": password, "action": "auth", "redirect": "/calls/"},
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=10,
                allow_redirects=True,
            )
            if resp2.status_code == 200:
                bcfy_logged_in = True
                log.info("Broadcastify login OK (fallback)")
    except Exception as e:
        log.warning(f"Broadcastify login error: {e}")

# ── Duration Filter ───────────────────────────────────────────────────────
def passes_duration(call: dict) -> bool:
    dur = float(call.get("duration", call.get("len", 0)))
    return dur >= config["filtering"]["min_duration_seconds"]

def call_uid(call: dict) -> str:
    raw = f"{call.get('filename','')}{call.get('ts','')}{call.get('tg','')}"
    return hashlib.md5(raw.encode()).hexdigest()

# ── Keyword Detection ─────────────────────────────────────────────────────
def detect_keywords(transcript: str) -> tuple[str | None, str | None]:
    t = transcript.lower()
    for kw in config["alerting"]["high_priority_keywords"]:
        if kw.lower() in t:
            return "HIGH", kw
    for kw in config["alerting"]["medium_priority_keywords"]:
        if kw.lower() in t:
            return "MEDIUM", kw
    for kw in config["alerting"]["location_keywords"]:
        if kw.lower() in t:
            return "MEDIUM", f"📍 {kw}"
    return None, None

# ── OpenAI Whisper Transcription ──────────────────────────────────────────
def transcribe_openai(audio_url: str) -> str:
    """Download audio from Broadcastify and transcribe via OpenAI Whisper API."""
    if not OPENAI_API_KEY:
        log.warning("No OpenAI API key — transcription disabled")
        return "[transcription unavailable]"

    try:
        # Download audio using authenticated Broadcastify session
        resp = bcfy_session.get(
            audio_url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://www.broadcastify.com/",
            },
            timeout=20,
            stream=True,
        )
        if resp.status_code != 200:
            log.warning(f"Audio download failed: {resp.status_code} — {audio_url}")
            return ""

        suffix = ".mp3" if "mp3" in audio_url else ".m4a"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            for chunk in resp.iter_content(8192):
                tmp.write(chunk)
            tmp_path = tmp.name

        # Send to OpenAI Whisper API
        with open(tmp_path, "rb") as audio_file:
            whisper_resp = requests.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                files={"file": (f"audio{suffix}", audio_file, "audio/mpeg")},
                data={
                    "model": "whisper-1",
                    "language": "en",
                    "prompt": (
                        "North Texas public safety radio dispatch. NTIRN system. "
                        "Agencies: Grapevine Police, Grapevine Fire, Euless Police, Euless Fire, "
                        "Colleyville Police, Southlake Police, Keller Police. "
                        "Common terms: Code 4, en route, on scene, clear, 10-4, structure fire, "
                        "working fire, MVA, domestic disturbance, welfare check, shots fired, EMS requested."
                    ),
                },
                timeout=30,
            )

        os.unlink(tmp_path)

        if whisper_resp.status_code == 200:
            text = whisper_resp.json().get("text", "").strip()
            # Filter out hallucinated phrases Whisper sometimes produces on silence
            block_phrases = config.get("transcription", {}).get("block_phrases", [])
            for phrase in block_phrases:
                if phrase.lower() in text.lower():
                    return ""
            log.info(f"Transcribed: {text[:80]}")
            return text
        else:
            log.warning(f"OpenAI Whisper error {whisper_resp.status_code}: {whisper_resp.text[:200]}")
            return ""

    except Exception as e:
        log.debug(f"Transcribe error: {e}")
        return ""

def transcribe(audio_url: str) -> str:
    """Transcription entry point — uses OpenAI Whisper API."""
    if not audio_url:
        return "[transcription unavailable]"
    result = transcribe_openai(audio_url)
    return result if result else "[transcription unavailable]"

# ── Broadcastify Calls Polling ────────────────────────────────────────────
PLAYLIST_UUID = "74d8c1ad-432c-11f1-bb32-0ef97433b5f9"
_playlist_pos: int = 0

def fetch_all_calls() -> list[dict]:
    """Poll the Mid Cities playlist via live-calls API — one request for all channels."""
    global _playlist_pos

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Referer": f"https://www.broadcastify.com/calls/playlists/?uuid={PLAYLIST_UUID}",
        "Origin": "https://www.broadcastify.com",
    }

    # sessionKey is required by Broadcastify — set via BCFY_SESSION_KEY env var
    session_key = os.environ.get("BCFY_SESSION_KEY", config.get("broadcastify", {}).get("session_key", ""))

    payload = {
        "playlist_uuid": PLAYLIST_UUID,
        "pos": _playlist_pos,
        "doInit": 1 if _playlist_pos == 0 else 0,
        "sid": 0,
        "systemId": 0,
        "sessionKey": session_key,
    }

    try:
        r = bcfy_session.post(
            "https://www.broadcastify.com/calls/apis/live-calls",
            data=payload,
            headers=headers,
            timeout=15,
        )
        log.info(f"playlist poll status={r.status_code} pos={_playlist_pos}")

        if r.status_code == 200:
            try:
                data = r.json()
            except Exception:
                log.warning(f"playlist: non-JSON response: {r.text[:300]}")
                return []

            raw_calls = data.get("calls", [])
            new_pos = data.get("pos", data.get("lastPos", None))
            if new_pos is not None:
                _playlist_pos = int(new_pos)
            elif raw_calls:
                latest = max(int(c.get("attrs", c).get("ts", 0)) for c in raw_calls)
                if latest:
                    _playlist_pos = latest

            log.info(f"playlist: {len(raw_calls)} calls received, new pos={_playlist_pos}")
            return [_normalize_call(c) for c in raw_calls]

        elif r.status_code in (401, 403):
            log.warning(f"playlist: auth error {r.status_code} — re-logging in")
            broadcastify_login()
        else:
            log.warning(f"playlist: unexpected status {r.status_code}: {r.text[:200]}")

    except Exception as e:
        log.error(f"fetch_all_calls: {e}")
    return []

def _normalize_call(c: dict) -> dict:
    """Normalize a raw call from the live-calls API response."""
    attrs = c.get("attrs", c)
    tg_id = int(attrs.get("tg", attrs.get("call_tg", 0)))
    system_id = int(attrs.get("sid", attrs.get("systemId", 0)))
    fn = attrs.get("filename", "")
    h = attrs.get("hash", "")
    enc = attrs.get("enc", "mp3")
    cdn = "calls-ai-1" if attrs.get("tag") in (97, 98) else "calls"
    if h and fn and system_id:
        audio_url = f"https://{cdn}.broadcastify.com/{h}/{system_id}/{fn}.{enc}"
    elif fn and system_id:
        audio_url = f"https://calls.broadcastify.com/{system_id}/{fn}.{enc}"
    else:
        audio_url = ""
    return {
        "tg": tg_id,
        "ts": attrs.get("ts", 0),
        "len": float(attrs.get("len", attrs.get("call_duration", 0))),
        "filename": fn,
        "enc": enc,
        "hash": h,
        "audio_url": audio_url,
        "transcription": attrs.get("transcription", ""),
        "display": attrs.get("display", ""),
    }

# ── Build a call event dict ────────────────────────────────────────────────
def make_event(call: dict, tg_id: int) -> dict:
    tg_name = TALKGROUP_MAP.get(tg_id, f"TG {tg_id}")
    duration = int(float(call.get("duration", call.get("len", 0))))
    audio_url = call.get("audio_url", call.get("url", ""))

    transcript = transcribe(audio_url) if audio_url else "[transcription unavailable]"

    priority, keyword = detect_keywords(transcript) if transcript else (None, None)

    is_priority_tg = tg_id in PRIORITY_TG_IDS
    tg_type = next((tg["type"] for tg in config["talkgroups"]["all"] if tg["id"] == tg_id), "")

    uid = call_uid(call)

    event = {
        "id": uid,
        "timestamp": datetime.now().isoformat(),
        "timestamp_display": datetime.now().strftime("%I:%M:%S %p"),
        "tg_id": tg_id,
        "tg_name": tg_name,
        "tg_type": tg_type,
        "duration": duration,
        "transcript": transcript,
        "priority": priority,
        "keyword": keyword,
        "is_priority": is_priority_tg,
        "audio_url": audio_url,   # kept for server-side proxy use
    }
    return event

# ── SSE broadcast ─────────────────────────────────────────────────────────
async def broadcast(event: dict):
    dead = []
    for q in active_sse_queues:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        try:
            active_sse_queues.remove(q)
        except ValueError:
            pass

# ── Background polling loop ───────────────────────────────────────────────
async def polling_loop():
    broadcastify_login()
    log.info("Starting polling loop")

    poll_interval = config["filtering"]["poll_interval_seconds"]

    while True:
        try:
            calls = await asyncio.get_event_loop().run_in_executor(None, fetch_all_calls)
            for call in calls:
                uid = call_uid(call)
                if uid in seen_calls:
                    continue
                seen_calls.add(uid)

                if not passes_duration(call):
                    continue

                tg_id = call.get("tg", 0)
                event = await asyncio.get_event_loop().run_in_executor(None, make_event, call, tg_id)

                call_log.append(event)
                call_log_by_id[event["id"]] = event
                if len(call_log) > MAX_LOG:
                    removed = call_log.pop(0)
                    call_log_by_id.pop(removed["id"], None)

                await broadcast(event)

                level = event.get("priority") or "—"
                log.info(f"[{level}] {event['tg_name']} | {event['duration']}s | {event['transcript'][:60]}")

        except Exception as e:
            log.error(f"Poll error: {e}")

        await asyncio.sleep(poll_interval)

# ── FastAPI App ────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(polling_loop())
    yield

app = FastAPI(title="Scanner AI", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/api/calls")
def get_calls(limit: int = 50, priority_only: bool = False, tg_id: int = 0):
    """Return recent calls from memory."""
    results = list(reversed(call_log))
    if priority_only:
        results = [c for c in results if c.get("priority")]
    if tg_id:
        results = [c for c in results if c.get("tg_id") == tg_id]
    return results[:limit]

@app.get("/api/talkgroups")
def get_talkgroups():
    """Return configured talkgroups."""
    return {
        "priority": config["talkgroups"]["priority"],
        "all": config["talkgroups"]["all"],
    }

@app.get("/api/stats")
def get_stats():
    """Return quick stats."""
    alerts = [c for c in call_log if c.get("priority")]
    high = [c for c in alerts if c.get("priority") == "HIGH"]
    return {
        "total_calls": len(call_log),
        "total_alerts": len(alerts),
        "high_priority": len(high),
        "is_polling": True,
        "talkgroup_count": len(config["talkgroups"]["all"]),
    }

@app.get("/api/audio/{call_id}")
def proxy_audio(call_id: str):
    """
    Proxy audio from Broadcastify using the authenticated server session.
    The browser can't fetch Broadcastify audio directly (403) — this endpoint
    fetches it server-side with session cookies and streams it to the browser.
    """
    event = call_log_by_id.get(call_id)
    if not event:
        return JSONResponse({"error": "call not found"}, status_code=404)

    audio_url = event.get("audio_url", "")
    if not audio_url:
        return JSONResponse({"error": "no audio url"}, status_code=404)

    try:
        resp = bcfy_session.get(
            audio_url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://www.broadcastify.com/",
            },
            timeout=20,
            stream=True,
        )
        if resp.status_code != 200:
            log.warning(f"Audio proxy fetch failed: {resp.status_code} — {audio_url}")
            return JSONResponse({"error": f"upstream {resp.status_code}"}, status_code=502)

        content_type = "audio/mp4" if audio_url.endswith(".m4a") else "audio/mpeg"

        def stream_audio():
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk

        return StreamingResponse(
            stream_audio(),
            media_type=content_type,
            headers={
                "Accept-Ranges": "bytes",
                "Cache-Control": "no-cache",
            },
        )
    except Exception as e:
        log.error(f"Audio proxy error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/api/stream")
async def stream(request: Request):
    """SSE endpoint — push call events to the browser in real time."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=50)
    active_sse_queues.append(queue)

    async def event_gen() -> AsyncGenerator:
        try:
            # Send last 20 calls immediately on connect
            for call in list(reversed(call_log))[:20]:
                yield {"data": json.dumps(call)}
                await asyncio.sleep(0.05)
            # Then stream new ones
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield {"data": json.dumps(event)}
                except asyncio.TimeoutError:
                    yield {"data": json.dumps({"type": "heartbeat", "ts": time.time()})}
        finally:
            try:
                active_sse_queues.remove(queue)
            except ValueError:
                pass

    return EventSourceResponse(event_gen())

@app.get("/health")
def health():
    return {"status": "ok", "calls_buffered": len(call_log)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
