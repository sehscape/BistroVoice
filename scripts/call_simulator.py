"""Phone the running server the way the browser does, with a synthetic caller.

The caller's lines are spoken by a Piper voice and streamed over the
WebSocket as 16 kHz PCM in 20 ms frames, followed by "end_utterance".
The last part barges in on the booking read-back and then says "yes":
because the caller never heard the details, nothing may be booked.

    bistrovoice serve            # in one terminal
    python scripts/call_simulator.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import numpy as np
import websockets

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from bistrovoice.speech import CALLER_VOICES, Speaker  # noqa: E402

FRAME_BYTES = 640  # 20 ms of 16 kHz PCM16


class Caller:
    def __init__(self, ws, voice: Speaker):
        self.ws = ws
        self.voice = voice

    async def say(self, text: str) -> float:
        pcm = (self.voice.to_16k(self.voice.synthesize(text).wav) * 32767).astype(np.int16).tobytes()
        for i in range(0, len(pcm), FRAME_BYTES):
            await self.ws.send(pcm[i:i + FRAME_BYTES])
        await self.ws.send(json.dumps({"type": "end_utterance"}))
        print(f"\ncaller > {text}")
        return time.perf_counter()

    async def listen(self, stop_after_first_audio: bool = False) -> dict:
        """Read server events until the turn's metrics (or the first audio)."""
        seen: dict = {"tools": [], "audio": 0}
        t0 = time.perf_counter()
        while True:
            msg = await asyncio.wait_for(self.ws.recv(), timeout=90)
            if isinstance(msg, bytes):
                seen["audio"] += 1
                if seen["audio"] == 1:
                    seen["first_audio_s"] = time.perf_counter() - t0
                if stop_after_first_audio:
                    return seen
                continue
            m = json.loads(msg)
            if m["type"] == "transcript":
                print(f"  heard  : {m['text']!r} (confidence {m['confidence']})")
            elif m["type"] == "tool":
                seen["tools"].append(m)
                status = m["result"].get("status") or m["result"].get("error") or "ok"
                print(f"  tool   : {m['name']}({json.dumps(m['args'])}) -> {status}")
            elif m["type"] == "guard":
                print(f"  guard  : {m['events']}")
            elif m["type"] == "reply":
                seen["reply"] = m["text"]
                print(f"agent  > {m['text']}")
            elif m["type"] == "metrics":
                seen["metrics"] = m
                print(f"  timing : {m}")
                return seen


async def next_text(ws) -> dict:
    """Next JSON message, skipping any audio frames in between."""
    while True:
        msg = await ws.recv()
        if isinstance(msg, str):
            return json.loads(msg)


async def main(url: str) -> None:
    voice = Speaker(CALLER_VOICES[0])
    # open_timeout: a free-tier host may need up to a minute to wake up
    async with websockets.connect(url, max_size=None, open_timeout=90) as ws:
        ready = json.loads(await ws.recv())
        print(f"connected: {ready}")
        while (await next_text(ws))["type"] != "bookings":
            pass
        caller = Caller(ws, voice)

        await caller.say("Can I reserve a table for four people on Thursday at 7:30pm? "
                         "Also, does the pesto contain nuts? I'm allergic.")
        await caller.listen()

        # Barge in on the read-back: stop at the first audio chunk.
        await caller.say("Lucy Brennan, 07700 900211.")
        await caller.listen(stop_after_first_audio=True)
        await ws.send(json.dumps({"type": "interrupt", "heard": 0}))
        print("  (caller talks over the read-back)")
        while (await next_text(ws)).get("state") != "interrupted":
            pass  # audio already on its way is skipped

        await caller.say("Yes, that's right.")
        turn = await caller.listen()
        statuses = [t["result"].get("status") for t in turn["tools"]]
        print("\nRESULT: " + ("FAIL - booked details the caller never heard" if "booked" in statuses
                              else "pass - interrupted read-back not treated as confirmed"
                              + (" (read back again)" if "needs_confirmation" in statuses else "")))

        await caller.say("Yes please.")
        turn = await caller.listen()
        booked = [t for t in turn["tools"] if t["result"].get("status") == "booked"]
        print("RESULT: " + ("pass - booked after a read-back the caller heard"
                            if booked else "no booking on this turn"))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://127.0.0.1:8000/ws")
    asyncio.run(main(ap.parse_args().url))
