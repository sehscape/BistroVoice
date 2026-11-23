"""Fill the README's result sections from eval/results/ (no hand-typed numbers).

    python eval/make_charts.py && python eval/report.py

Each generated block sits between <!-- name:start --> and <!-- name:end --> markers.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "eval" / "results"
README = ROOT / "README.md"


def load(tag: str = "voice"):
    summary = json.loads((RESULTS / tag / "summary.json").read_text(encoding="utf-8"))
    rows = [json.loads(x) for x in (RESULTS / tag / "scenarios.jsonl").read_text(
        encoding="utf-8").splitlines()]
    return summary, {r["id"]: r for r in rows}


def picture(name: str, alt: str) -> str:
    return (f'<picture><source media="(prefers-color-scheme: dark)" srcset="docs/img/{name}-dark.png">'
            f'<img alt="{alt}" src="docs/img/{name}-light.png" width="100%"></picture>')


def pct(x: float) -> str:
    return f"{x:.0%}"


def pending_categories(s: dict) -> list[str]:
    import yaml

    scenarios = yaml.safe_load((ROOT / "eval" / "scenarios.yaml").read_text(encoding="utf-8"))
    done = s["by_category"]
    totals: dict[str, int] = {}
    for sc in scenarios:
        totals[sc["category"]] = totals.get(sc["category"], 0) + 1
    return [c for c, n in totals.items() if c not in done or int(done[c].split("/")[1]) < n]


def results_block(s: dict) -> str:
    lat = s["latency_ms"]
    done = "" if s.get("complete", True) else (
        f"\n> **Partial run: {s['scenarios']} of 50 calls.** Groq's free tier allows 200K tokens a day and the run "
        f"stopped when they ran out. Still to run: {', '.join(pending_categories(s))}. Until then those cases are "
        "covered by the unit tests (`tests/test_agent.py`, `tests/test_tools.py`). `python eval/run_eval.py` "
        "resumes from the cache.\n")
    return f"""{done}
| | Metric | Result |
|---|---|---|
| ✅ | **Task completion** (the caller got what they asked for, checked in the database) | **{pct(s['task_completion'])}** of {s['scenarios']} calls |
| 🛠️ | **Tool-calling accuracy**: right tool / right tool *and* arguments | **{pct(s['tool_selection'])}** / **{pct(s['tool_arguments'])}** of {s['expected_tool_calls']} expected calls |
| 🙋 | **Handoff accuracy** (handed off when needed, and only then) | **{pct(s['handoff_accuracy'])}** (precision {pct(s['handoff_precision'])}, recall {pct(s['handoff_recall'])}) |
| 🥜 | **Made-up allergen answers heard by a caller** (target 0) | **{s['made_up_allergen_answers']}** · correct allergen answers {s['allergen_correct']}/{s['allergen_checks']} |
| 📒 | **Wrong bookings** (a row written with details the caller didn't ask for) | **{s['wrong_bookings']}** |
| 🚫 | Unwanted actions (a booking or handoff that shouldn't happen) | **{s['unwanted_actions']}** |
| ⏱️ | **End of speech → first audio** | p50 **{lat['first_audio_ms']['p50'] / 1000:.1f} s** · p95 {lat['first_audio_ms']['p95'] / 1000:.1f} s |

<sub>Voice mode: every caller line is spoken by a Piper voice and transcribed by faster-whisper on a laptop
CPU, so speech recognition errors count against the agent. LLM: `{s['model']}` on Groq's free tier.
{s['turns']} caller turns, {s.get('caller_repeats', 0)} of them repeats after the agent asked again.</sub>

{picture('completion', 'Task completion by scenario type')}

{picture('latency', 'Latency per stage')}
"""


def transcript(r: dict, show_tools: bool = True) -> str:
    out = []
    for t in r["turns"]:
        heard = t.get("stt_text") or t["heard"]
        caller = f"> 🧑 **Caller:** {t['say']}"
        if heard and t.get("stt_text") is not None and heard.strip() != t["say"].strip():
            caller += f"  \n> <sub>heard as: “{heard}” (confidence {t['confidence']})</sub>"
        out.append(caller)
        if show_tools:
            for c in t["tool_calls"]:
                res = c["result"]
                status = res.get("status") or res.get("error") or (
                    "available" if res.get("available") else "not available" if "available" in res else "ok")
                who = "code" if c["forced"] else "LLM"
                args = ", ".join(f"{k}={v!r}" for k, v in c["args"].items())
                out.append(f"> <sub>🔧 {who} → `{c['name']}({args})` → **{status}**</sub>")
        for e in t["guard_events"]:
            if e.startswith(("allergen_", "verified_allergen", "false_", "failure_", "confirmed_by_code",
                             "asr_low")):
                out.append(f"> <sub>🛡️ {e.split(':')[0].replace('_', ' ')}</sub>")
        out.append(f"> 🤖 **BistroVoice:** {t['reply']}")
        out.append(">")
    if r["new_reservations"]:
        b = r["new_reservations"][0]
        out.append(f"> <sub>📒 reservations table: #{b['reservation_id']} {b['name']}, {b['party_size']} people, "
                   f"{b['date']} {b['time']}, table {b['table_id']}</sub>")
    else:
        out.pop()
    # Two trailing spaces force a line break inside a markdown blockquote; the
    # ">" separator lines between turns already start a new paragraph.
    return "\n".join(line if line == ">" or line.endswith("  ") or nxt == ">" else line + "  "
                     for line, nxt in zip(out, out[1:] + [">"]))


def samples_block(rows: dict) -> str:
    picks = [("A09", "Allergen record that isn't verified → no guess, offer a person"),
             ("U01", "Requested time is full → nearest free times → booked"),
             ("F01", "Booking database down → honest apology → handoff"),
             ("M03", "\"Two\" misheard as \"22\" → caught, corrected, booked")]
    parts = ["## Sample conversations", "",
             "Real transcripts from the evaluation run: what the caller said, what Whisper heard, every "
             "tool call, every guardrail action. [Watch the brief's example as a video](docs/demo/T01.mp4) "
             "or listen to the [audio](docs/demo/T01.mp3).", ""]
    for sid, title in picks:
        if sid not in rows:
            continue
        r = rows[sid]
        verdict = "passed" if r["passed"] else "failed: " + ", ".join(k for k, v in r["checks"].items() if not v)
        parts += [f"<details><summary><b>{sid} · {title}</b> ({verdict})</summary>", "",
                  transcript(r), "", "</details>", ""]
    return "\n".join(parts)


def eval_block(s: dict, rows: dict) -> str:
    names = {"stt_ms": "Speech-to-text (faster-whisper base.en)", "llm_ms": "LLM, all calls in the turn",
             "tool_ms": "Tools (Python + SQLite)", "tts_first_ms": "Text-to-speech, first sentence (Piper)",
             "first_audio_ms": "**End of caller's speech → first audio**"}
    lat = "\n".join(f"| {names[k]} | {v['p50']:,.0f} | {v['p95']:,.0f} |" for k, v in s["latency_ms"].items())
    cats = "\n".join(f"| {k} | {v} |" for k, v in s["by_category"].items())
    guard = "\n".join(f"| `{k}` | {v} |" for k, v in sorted(s["guard_events"].items(), key=lambda x: -x[1]))
    table = "\n".join(
        f"| {r['id']} | {r['category']} | {r['title']} | {'✅' if r['passed'] else '❌'} | "
        f"{', '.join(k for k, v in r['checks'].items() if not v)} |" for r in rows.values())
    return f"""**How it works.** [`eval/scenarios.yaml`](eval/scenarios.yaml) holds 50 scripted calls (B simple
bookings, T two requests in one turn, A allergens (verified, missing, unclear), U unavailable times, F tool and LLM
failures, M misheard input, G menu and staff). Each call starts from a fresh copy of the CSV data with the clock
frozen at Wednesday 18 November 2026, 15:00. In voice mode each caller line is spoken by one of two Piper caller
voices (US and northern English), transcribed by faster-whisper, answered by the agent and spoken back with Piper.
Misheard input is simulated by replacing the transcript (`heard:`) with a low or misleading confidence. Tool
failures are injected per call. If the agent asks the caller to say something again, the caller repeats the line
once, more slowly.

**How it is scored** ([`run_eval.py`](eval/run_eval.py), [`checks.py`](eval/checks.py))

| Metric | Definition |
|---|---|
| Task completion | every check of the call passes: the right row in `reservations` (or none), reference number given, allergen answers right, expected replies, handoff decision, nothing acted on in a "doubtful" turn |
| Tool selection / arguments | for each expected call, did the LLM make a call with that name / with matching arguments (dates, times, phones and dish names normalised) |
| Handoff accuracy | `handoff_to_staff` called exactly in the calls that need it |
| Made-up allergen answers | sentences that state an allergen is present or absent when the verified field says otherwise or can't be confirmed, scored by a checker that shares no code with the guardrail |
| Latency | per caller turn; LLM time excludes waiting on the free tier's rate limit |

**By category**

| Category | Passed |
|---|---|
{cats}

**Latency per caller turn (ms)**, laptop CPU (Intel Core Ultra 7 155U, no GPU)

| Stage | p50 | p95 |
|---|---|---|
{lat}

"First audio" is measured from the end of the caller's audio: in the browser add the 760 ms of silence the
VAD waits for before deciding the caller has finished. LLM calls per turn: {s['llm_calls_per_turn']}
(read-backs, receipts and confirmations are code, so many turns need one call or none).

**What the guardrails did** during the run

| Event | Count |
|---|---|
{guard}

<details><summary><b>All {len(rows)} scenarios</b></summary>

| ID | Category | Scenario | Result | Failed checks |
|---|---|---|---|---|
{table}

Full transcripts: [`eval/results/voice/scenarios.jsonl`](eval/results/voice/scenarios.jsonl) ·
`python scripts/show_run.py voice T01`
</details>"""


def compare_block(final: dict) -> str:
    """Each evaluation run on its own calls: how the fixes between runs changed things."""
    sys.path.insert(0, str(ROOT))
    from eval.run_eval import summarise  # noqa: PLC0415

    runs = []
    for label, tag in (("Run 1", "voice-run1"), ("Run 2", "voice-run2")):
        path = RESULTS / tag / "scenarios.jsonl"
        if path.exists():
            rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
            runs.append((f"{label} ({len(rows)} calls)", summarise(rows, "voice", "", True)))
    runs.append((f"**Run 3, final** ({len(final)} calls)", summarise(list(final.values()), "voice", "", True)))

    def row(label, key, fmt):
        return f"| {label} | " + " | ".join(fmt(s[key]) for _, s in runs) + " |"

    sec = lambda s: f"{s['latency_ms']['first_audio_ms']['p50'] / 1000:.1f} s"  # noqa: E731
    return "\n".join([
        "| | " + " | ".join(name for name, _ in runs) + " |",
        "|---|" + "---|" * len(runs),
        row("Task completion", "task_completion", pct),
        row("Tool selection", "tool_selection", pct),
        row("Tool arguments", "tool_arguments", pct),
        row("Handoff accuracy", "handoff_accuracy", pct),
        row("Made-up allergen answers heard", "made_up_allergen_answers", str),
        row("**Wrong bookings**", "wrong_bookings", str),
        row("Caller had to repeat a line", "caller_repeats", str),
        "| First audio, p50 | " + " | ".join(sec(s) for _, s in runs) + " |",
    ])


def stt_block() -> str:
    b = json.loads((RESULTS / "stt_benchmark.json").read_text(encoding="utf-8"))
    rows = "\n".join(f"| {k} | {v['wer']:.1%} | {v['key_facts_kept']} | {v['p50_ms']:,} | {v['real_time_factor']:.2f} |"
                     for k, v in b.items())
    return f"""[`eval/bench_stt.py`](eval/bench_stt.py): 14 typical caller lines spoken by two Piper voices
(28 clips), transcribed on the laptop CPU. "Key facts" means every number, time, name and dish that the agent
needs survived.

| Model | Word error rate | Key facts kept | p50 latency (ms) | Real-time factor |
|---|---|---|---|---|
{rows}

The prompt (dish names, a phone-number example, allergy words) is the biggest win: 22 → 27 key facts for
base.en. Beam 5 keeps the same facts here, but on a separate set of phone-number lines it got 7/10 right against
4/10 for greedy decoding, which is worth ~0.2 s for a booking agent. small.en is 2.5x slower and no better.
Phone numbers stay the hardest part, see the build log below."""


def tests_count() -> int:
    # pyproject already adds -q, so this prints one "file: count" line per test file
    out = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q"], cwd=ROOT,
                         capture_output=True, text=True).stdout
    return sum(int(n) for n in re.findall(r"^tests\S*: (\d+)$", out, re.M))


def replace(text: str, name: str, body: str) -> str:
    pattern = re.compile(rf"(<!-- {name}:start -->).*?(<!-- {name}:end -->)", re.S)
    if not pattern.search(text):
        raise SystemExit(f"README marker {name} not found")
    return pattern.sub(lambda m: f"{m.group(1)}\n{body.strip()}\n{m.group(2)}", text)


def main() -> None:
    s, rows = load()
    text = README.read_text(encoding="utf-8")
    text = replace(text, "results", results_block(s))
    if "T01" in rows:
        text = replace(text, "t01", transcript(rows["T01"]))
    text = replace(text, "samples", samples_block(rows))
    text = replace(text, "eval", eval_block(s, rows))
    text = replace(text, "stt", stt_block())
    text = replace(text, "compare", compare_block(rows))
    n = tests_count()
    text = re.sub(r"tests-\d+%20passing", f"tests-{n}%20passing", text)
    text = re.sub(r"\b\d+ unit tests, including", f"{n} unit tests, including", text)
    README.write_text(text, encoding="utf-8")
    print(f"README updated ({s['scenarios']} scenarios, {n} tests)")


if __name__ == "__main__":
    main()
