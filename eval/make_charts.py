"""Charts for the README, drawn from eval/results/*/summary.json.

Each chart is saved twice (light and dark) so the README can pick the one
that matches the reader's GitHub theme.

    python eval/make_charts.py
"""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "eval" / "results"
OUT = ROOT / "docs" / "img"

THEMES = {
    "light": {"bg": "#fcfcfb", "text": "#0b0b0b", "muted": "#52514e", "grid": "#e4e2dc",
              "s1": "#2a78d6", "s1_soft": "#b9d3f2", "s2": "#eb6834"},
    "dark": {"bg": "#1a1a19", "text": "#f0efec", "muted": "#c3c2b7", "grid": "#33322f",
             "s1": "#3987e5", "s1_soft": "#24466e", "s2": "#d95926"},
}


def _style(ax, t):
    ax.set_facecolor(t["bg"])
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(t["grid"])
    ax.tick_params(colors=t["muted"], length=0, labelsize=10)
    ax.xaxis.grid(True, color=t["grid"], linewidth=0.8)
    ax.set_axisbelow(True)


def _save(fig, name, theme):
    OUT.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT / f"{name}-{theme}.png", dpi=180, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)


def latency(summary: dict) -> None:
    names = {"stt_ms": "Speech-to-text", "llm_ms": "LLM (per turn)", "tool_ms": "Tools (SQL)",
             "tts_first_ms": "Text-to-speech\n(first sentence)",
             "first_audio_ms": "End of speech\nto first audio"}
    keys = [k for k in names if k in summary["latency_ms"]]
    p50 = [summary["latency_ms"][k]["p50"] for k in keys]
    p95 = [summary["latency_ms"][k]["p95"] for k in keys]
    for theme, t in THEMES.items():
        fig, ax = plt.subplots(figsize=(8, 3.6), facecolor=t["bg"])
        _style(ax, t)
        y = range(len(keys))[::-1]
        ax.barh(list(y), p95, height=0.56, color=t["s1_soft"], label="p95")
        ax.barh(list(y), p50, height=0.56, color=t["s1"], label="p50")
        for yi, a, b in zip(y, p50, p95):
            ax.text(b + max(p95) * 0.015, yi, f"p50 {a:,.0f} ms  ·  p95 {b:,.0f} ms",
                    va="center", color=t["text"], fontsize=9.5)
        ax.set_yticks(list(y), [names[k] for k in keys], color=t["text"])
        ax.set_xlim(0, max(p95) * 1.55)
        ax.set_xlabel("milliseconds", color=t["muted"])
        ax.legend(frameon=False, labelcolor=t["text"], loc="lower right", ncols=2)
        ax.set_title("Latency per customer turn (local CPU, voice mode)", loc="left",
                     color=t["text"], fontsize=12, pad=12)
        _save(fig, "latency", theme)


def categories(summary: dict) -> None:
    rows = list(summary["by_category"].items())
    labels = [k for k, _ in rows]
    passed = [int(v.split("/")[0]) for _, v in rows]
    total = [int(v.split("/")[1]) for _, v in rows]
    share = [p / n * 100 for p, n in zip(passed, total)]
    for theme, t in THEMES.items():
        fig, ax = plt.subplots(figsize=(8, 3.6), facecolor=t["bg"])
        _style(ax, t)
        y = list(range(len(labels)))[::-1]
        ax.barh(y, share, height=0.56, color=t["s1"])
        for yi, s, p, n in zip(y, share, passed, total):
            ax.text(s + 1.5, yi, f"{p}/{n}", va="center", color=t["text"], fontsize=9.5)
        ax.set_yticks(y, labels, color=t["text"])
        ax.set_xlim(0, 112)
        ax.set_xticks([0, 25, 50, 75, 100], ["0%", "25%", "50%", "75%", "100%"])
        ax.set_title("Task completion by scenario type", loc="left", color=t["text"],
                     fontsize=12, pad=12)
        _save(fig, "completion", theme)


def guards(on: dict, off: dict) -> None:
    metrics = [
        ("Task completion", on["task_completion"] * 100, off["task_completion"] * 100, "%"),
        ("Handoff accuracy", on["handoff_accuracy"] * 100, off["handoff_accuracy"] * 100, "%"),
    ]
    counts = [
        ("Made-up allergen answers\nheard by the caller", on["made_up_allergen_answers"],
         off["made_up_allergen_answers"]),
        ("Unwanted actions\n(bookings / handoffs)", on["unwanted_actions"], off["unwanted_actions"]),
    ]
    for theme, t in THEMES.items():
        fig, axes = plt.subplots(1, 2, figsize=(9, 3.4), facecolor=t["bg"])
        for ax, data, unit in ((axes[0], metrics, "%"), (axes[1], counts, "")):
            _style(ax, t)
            y = list(range(len(data)))[::-1]
            h = 0.36
            ax.barh([v + h / 2 for v in y], [d[2] for d in data], height=h, color=t["s2"],
                    label="code guardrails off")
            ax.barh([v - h / 2 for v in y], [d[1] for d in data], height=h, color=t["s1"],
                    label="code guardrails on")
            top = max(max(d[1], d[2]) for d in data) or 1
            for yi, d in zip(y, data):
                ax.text(d[2] + top * 0.02, yi + h / 2, f"{d[2]:.0f}{unit}", va="center",
                        color=t["text"], fontsize=9)
                ax.text(d[1] + top * 0.02, yi - h / 2, f"{d[1]:.0f}{unit}", va="center",
                        color=t["text"], fontsize=9)
            ax.set_yticks(y, [d[0] for d in data], color=t["text"])
            ax.set_xlim(0, top * 1.25)
        axes[1].legend(frameon=False, labelcolor=t["text"], loc="lower right", fontsize=9)
        fig.suptitle("Same model, same calls: with and without the code guardrails", x=0.02,
                     ha="left", color=t["text"], fontsize=12)
        fig.tight_layout()
        _save(fig, "guardrails", theme)


def main() -> None:
    voice = json.loads((RESULTS / "voice" / "summary.json").read_text())
    latency(voice)
    categories(voice)
    off_path = RESULTS / "text-no-guards" / "summary.json"
    on_path = RESULTS / "text" / "summary.json"
    if off_path.exists() and on_path.exists():
        guards(json.loads(on_path.read_text()), json.loads(off_path.read_text()))
    print(f"charts written to {OUT}")


if __name__ == "__main__":
    main()
