#!/usr/bin/env python3
"""
Scanner AI — FastAPI Backend
Polls Broadcastify Calls API, transcribes, detects keywords.
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
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("scanner_web")

# ── State ─────────────────────────────────────────────────────────────────
seen_calls: set[str] = set()
call_log: list[dict] = []          # In-memory ring buffer (last 200 calls)
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
        resp = bcfy_session.post(
            "https://www.broadcastify.com/login",
            data={"username": username, "password": password, "action": "auth", "redirect": "/"},
            headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
        )
        if resp.status_code == 200:
            bcfy_logged_in = True
            log.info("Broadcastify login OK")
    except Exception as e:
        log.warning(f"Broadcastify login error: {e}")

# ── Duration Filter ───────────────────────────────────────────────────────
def passes_duration(call: dict) -> bool:
    dur = int(call.get("duration", call.get("len", 0)))
    return dur >= config["filtering"]["min_duration_seconds"]

def call_uid(call: dict) -> str:
    raw = f"{call.get('id','')}{call.get('start_time','')}{call.get('ts','')}{call.get('tg','')}"
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

# ── Transcription ─────────────────────────────────────────────────────────
_whisper_model = None

def get_whisper():
    global _whisper_model
    if _whisper_model is None:
        try:
            from faster_whisper import WhisperModel
            _whisper_model = WhisperModel(
                config["transcription"]["model_size"],
                device="cpu",
                compute_type="int8",
            )
            log.info("Whisper loaded")
        except ImportError:
            log.warning("faster-whisper not installed — transcription disabled")
            return None
    return _whisper_model

def transcribe(audio_url: str) -> str:
    try:
        model = get_whisper()
        if model is None:
            return "[transcription unavailable]"
        resp = bcfy_session.get(audio_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15, stream=True)
        if resp.status_code != 200:
            return ""
        suffix = ".mp3" if "mp3" in audio_url else ".m4a"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            for chunk in resp.iter_content(8192):
                tmp.write(chunk)
            tmp_path = tmp.name

        segs, _ = model.transcribe(
            tmp_path,
            language="en",
            initial_prompt=config["transcription"]["initial_prompt"],
            beam_size=config["transcription"]["beam_size"],
            no_speech_threshold=config["transcription"]["no_speech_threshold"],
            vad_filter=True,
        )
        text = " ".join(s.text.strip() for s in segs).strip()
        os.unlink(tmp_path)

        for phrase in config["transcription"].get("block_phrases", []):
            if phrase.lower() in text.lower():
                return ""
        return text
    except Exception as e:
        log.debug(f"Transcribe error: {e}")
        return ""

# ── Broadcastify Calls Polling ────────────────────────────────────────────
def fetch_calls(tg_id: int) -> list[dict]:
    """Try multiple Broadcastify Calls API endpoints."""
    since = int((datetime.utcnow() - timedelta(minutes=config["filtering"]["lookback_minutes"])).timestamp())
    
    endpoints = [
        f"https://www.broadcastify.com/calls/feed?sys=7349&tg={tg_id}&since={since}",
        f"https://api.broadcastify.com/call-feed/7349/{tg_id}?since={since}",
    ]
    
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
    }
    
    for url in endpoints:
        try:
            r = bcfy_session.get(url, headers=headers, timeout=8)
            if r.status_code == 200:
                try:
                    data = r.json()
                    calls = data.get("calls", data.get("results", []))
                    if isinstance(calls, list):
                        for c in calls:
                            c["tg"] = c.get("tg", tg_id)
                        return calls
                except ValueError:
                    continue
        except requests.RequestException:
            continue
    return []

# ── Build a call event dict ────────────────────────────────────────────────
def make_event(call: dict, tg_id: int) -> dict:
    tg_name = TALKGROUP_MAP.get(tg_id, f"TG {tg_id}")
    duration = int(call.get("duration", call.get("len", 0)))
    audio_url = call.get("audio_url", call.get("url", ""))
    
    transcript = ""
    if audio_url:
        transcript = transcribe(audio_url)

    priority, keyword = detect_keywords(transcript) if transcript else (None, None)

    is_priority_tg = tg_id in PRIORITY_TG_IDS
    tg_type = next((tg["type"] for tg in config["talkgroups"]["all"] if tg["id"] == tg_id), "")

    event = {
        "id": call_uid(call),
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
        "audio_url": audio_url,
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
    
    # Pre-load whisper in background
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, get_whisper)
    
    tg_ids = (
        [tg["id"] for tg in config["talkgroups"]["priority"]]
        + [tg["id"] for tg in config["talkgroups"]["all"] if tg["id"] not in {t["id"] for t in config["talkgroups"]["priority"]}]
    )
    tg_ids = list(dict.fromkeys(tg_ids))  # dedupe preserve order

    poll_interval = config["filtering"]["poll_interval_seconds"]
    
    while True:
        for tg_id in tg_ids:
            try:
                calls = await asyncio.get_event_loop().run_in_executor(None, fetch_calls, tg_id)
                for call in calls:
                    uid = call_uid(call)
                    if uid in seen_calls:
                        continue
                    seen_calls.add(uid)

                    if not passes_duration(call):
                        continue

                    # Transcribe + detect in thread pool
                    event = await asyncio.get_event_loop().run_in_executor(None, make_event, call, tg_id)
                    
                    # Append to ring buffer
                    call_log.append(event)
                    if len(call_log) > MAX_LOG:
                        call_log.pop(0)

                    # Broadcast to all SSE clients
                    await broadcast(event)

                    level = event.get("priority") or "—"
                    log.info(f"[{level}] {event['tg_name']} | {event['duration']}s | {event['transcript'][:60]}")

            except Exception as e:
                log.error(f"Poll error TG {tg_id}: {e}")

        await asyncio.sleep(poll_interval)

# ── FastAPI App ────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(polling_loop())
    yield

# Health check responds immediately even before Whisper loads

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
