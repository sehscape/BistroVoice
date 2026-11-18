"""Command line: `bistrovoice init-db | chat | serve | download-models | eval`."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .config import Settings


def open_restaurant(settings: Settings, *, rebuild: bool = False):
    """Connection + ToolContext on the real clock. Builds the DB if needed."""
    from .db import build_database, connect, load_rules
    from .tools import ToolContext

    if rebuild or not settings.db_path.exists():
        conn = build_database(settings.db_path, align_to=date.today())
    else:
        conn = connect(settings.db_path)
    rules = load_rules(conn)
    tz = ZoneInfo(rules.timezone)
    return ToolContext(conn=conn, rules=rules, clock=lambda: datetime.now(tz))


def cmd_init_db(args, settings: Settings) -> None:
    ctx = open_restaurant(settings, rebuild=True)
    counts = {t: ctx.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("menu", "ingredients", "allergens", "tables", "reservations")}
    print(f"Built {settings.db_path}")
    print("  " + ", ".join(f"{k}: {v}" for k, v in counts.items()))


def cmd_chat(args, settings: Settings) -> None:
    """Text mode: the same agent as the voice app, typed instead of spoken."""
    from .agent import Agent
    from .llm import LLMClient

    ctx = open_restaurant(settings, rebuild=args.fresh)
    agent = Agent(ctx, LLMClient(settings))
    print(f"{ctx.rules.name} - text mode ({settings.llm_model}). Ctrl+C to quit.\n")
    while True:
        try:
            text = input("you   > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not text:
            continue
        turn = agent.respond(text)
        if args.verbose:
            for c in turn.tool_calls:
                tag = "code" if c.forced else "llm"
                print(f"  [{tag}] {c.name}({json.dumps(c.args)}) -> "
                      f"{json.dumps(c.result, ensure_ascii=False)[:200]}  {c.ms:.0f} ms")
            for e in turn.guard_events:
                print(f"  [guard] {e}")
            print(f"  [llm] {turn.llm_calls} call(s), {turn.llm_ms:.0f} ms")
        print(f"agent > {turn.reply}\n")


def cmd_serve(args, settings: Settings) -> None:
    import uvicorn

    uvicorn.run("bistrovoice.server:app", host=args.host, port=args.port, log_level="info")


def cmd_download(args, settings: Settings) -> None:
    from .speech import download_models

    download_models(settings, agent_voice_only=args.agent_voice_only)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="bistrovoice")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db", help="rebuild the SQLite database from data/*.csv")
    chat = sub.add_parser("chat", help="talk to the agent in text mode")
    chat.add_argument("-v", "--verbose", action="store_true", help="show tool calls and guard events")
    chat.add_argument("--fresh", action="store_true", help="rebuild the database first")
    serve = sub.add_parser("serve", help="run the voice web app")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    dl = sub.add_parser("download-models", help="download the Whisper and Piper models")
    dl.add_argument("--agent-voice-only", action="store_true",
                    help="only the agent's Piper voice (hosted image)")

    args = parser.parse_args(argv)
    settings = Settings.from_env()
    {"init-db": cmd_init_db, "chat": cmd_chat, "serve": cmd_serve,
     "download-models": cmd_download}[args.command](args, settings)


if __name__ == "__main__":
    sys.exit(main())
