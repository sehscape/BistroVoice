"""Run the 50 scripted calls and score them.

    python eval/run_eval.py                    # voice mode: caller audio -> Whisper -> agent -> Piper
    python eval/run_eval.py --mode text        # skip audio, feed the text straight in
    python eval/run_eval.py --only T01,A09     # a few scenarios
    python eval/run_eval.py --no-guards --tag baseline   # agent without the safety code

LLM replies are cached in runs/llm_cache.sqlite, so rerunning costs no API
quota. Latency for a cached reply is the latency measured on the real call.
Results go to eval/results/<tag>/ (summary.json, results.md, scenarios.jsonl).
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from bistrovoice.agent import Agent  # noqa: E402
from bistrovoice.config import Settings  # noqa: E402
from bistrovoice.db import build_database, load_rules  # noqa: E402
from bistrovoice.llm import LLMClient, LLMError  # noqa: E402
from bistrovoice.tools import ToolContext, resolve_dish  # noqa: E402
from eval.checks import allergen_verdict, call_matches  # noqa: E402

NOW = datetime(2026, 11, 18, 15, 0, tzinfo=ZoneInfo("Europe/London"))
FIRST_NEW_RESERVATION = 1028


class DownLLM:
    """Stands in for the LLM provider being unreachable."""

    def chat(self, *_args, **_kw):
        raise LLMError("simulated provider outage")


class Voice:
    """Caller voices (Piper) -> Whisper, and the agent's own Piper voice."""

    def __init__(self, settings: Settings):
        from bistrovoice.speech import CALLER_VOICES, Speaker, Transcriber, menu_hint

        self.stt = Transcriber(settings.stt_model, hint=menu_hint(build_database(":memory:")))
        self.agent_voice = Speaker(settings.tts_voice)
        self.callers = [Speaker(v) for v in CALLER_VOICES]
        # Asked to repeat, people slow down: same voices, 20% slower.
        self.slow_callers = [Speaker(v, length_scale=1.2) for v in CALLER_VOICES]
        self.stt.transcribe(np.zeros(16000, dtype=np.float32))  # warm-up

    def hear(self, text: str, caller: int, slow: bool = False):
        if text.startswith("("):  # "(background noise)": real noise, not words
            rng = np.random.default_rng(7)
            audio = (rng.normal(0, 0.05, 24000)).astype(np.float32)
        else:
            spk = (self.slow_callers if slow else self.callers)[caller % len(self.callers)]
            audio = spk.to_16k(spk.synthesize(text).wav)
        return self.stt.transcribe(audio)

    def speak(self, reply: str) -> tuple[float, float, float]:
        first = total = seconds = 0.0
        for i, sentence in enumerate(self.agent_voice.sentences(reply)):
            chunk = self.agent_voice.synthesize(sentence)
            total += chunk.ms
            seconds += chunk.seconds
            if i == 0:
                first = chunk.ms
        return first, total, seconds


def _needs_repeat(said: str, res) -> bool:
    if res.clarified:
        return True
    if len(re.sub(r"\D", "", said)) < 7:  # only lines where the caller gave a phone number
        return False
    errors = {c.result.get("error") for c in res.tool_calls}
    asked_again = "?" in res.reply and re.search(r"\b(phone|number)\b", res.reply, re.I)
    return bool(errors & {"invalid_phone", "details_not_given"}) or bool(asked_again)


def run_scenario(sc: dict, llm, voice: Voice | None, guards: bool, index: int) -> dict:
    conn = build_database(":memory:")
    ctx = ToolContext(conn=conn, rules=load_rules(conn), clock=lambda: NOW,
                      faults=set(sc.get("faults", [])) - {"llm"})
    agent = Agent(ctx, DownLLM() if "llm" in sc.get("faults", []) else llm, guards=guards)

    turns = []
    n, repeated = 0, False
    while n < len(sc["turns"]):
        turn = sc["turns"][n]
        text, confidence, stt_ms = turn["say"], turn.get("confidence"), None
        heard_by_stt = None
        if voice is not None:
            tr = voice.hear(turn["say"], index, slow=repeated)
            stt_ms, heard_by_stt = tr.ms, tr.text
            text, confidence = tr.text, tr.confidence
        if "heard" in turn:  # simulated mishearing overrides what Whisper produced
            text, confidence = turn["heard"], turn.get("confidence")

        res = agent.respond(text, confidence)
        record = {
            "turn": n + 1, "repeat": repeated, "say": turn["say"], "heard": text,
            "stt_text": heard_by_stt, "confidence": confidence, "reply": res.reply, "draft": res.draft,
            "tool_calls": [{"name": c.name, "args": c.args, "result": c.result, "ms": round(c.ms, 1),
                            "forced": c.forced} for c in res.tool_calls],
            "guard_events": res.guard_events, "clarified": res.clarified,
            "llm_ms": round(res.llm_ms, 1), "llm_calls": res.llm_calls, "tool_ms": round(res.tool_ms, 1),
            "prompt_tokens": res.prompt_tokens, "cached_tokens": res.cached_tokens,
            "completion_tokens": res.completion_tokens,
        }
        if voice is not None:
            first, total, seconds = voice.speak(res.reply)
            record.update(stt_ms=round(stt_ms, 1), tts_first_ms=round(first, 1),
                          tts_ms=round(total, 1), reply_seconds=round(seconds, 2))
            record["first_audio_ms"] = round(stt_ms + res.llm_ms + res.tool_ms + first, 1)
        turns.append(record)

        # A real caller who is asked to say something again does so. The script
        # can't know that in advance, so repeat the line once when the agent
        # couldn't use it (unclear audio, or a phone number it couldn't read).
        # Scripted mishearings ("heard") are left alone: the next line is the repeat.
        if not repeated and "heard" not in turn and _needs_repeat(turn["say"], res):
            repeated = True
            continue
        n, repeated = n + 1, False

    new = [dict(r) for r in conn.execute(
        "SELECT * FROM reservations WHERE reservation_id >= ?", (FIRST_NEW_RESERVATION,))]
    return {"id": sc["id"], "category": sc["category"], "title": sc["title"],
            "turns": turns, "new_reservations": new, "ctx": ctx}


def score(sc: dict, run: dict) -> dict:
    exp = sc["expect"]
    turns = run["turns"]
    replies = [t["reply"] for t in turns]
    drafts = [t["draft"] for t in turns]
    llm_calls = [c for t in turns for c in t["tool_calls"] if not c["forced"]]
    text = " ".join(replies).lower()
    checks: dict[str, bool] = {}

    # booking
    want = exp.get("booking", "none")
    if want == "none":
        checks["no_booking"] = not run["new_reservations"]
    else:
        rows = run["new_reservations"]
        ok = len(rows) == 1 and all(str(rows[0][k]) == str(v) for k, v in want.items() if k != "name")
        if ok and "name" in want:
            ok = want["name"].lower() in rows[0]["name"].lower()
        checks["booking_correct"] = ok
        if ok:
            checks["reference_given"] = str(rows[0]["reservation_id"]) in text

    # handoff
    handoff_called = any(c["name"] == "handoff_to_staff" for c in llm_calls)
    checks["handoff_decision"] = handoff_called == bool(exp.get("handoff", False))

    # allergen answers
    allergen = []
    for a in exp.get("allergen", []):
        final = allergen_verdict(replies, a["dish"], a.get("allergen", ""), a["truth"])
        draft = allergen_verdict([d for d in drafts if d], a["dish"], a.get("allergen", ""), a["truth"])
        allergen.append({**a, "final": final, "draft": draft})
        checks[f"allergen:{a['dish']}"] = final == "correct"

    # reply content
    if exp.get("reply_any"):
        checks["reply_any"] = any(s.lower() in text for s in exp["reply_any"])
    for s in exp.get("reply_all", []):
        checks[f"reply_has:{s}"] = s.lower() in text
    for s in exp.get("reply_none", []):
        checks[f"reply_lacks:{s}"] = s.lower() not in text

    # turns where nothing should happen (doubtful input)
    for n in exp.get("no_action_turns", []):
        acted = any(c["name"] == "handoff_to_staff" or
                    (c["name"] == "create_reservation" and c["result"].get("status") in
                     ("booked", "needs_confirmation"))
                    for t in turns if t["turn"] == n for c in t["tool_calls"])
        checks[f"no_action_turn_{n}"] = not acted

    # tool-calling accuracy (LLM-initiated calls only)
    def dish_of(name: str):
        row, _ = resolve_dish(run["ctx"], name)
        return row["dish"] if row else None

    tool_results = []
    for e in exp.get("tools", []):
        name_ok = args_ok = False
        for c in llm_calls:
            n_ok, a_ok = call_matches(e, c, dish_of)
            name_ok |= n_ok
            args_ok |= a_ok
        tool_results.append({"expected": e, "name_ok": name_ok, "args_ok": args_ok})

    unwanted = []
    if want == "none" and run["new_reservations"]:
        unwanted.append("booking")
    if handoff_called and not exp.get("handoff", False):
        unwanted.append("handoff")

    return {
        "id": sc["id"], "category": sc["category"], "title": sc["title"],
        "passed": all(checks.values()), "checks": checks, "allergen": allergen,
        "tools": tool_results, "unwanted_actions": unwanted,
        "handoff_expected": bool(exp.get("handoff", False)), "handoff_called": handoff_called,
        "turns": turns, "new_reservations": run["new_reservations"],
    }


def pct(values: list[float], q: float) -> float:
    return round(float(np.percentile(values, q)), 0) if values else 0.0


def summarise(results: list[dict], mode: str, model: str, guards: bool) -> dict:
    by_cat = defaultdict(list)
    for r in results:
        by_cat[r["category"]].append(r["passed"])
    tools = [t for r in results for t in r["tools"]]
    allergen = [a for r in results for a in r["allergen"]]
    turns = [t for r in results for t in r["turns"]]
    tp = sum(r["handoff_called"] and r["handoff_expected"] for r in results)
    fp = sum(r["handoff_called"] and not r["handoff_expected"] for r in results)
    fn = sum(not r["handoff_called"] and r["handoff_expected"] for r in results)
    guard = Counter(e.split(":")[0].split(" (")[0] for t in turns for e in t["guard_events"])

    latency = {}
    for key in ("stt_ms", "llm_ms", "tool_ms", "tts_first_ms", "first_audio_ms"):
        vals = [t[key] for t in turns if t.get(key) is not None and not (key == "llm_ms" and not t["llm_calls"])]
        if vals:
            latency[key] = {"p50": pct(vals, 50), "p95": pct(vals, 95), "n": len(vals)}

    return {
        "mode": mode, "model": model, "guards": guards, "scenarios": len(results),
        "turns": len(turns),
        "task_completion": round(sum(r["passed"] for r in results) / len(results), 3),
        "by_category": {k: f"{sum(v)}/{len(v)}" for k, v in by_cat.items()},
        "tool_selection": round(sum(t["name_ok"] for t in tools) / max(1, len(tools)), 3),
        "tool_arguments": round(sum(t["args_ok"] for t in tools) / max(1, len(tools)), 3),
        "expected_tool_calls": len(tools),
        "unwanted_actions": sum(len(r["unwanted_actions"]) for r in results),
        "handoff_accuracy": round(sum(r["handoff_called"] == r["handoff_expected"] for r in results)
                                  / len(results), 3),
        "handoff_precision": round(tp / max(1, tp + fp), 3),
        "handoff_recall": round(tp / max(1, tp + fn), 3),
        "allergen_checks": len(allergen),
        "allergen_correct": sum(a["final"] == "correct" for a in allergen),
        "made_up_allergen_answers": sum(a["final"] == "made_up" for a in allergen),
        "made_up_in_llm_drafts": sum(a["draft"] == "made_up" for a in allergen),
        "latency_ms": latency,
        "llm_calls_per_turn": round(statistics.mean(t["llm_calls"] for t in turns), 2),
        "caller_repeats": sum(t["repeat"] for t in turns),
        "prompt_tokens_total": sum(t["prompt_tokens"] for t in turns),
        "cached_tokens_total": sum(t["cached_tokens"] for t in turns),
        "completion_tokens_total": sum(t["completion_tokens"] for t in turns),
        "guard_events": dict(guard),
        "failed": [r["id"] for r in results if not r["passed"]],
    }


def write_markdown(summary: dict, results: list[dict], path: Path) -> None:
    s = summary
    lines = [f"# Evaluation results ({s['mode']} mode, guards {'on' if s['guards'] else 'off'})", "",
             f"Model: `{s['model']}` - {s['scenarios']} scenarios, {s['turns']} customer turns", "",
             "| Metric | Result |", "|---|---|",
             f"| Task completion | {s['task_completion']:.0%} |",
             f"| Tool selection (right tool) | {s['tool_selection']:.0%} of {s['expected_tool_calls']} expected calls |",
             f"| Tool arguments (right tool + args) | {s['tool_arguments']:.0%} |",
             f"| Unwanted actions (booking / handoff) | {s['unwanted_actions']} |",
             f"| Handoff accuracy | {s['handoff_accuracy']:.0%} (precision {s['handoff_precision']:.0%}, recall {s['handoff_recall']:.0%}) |",
             f"| Allergen answers correct | {s['allergen_correct']}/{s['allergen_checks']} |",
             f"| **Made-up allergen answers reaching the caller** | **{s['made_up_allergen_answers']}** |",
             f"| Made-up allergen answers in raw LLM drafts (caught by guard) | {s['made_up_in_llm_drafts']} |",
             "", "## By category", "", "| Category | Passed |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in s["by_category"].items()]
    if s["latency_ms"]:
        lines += ["", "## Latency per customer turn (ms)", "", "| Stage | p50 | p95 |", "|---|---|---|"]
        names = {"stt_ms": "Speech-to-text", "llm_ms": "LLM (all calls in the turn)", "tool_ms": "Tools",
                 "tts_first_ms": "Text-to-speech, first sentence", "first_audio_ms": "End of speech to first audio"}
        lines += [f"| {names[k]} | {v['p50']:.0f} | {v['p95']:.0f} |" for k, v in s["latency_ms"].items()]
    lines += ["", "## Scenarios", "", "| ID | Category | Scenario | Result | Failed checks |", "|---|---|---|---|---|"]
    for r in results:
        failed = ", ".join(k for k, v in r["checks"].items() if not v) or ""
        lines.append(f"| {r['id']} | {r['category']} | {r['title']} | {'pass' if r['passed'] else 'FAIL'} | {failed} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["voice", "text"], default="voice")
    ap.add_argument("--only", default="")
    ap.add_argument("--category", default="")
    ap.add_argument("--no-guards", action="store_true")
    ap.add_argument("--tag", default="")
    ap.add_argument("--model", default="")
    args = ap.parse_args(argv)

    settings = Settings.from_env()
    if args.model:
        settings = Settings(**{**settings.__dict__, "llm_model": args.model})
    scenarios = yaml.safe_load((ROOT / "eval" / "scenarios.yaml").read_text(encoding="utf-8"))
    if args.only:
        wanted = set(args.only.split(","))
        scenarios = [s for s in scenarios if s["id"] in wanted]
    if args.category:
        scenarios = [s for s in scenarios if args.category in s["category"]]

    llm = LLMClient(settings, cache_path=ROOT / "runs" / "llm_cache.sqlite", max_wait_s=90)
    voice = Voice(settings) if args.mode == "voice" else None
    guards = not args.no_guards
    tag = args.tag or f"{args.mode}{'' if guards else '-no-guards'}"
    out_dir = ROOT / "eval" / "results" / tag
    out_dir.mkdir(parents=True, exist_ok=True)

    results = []
    live = (ROOT / "runs" / f"{tag}.jsonl").open("w", encoding="utf-8")  # inspect while it runs
    for i, sc in enumerate(scenarios):
        run = run_scenario(sc, llm, voice, guards, i)
        if llm.quota_exhausted:
            # Don't score a call where the "LLM down" reply was really our quota.
            print(f"Daily LLM quota used up at {sc['id']}: stopping. Rerun later; "
                  "finished calls are cached and cost nothing.")
            break
        r = score(sc, run)
        results.append(r)
        live.write(json.dumps(r, ensure_ascii=False) + "\n")
        live.flush()
        mark = "pass" if r["passed"] else "FAIL " + ", ".join(k for k, v in r["checks"].items() if not v)
        print(f"[{i + 1:2}/{len(scenarios)}] {sc['id']} {sc['title'][:38]:38} {mark}", flush=True)

    if not results:
        return
    summary = summarise(results, args.mode, settings.llm_model, guards)
    summary["complete"] = len(results) == len(scenarios)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    with (out_dir / "scenarios.jsonl").open("w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    write_markdown(summary, results, out_dir / "results.md")
    print(json.dumps({k: v for k, v in summary.items() if k != "latency_ms"}, indent=2))
    print(json.dumps(summary["latency_ms"], indent=2))


if __name__ == "__main__":
    main()
