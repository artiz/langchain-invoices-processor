"""invoice-agent CLI.

auth    one-time Google OAuth (stores refresh token in .data/)
run     daemon: poll Gmail via MCP, notify + resume via Telegram
check   one Gmail scan, then exit (pending reviews are resumed later by `run`)
demo    process a local email JSON file, human-in-the-loop in the terminal
graph   print the graph as Mermaid
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from .app import InvoiceApp, make_llm, open_app
from .config import load_settings
from .google_auth import run_auth_flow
from .graph import PAYEES_NS, AppContext, build_graph, email_block
from .mail_parser import parse_raw_email
from .samples import load_case
from .telegram import format_review, strip_html


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs full URLs at INFO, which would leak the Telegram bot token.
    for name in ("httpx", "httpcore", "mcp"):
        logging.getLogger(name).setLevel(logging.WARNING)


def cmd_auth(settings, _args) -> None:
    if not settings.gmail_enabled:
        sys.exit("Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET first.")
    run_auth_flow(
        settings.google_client_id, settings.google_client_secret, settings.google_token_file, settings.google_oauth_port
    )


async def cmd_run(settings, _args) -> None:
    async with open_app(settings) as app:
        if not app.gmail:
            logging.warning("Gmail disabled: GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET not set")
        if not app.bot:
            logging.warning("Telegram disabled: TELEGRAM_BOT_TOKEN not set")
        await app.run_forever()


async def cmd_check(settings, _args) -> None:
    async with open_app(settings) as app:
        count = await app.poll_gmail()
        pending = await app.pending()
        print(f"Processed {count} new email(s); {len(pending)} invoice(s) waiting for review.")


async def cmd_demo(settings, args) -> None:
    email, history_seed, _ = load_case(args.file)
    store = InMemoryStore()
    for domain, history in history_seed.items():
        await store.aput(PAYEES_NS, domain, history)
    context = AppContext(llm=make_llm(settings), timezone=settings.timezone, remind_after_hours=0)
    app = InvoiceApp(settings, build_graph(InMemorySaver(), store), store, context)

    snap = await app.process_email(email)
    while snap.interrupts:
        print("\n" + strip_html(format_review(snap.interrupts[0].value)) + "\n")
        answer = ""
        while answer not in ("p", "r", "i"):
            answer = input("[p]aid / [r]emind me / [i]gnore > ").strip().lower()[:1]
        snap = await app.resume(email.id, {"p": "paid", "r": "remind", "i": "ignored"}[answer])

    print("\nTrace:")
    for line in snap.values.get("trace", []):
        print(f"  {line}")
    print(f"Status: {snap.values.get('status')}")
    for item in await store.asearch(PAYEES_NS):
        print(f"Payee memory [{item.key}]: {item.value}")


async def cmd_parse(settings, args) -> None:
    """Show exactly what the LLM receives for a Gmail message id or a local .eml file."""
    if Path(args.source).exists():
        raw = Path(args.source).read_bytes()
        email = parse_raw_email(Path(args.source).stem, raw, settings.owner_emails)
    else:
        async with open_app(settings) as app:
            if not app.gmail:
                sys.exit("Gmail not configured (GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET).")
            raw = await app.gmail.get_raw(args.source)
            email = parse_raw_email(args.source, raw, settings.owner_emails)
    block = email_block(email)
    print(block)
    print(f"\nraw: {len(raw):,} bytes -> prompt: {len(block):,} chars (~{len(block) // 4:,} tokens)", file=sys.stderr)
    for att in email.attachments:
        print(f"  attachment {att.name} ({att.content_type}, {att.size:,} bytes) {att.note}", file=sys.stderr)


def cmd_graph(_settings, _args) -> None:
    print(build_graph().get_graph().draw_mermaid())


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="invoice-agent", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("auth", help="authorize Gmail read access")
    sub.add_parser("run", help="run the monitoring daemon")
    sub.add_parser("check", help="scan Gmail once")
    demo = sub.add_parser("demo", help="process a local email (JSON or .eml) with terminal HITL")
    demo.add_argument("file")
    parse = sub.add_parser("parse", help="show the parsed email (Gmail id or .eml) as the LLM sees it")
    parse.add_argument("source")
    sub.add_parser("graph", help="print Mermaid diagram")
    args = parser.parse_args()

    _setup_logging(args.verbose)
    settings = load_settings()
    handler = {
        "auth": cmd_auth,
        "run": cmd_run,
        "check": cmd_check,
        "demo": cmd_demo,
        "parse": cmd_parse,
        "graph": cmd_graph,
    }[args.command]
    result = handler(settings, args)
    if asyncio.iscoroutine(result):
        try:
            asyncio.run(result)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
