"""Offline evaluation on synthetic emails in examples/emails/*.json (inline bodies or .eml with attachments).

Runs the real graph (real LLM, in-memory checkpointer/store, no Gmail) until it
finishes or stops at the human-review interrupt, then compares triage, verdict,
notification and extracted fields with each case's `expected` block.

    uv run python evals/run.py [-k magenta]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from invoice_agent import iban as iban_utils
from invoice_agent.app import make_llm
from invoice_agent.config import load_settings
from invoice_agent.graph import PAYEES_NS, AppContext, build_graph
from invoice_agent.samples import load_case

CASES_DIR = Path(__file__).parent.parent / "examples" / "emails"


async def run_case(path: Path, settings, sem: asyncio.Semaphore) -> dict:
    email, history_seed, expected = load_case(path)
    store = InMemoryStore()
    for domain, history in history_seed.items():
        await store.aput(PAYEES_NS, domain, history)
    clock = lambda: datetime(2026, 10, 8, 12, 0, tzinfo=settings.timezone)  # noqa: E731
    context = AppContext(llm=make_llm(settings), timezone=settings.timezone, clock=clock)
    graph = build_graph(InMemorySaver(), store)
    config = {"configurable": {"thread_id": email.id}, "recursion_limit": 30}
    async with sem:
        await graph.ainvoke({"email": email.model_dump(), "trace": []}, config, context=context)
    snap = await graph.aget_state(config)
    values = snap.values
    actual = {
        "is_payment_request": values.get("triage", {}).get("is_payment_request"),
        "verdict": values.get("validation", {}).get("verdict"),
        "notify": bool(snap.interrupts),
        "amount": (values.get("invoice") or {}).get("amount"),
        "iban": iban_utils.normalize((values.get("invoice") or {}).get("iban") or "") or None,
    }
    failures = []
    for key, want in expected.items():
        got = actual[key]
        ok = abs((got or 0) - want) < 0.01 if key == "amount" else got == want
        if not ok:
            failures.append(f"{key}: expected {want!r}, got {got!r}")
    return {
        "case": path.stem,
        "actual": actual,
        "risk": (values.get("investigation") or {}).get("risk"),
        "route": [t for t in values.get("trace", []) if t.startswith("supervisor")],
        "failures": failures,
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("-k", help="only cases whose name contains this string")
    args = parser.parse_args()
    settings = load_settings()
    paths = sorted(p for p in CASES_DIR.glob("*.json") if not args.k or args.k in p.stem)
    sem = asyncio.Semaphore(4)
    results = await asyncio.gather(*(run_case(p, settings, sem) for p in paths))

    print(f"{'case':24} {'payreq':7} {'verdict':13} {'notify':7} {'risk':7} result")
    for r in results:
        a = r["actual"]
        status = "PASS" if not r["failures"] else "FAIL: " + "; ".join(r["failures"])
        columns = f"{a['is_payment_request']!s:7} {a['verdict']!s:13} {a['notify']!s:7} {r['risk']!s:7}"
        print(f"{r['case']:24} {columns} {status}")
        for step in r["route"]:
            print(f"{'':26}{step}")
    passed = sum(not r["failures"] for r in results)
    print(f"\n{passed}/{len(results)} passed (model: {settings.openai_model})")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
