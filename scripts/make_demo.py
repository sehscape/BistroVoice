"""Turn an evaluation transcript into a short demo: audio + captioned video.

The caller's lines and the agent's replies are taken word for word from
eval/results/voice/scenarios.jsonl (a real run), voiced with Piper and
joined with short pauses. ffmpeg then draws a waveform video with captions.

    python scripts/make_demo.py T01 A09 F01
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from bistrovoice.config import Settings  # noqa: E402
from bistrovoice.speech import CALLER_VOICES, Speaker  # noqa: E402

OUT = ROOT / "docs" / "demo"
RATE = 22050
GAP = 0.45


def _pcm(speaker: Speaker, text: str) -> np.ndarray:
    with wave.open(io.BytesIO(speaker.synthesize(text).wav)) as w:
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        rate = w.getframerate()
    if rate != RATE:
        n = int(len(audio) * RATE / rate)
        audio = np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio).astype(np.int16)
    return audio


def _srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"


def _wrap(text: str, width: int = 70) -> str:
    lines, line = [], ""
    for word in text.split():
        if len(line) + len(word) + 1 > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    return "\n".join(lines + [line])


def render(scenario_id: str, caller: Speaker, agent: Speaker) -> None:
    rows = [json.loads(x) for x in (ROOT / "eval/results/voice/scenarios.jsonl").read_text(
        encoding="utf-8").splitlines()]
    sc = next(r for r in rows if r["id"] == scenario_id)
    pieces, subs, t = [], [], 0.6
    silence = np.zeros(int(RATE * GAP), dtype=np.int16)
    pieces.append(np.zeros(int(RATE * 0.6), dtype=np.int16))
    for turn in sc["turns"]:
        for who, speaker, text in (("Caller", caller, turn["say"]), ("Agent", agent, turn["reply"])):
            audio = _pcm(speaker, text)
            subs.append((t, t + len(audio) / RATE, f"{who}: {text}"))
            pieces += [audio, silence]
            t += len(audio) / RATE + GAP
    OUT.mkdir(parents=True, exist_ok=True)
    wav_path = OUT / f"{scenario_id}.wav"
    with wave.open(str(wav_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(np.concatenate(pieces).tobytes())
    srt = "\n".join(f"{i}\n{_srt_time(a)} --> {_srt_time(b)}\n{_wrap(text)}\n"
                    for i, (a, b, text) in enumerate(subs, 1))
    (OUT / f"{scenario_id}.srt").write_text(srt, encoding="utf-8")

    run = lambda *args: subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args],  # noqa: E731
                                       cwd=OUT, check=True)
    run("-i", wav_path.name, "-b:a", "96k", f"{scenario_id}.mp3")
    style = ("FontName=Segoe UI,FontSize=15,PrimaryColour=&H00F1E9E1,OutlineColour=&H00100D0B,"
             "BorderStyle=3,Outline=6,Shadow=0,MarginV=36,Alignment=2")
    run("-i", wav_path.name, "-filter_complex",
        "[0:a]showwaves=s=1280x720:mode=cline:rate=25:colors=0x3B74E8|0x3B74E8,"
        "format=yuv420p,drawbox=c=0x0F1114@0.0:t=fill[w];"
        f"color=c=0x0F1114:s=1280x720:r=25[bg];[bg][w]overlay=shortest=1,"
        f"subtitles={scenario_id}.srt:force_style='{style}'[v]",
        "-map", "[v]", "-map", "0:a", "-c:v", "libx264", "-crf", "28", "-preset", "veryfast",
        "-c:a", "aac", "-b:a", "96k", "-shortest", f"{scenario_id}.mp4")
    wav_path.unlink()
    (OUT / f"{scenario_id}.srt").unlink()
    print(f"{scenario_id}: {t:.0f}s -> docs/demo/{scenario_id}.mp3 / .mp4")


def main() -> None:
    settings = Settings.from_env()
    caller, agent = Speaker(CALLER_VOICES[0]), Speaker(settings.tts_voice)
    for sid in sys.argv[1:] or ["T01"]:
        render(sid, caller, agent)


if __name__ == "__main__":
    main()
