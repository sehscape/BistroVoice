"""FastAPI app: the browser streams microphone audio over a WebSocket,
the server runs speech-to-text -> agent -> text-to-speech and streams
the reply back sentence by sentence.

Protocol (one WebSocket per call)
  client -> server  binary   16 kHz mono PCM16 while the customer speaks
                    {"type": "end_utterance"}       customer stopped talking
                    {"type": "interrupt", "heard": n} customer talked over the
                                                    agent; n = sentences heard
                    {"type": "text", "text": "..."} typed instead of spoken
                    {"type": "reset"}               start a new call
  server -> client  {"type": "transcript" | "tool" | "reply" | "audio" |
                     "metrics" | "bookings" | "status", ...}
                    binary   WAV for the preceding "audio" message
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .agent import Agent
from .config import WEB_DIR, Settings
from .db import build_database, load_rules
from .llm import LLMClient
from .speech import SAMPLE_RATE, Speaker, make_transcriber, menu_hint, pcm16_to_float
from .tools import ToolContext

log = logging.getLogger("bistrovoice")
MIN_UTTERANCE_SECONDS = 0.3


class Services:
    """Models are loaded once per process and shared by all calls."""

    def __init__(self, settings: Settings):
        self.settings = settings
        hint_db = build_database(":memory:")
        self.stt = make_transcriber(settings, hint=menu_hint(hint_db))
        self.tts = Speaker(settings.tts_voice)
        self.llm = LLMClient(settings)
        self.stt_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="stt")
        self.tts_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tts")
        # Warm up so the first caller doesn't pay for model initialisation.
        if settings.stt_backend == "local":
            self.stt.transcribe(pcm16_to_float(b"\0\0" * SAMPLE_RATE))
        self.tts.synthesize("Hello.")


services: Services | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global services
    logging.basicConfig(level=logging.INFO)
    services = await asyncio.to_thread(Services, Settings.from_env())
    log.info("models loaded: stt=%s tts=%s llm=%s", services.settings.stt_model,
             services.settings.tts_voice, services.settings.llm_model)
    yield


app = FastAPI(title="BistroVoice", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/health")
async def health():
    s = services.settings if services else Settings.from_env()
    return {"ok": services is not None, "stt": s.stt_model, "tts": s.tts_voice, "llm": s.llm_model}


class Call:
    """One phone call: its own copy of the restaurant database and agent.

    Every call starts from the same spreadsheet data, so visitors to the
    hosted demo can't fill up each other's Friday night.
    """

    def __init__(self, ws: WebSocket, svc: Services):
        self.ws = ws
        self.svc = svc
        self.audio = bytearray()
        self.task: asyncio.Task | None = None
        self.sentences: list[str] = []
        # Agent work runs on one thread per call, so turns never overlap even
        # when an interrupted turn is still finishing a tool call.
        self.agent_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="agent")
        self.new_agent()

    def new_agent(self) -> None:
        conn = build_database(":memory:", align_to=date.today())
        rules = load_rules(conn)
        tz = ZoneInfo(rules.timezone)
        self.ctx = ToolContext(conn=conn, rules=rules, clock=lambda: datetime.now(tz))
        self.agent = Agent(self.ctx, self.svc.llm)

    async def send(self, kind: str, **data) -> None:
        await self.ws.send_text(json.dumps({"type": kind, **data}, ensure_ascii=False))

    async def send_bookings(self, highlight: str | None = None) -> None:
        today = self.ctx.now().date().isoformat()
        rows = self.ctx.conn.execute(
            "SELECT reservation_id, name, party_size, date, time, table_id FROM reservations "
            "WHERE date >= ? ORDER BY date, time LIMIT 60", (today,)).fetchall()
        handoffs = self.ctx.conn.execute(
            "SELECT handoff_id, reason, created_at FROM handoffs ORDER BY handoff_id").fetchall()
        await self.send("bookings", today=today, highlight=highlight,
                        reservations=[dict(r) for r in rows], handoffs=[dict(h) for h in handoffs])

    async def run(self) -> None:
        s = self.svc.settings
        stt = s.stt_model if s.stt_backend == "local" else "whisper-large-v3-turbo (Groq)"
        await self.send("status", state="ready", restaurant=self.ctx.rules.name,
                        stt=stt, llm=s.llm_model,
                        tts=self.svc.settings.tts_voice)
        await self.send_bookings()
        while True:
            msg = await self.ws.receive()
            if msg["type"] == "websocket.disconnect":
                raise WebSocketDisconnect()
            if msg.get("bytes") is not None:
                if len(self.audio) < SAMPLE_RATE * 2 * 30:  # cap one utterance at 30 s
                    self.audio.extend(msg["bytes"])
                continue
            data = json.loads(msg.get("text") or "{}")
            kind = data.get("type")
            if kind == "end_utterance":
                pcm, self.audio = bytes(self.audio), bytearray()
                self.start(self.turn(pcm=pcm))
            elif kind == "text" and data.get("text", "").strip():
                self.start(self.turn(text=data["text"].strip()))
            elif kind == "interrupt":
                await self.interrupt(int(data.get("heard", 0)))
            elif kind == "reset":
                await self.cancel()
                await asyncio.get_running_loop().run_in_executor(self.agent_pool, self.new_agent)
                await self.send("status", state="reset")
                await self.send_bookings()

    def start(self, coro) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
        self.task = asyncio.create_task(coro)

    async def cancel(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass

    async def interrupt(self, heard: int) -> None:
        """Customer barged in: stop speaking and record what they heard."""
        await self.cancel()
        heard_text = " ".join(self.sentences[:heard])
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self.agent_pool, self.agent.mark_interrupted, heard_text)
        await self.send("status", state="interrupted", heard=heard)

    async def turn(self, pcm: bytes | None = None, text: str | None = None) -> None:
        loop = asyncio.get_running_loop()
        t0 = time.perf_counter()
        self.sentences = []
        metrics: dict = {"stt_ms": 0.0}
        confidence = None
        try:
            if pcm is not None:
                if len(pcm) < SAMPLE_RATE * 2 * MIN_UTTERANCE_SECONDS:
                    return
                await self.send("status", state="transcribing")
                tr = await loop.run_in_executor(self.svc.stt_pool, self.svc.stt.transcribe,
                                                pcm16_to_float(pcm))
                metrics["stt_ms"] = round(tr.ms)
                text, confidence = tr.text, tr.confidence
                await self.send("transcript", text=text or "(nothing heard)", confidence=confidence)
                if not text:
                    await self.send("status", state="listening")
                    return
            else:
                await self.send("transcript", text=text, confidence=None)

            await self.send("status", state="thinking")
            # Run on the call's own thread. If this task is cancelled (barge-in)
            # the agent still finishes: a booking half-way through a tool call
            # is completed and recorded, never left in an unknown state.
            fut = loop.run_in_executor(self.agent_pool, self.agent.respond, text, confidence)
            result = await asyncio.shield(fut)

            for c in result.tool_calls:
                await self.send("tool", name=c.name, args=c.args, result=c.result, ms=round(c.ms),
                                forced=c.forced)
            if result.guard_events:
                await self.send("guard", events=result.guard_events)
            booked = next((c.result.get("reference") for c in result.tool_calls
                           if c.result.get("status") == "booked"), None)
            if booked or any(c.name == "handoff_to_staff" for c in result.tool_calls):
                await self.send_bookings(highlight=booked)

            metrics.update(llm_ms=round(result.llm_ms), llm_calls=result.llm_calls,
                           tool_ms=round(result.tool_ms))
            await self.send("reply", text=result.reply)

            self.sentences = self.svc.tts.sentences(result.reply)
            tts_ms = 0.0
            for i, sentence in enumerate(self.sentences):
                chunk = await loop.run_in_executor(self.svc.tts_pool, self.svc.tts.synthesize, sentence)
                tts_ms += chunk.ms
                if i == 0:
                    metrics["tts_first_ms"] = round(chunk.ms)
                    metrics["first_audio_ms"] = round((time.perf_counter() - t0) * 1000)
                    await self.send("status", state="speaking")
                await self.send("audio", seq=i, text=sentence, seconds=round(chunk.seconds, 2))
                await self.ws.send_bytes(chunk.wav)
            metrics["tts_ms"] = round(tts_ms)
            metrics["total_ms"] = round((time.perf_counter() - t0) * 1000)
            await self.send("metrics", **metrics)
        except asyncio.CancelledError:
            raise
        except WebSocketDisconnect:
            raise
        except Exception:
            log.exception("turn failed")
            await self.send("reply", text="Sorry, something went wrong on our side.")


@app.websocket("/ws")
async def websocket(ws: WebSocket):
    await ws.accept()
    call = Call(ws, services)
    try:
        await call.run()
    except WebSocketDisconnect:
        pass
    finally:
        await call.cancel()
        call.agent_pool.shutdown(wait=False)
