"""Pick the Whisper model size with data, not guesswork.

Customer lines are spoken by two Piper voices (US and northern English),
then transcribed by tiny.en, base.en and small.en. Reports word error rate,
how often the details that matter (numbers, times, dish names) survive,
and latency.

    python eval/bench_stt.py
"""

from __future__ import annotations

import json
import re
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bistrovoice.db import build_database  # noqa: E402
from bistrovoice.speech import CALLER_VOICES, Speaker, Transcriber, menu_hint  # noqa: E402

LINES = [
    "Can I reserve a table for four people at seven thirty tonight?",
    "Also, does the pesto contain nuts? I'm allergic.",
    "My name is Priya Shah and my number is 07700 900456.",
    "Yes, that's right, please book it.",
    "Do you have a table for two on Friday at eight?",
    "Is the sticky toffee pudding safe for someone with a nut allergy?",
    "What desserts do you have?",
    "Could we do half past nine instead?",
    "Does the calamari have shellfish in it?",
    "I'd like to speak to a manager please.",
    "Table for six on Saturday at seven fifteen, please.",
    "Is the trofie al pesto vegetarian?",
    # Both misheard as "not free" in the first voice evaluation:
    "Is the sticky toffee pudding nut free?",
    "Which of your desserts are nut free?",
]
# What must survive transcription for the agent to act correctly.
KEY_FACTS = [["four|4", r"7[:.]?30|seven thirty"], ["pesto", "nuts"], ["priya", r"07700 ?900 ?456"],
             ["yes"], ["two|2", "friday", r"at (eight|8)"], ["sticky toffee", "nut"], ["dessert"],
             [r"9[:.]30|half[- ]past (nine|9)|nine thirty"], ["calamari", "shellfish"], ["manager"],
             ["six|6", "saturday", r"7[:.]?15|seven fifteen"], ["trofie", "pesto"],
             ["sticky toffee", r"nut[- ]free"], ["dessert", r"nut[- ]free"]]


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def wer(ref: str, hyp: str) -> float:
    r, h = words(ref), words(hyp)
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev, d[j] = d[j], cur
    return d[len(h)] / max(1, len(r))


def main() -> None:
    conn = build_database(":memory:")
    hint = menu_hint(conn)
    audio = []
    for voice in CALLER_VOICES:
        spk = Speaker(voice)
        for line in LINES:
            audio.append((voice, line, spk.to_16k(spk.synthesize(line).wav)))

    results = {}
    configs = [("tiny.en", True, 1), ("base.en", False, 1), ("base.en", True, 1),
               ("base.en", True, 5), ("small.en", True, 1)]
    for model, use_hint, beam in configs:
        name = f"{model}{' + prompt' if use_hint else ''}, beam {beam}"
        stt = Transcriber(model, hint=hint if use_hint else "", beam_size=beam)
        stt.transcribe(audio[0][2])  # warm-up
        errs, ms, facts, confs, rtf = [], [], [], [], []
        for i, (_voice, line, pcm) in enumerate(audio):
            t = stt.transcribe(pcm)
            errs.append(wer(line, t.text))
            ms.append(t.ms)
            rtf.append(t.ms / 1000 / t.audio_seconds)
            confs.append(t.confidence)
            need = KEY_FACTS[i % len(LINES)]
            low = t.text.lower()
            facts.append(all(re.search(f, low) for f in need))
            if not facts[-1]:
                print(f"  {name}: missed {need} in {t.text!r}")
        results[name] = {
            "wer": round(statistics.mean(errs), 3),
            "key_facts_kept": f"{sum(facts)}/{len(facts)}",
            "p50_ms": round(statistics.median(ms)),
            "max_ms": round(max(ms)),
            "real_time_factor": round(statistics.mean(rtf), 3),
            "mean_confidence": round(statistics.mean(confs), 3),
        }
        print(name, results[name])

    out = ROOT / "eval" / "results" / "stt_benchmark.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
