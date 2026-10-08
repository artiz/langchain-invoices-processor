"""Graph flow with a fake LLM: interrupt -> resume -> long-term memory -> duplicate detection."""

from datetime import datetime
from zoneinfo import ZoneInfo

from langchain_core.runnables import RunnableLambda
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command

from invoice_agent.graph import PAYEES_NS, AppContext, allowed_next, build_graph
from invoice_agent.models import Invoice, Route, Triage

TZ = ZoneInfo("Europe/Vienna")


class FakeLLM:
    """Returns canned structured outputs per schema; records supervisor prompts."""

    def __init__(self, invoice: Invoice, route: str = "human_review"):
        self.outputs = {
            Triage: Triage(is_payment_request=True, category="invoice", reason="invoice"),
            Invoice: invoice,
            Route: Route(next=route, reason="test"),
        }

    def with_structured_output(self, schema, **_):
        return RunnableLambda(lambda _: self.outputs[schema])


def make_invoice(reference: str) -> Invoice:
    return Invoice(
        vendor_name="Magenta",
        payee_name="T-Mobile Austria GmbH",
        amount=50.05,
        currency="EUR",
        invoice_date="2026-10-05",
        due_date="2026-10-17",
        iban="AT82 1200 0528 4407 2305",
        bic="BKAUATWW",
        payment_reference=reference,
        invoice_number=None,
        customer_number=None,
        payment_method="bank_transfer",
    )


def email(email_id: str) -> dict:
    return {"id": email_id, "sender": "rechnung@magenta.at", "to": "", "date": "", "subject": "Rechnung", "body": "..."}


async def run(graph, llm, email_id, **kwargs):
    context = AppContext(llm=llm, clock=lambda: datetime(2026, 10, 8, 12, tzinfo=TZ), **kwargs)
    config = {"configurable": {"thread_id": email_id}}
    await graph.ainvoke({"email": email(email_id), "trace": []}, config, context=context)
    return config, context


async def test_hitl_flow_learns_payee():
    store = InMemoryStore()
    graph = build_graph(InMemorySaver(), store)
    llm = FakeLLM(make_invoice("REF-1"))

    config, context = await run(graph, llm, "m1")
    snap = await graph.aget_state(config)
    assert snap.next == ("human_review",)
    payload = snap.interrupts[0].value
    assert payload["validation"]["verdict"] == "needs_review"  # first invoice from magenta.at

    # "Remind me" loops back to the same interrupt with a notify_after timestamp.
    await graph.ainvoke(Command(resume={"action": "remind"}), config, context=context)
    snap = await graph.aget_state(config)
    assert snap.interrupts[0].value["notify_after"].startswith("2026-10-09T12:00")
    assert snap.interrupts[0].value["remind_count"] == 1

    await graph.ainvoke(Command(resume={"action": "paid"}), config, context=context)
    snap = await graph.aget_state(config)
    assert not snap.next and snap.values["status"] == "paid"
    memory = (await store.aget(PAYEES_NS, "magenta.at")).value
    assert memory["ibans"] == ["AT821200052844072305"]
    assert memory["paid_references"] == ["REF-1"]

    # Next month: known IBAN -> payable.
    config2, _ = await run(graph, FakeLLM(make_invoice("REF-2")), "m2")
    assert (await graph.aget_state(config2)).interrupts[0].value["validation"]["verdict"] == "payable"

    # Same reference again -> already paid, no human needed.
    config3, _ = await run(graph, FakeLLM(make_invoice("REF-1")), "m3")
    snap3 = await graph.aget_state(config3)
    assert not snap3.interrupts and snap3.values["status"] == "not_required"


async def test_supervisor_cannot_skip_human():
    """Even if the router LLM says 'record', the guardrail forces human_review."""
    graph = build_graph(InMemorySaver(), InMemoryStore())
    config, _ = await run(graph, FakeLLM(make_invoice("REF-9"), route="record"), "m9")
    snap = await graph.aget_state(config)
    assert snap.next == ("human_review",)
    assert "supervisor -> human_review (fallback)" in snap.values["trace"]


def test_allowed_next():
    assert allowed_next({"email": {}}) == ["triage"]
    assert allowed_next({"triage": {"is_payment_request": False}}) == ["finish"]
    base = {"triage": {"is_payment_request": True}, "invoice": {}, "validation": {"verdict": "payable"}}
    assert allowed_next(base) == ["investigate", "human_review"]
    assert allowed_next({**base, "investigation": {}}) == ["human_review"]
    assert allowed_next({**base, "decision": "paid"}) == ["record"]
    assert allowed_next({**base, "decision": "paid", "recorded": True}) == ["finish"]
