"""Supervisor / worker graph for one email (one LangGraph thread per Gmail message).

    START -> supervisor -> {triage | extract | validate | investigate | human_review | record} -> supervisor ... -> END

* The supervisor is an LLM router, but guarded: code computes the ALLOWED next
  workers from the state and the LLM may only choose among them. With a single
  option the LLM is skipped entirely.
* ``human_review`` calls ``interrupt()``. The graph state is persisted by the
  checkpointer, the process may exit, and the thread is resumed later with
  ``Command(resume={"action": ...})`` when the user taps a Telegram button.
* Payee history (IBANs the user actually paid) lives in the LangGraph Store,
  i.e. long-term memory shared across threads.
"""

from __future__ import annotations

import operator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Annotated, Any, Callable, Literal, TypedDict
from zoneinfo import ZoneInfo

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.store.base import BaseStore
from langgraph.types import Command, interrupt

from . import iban as iban_utils
from .checks import run_checks, sender_domain
from .gmail import GmailMCP
from .models import EmailMessage, Investigation, Invoice, PayeeHistory, Route, Triage
from .prompts import EXTRACT_SYSTEM, INVESTIGATOR_SYSTEM, SUPERVISOR_SYSTEM, TRIAGE_SYSTEM

PAYEES_NS = ("payees",)


class InvoiceState(TypedDict, total=False):
    email: dict
    triage: dict
    invoice: dict
    validation: dict
    investigation: dict
    decision: Literal["paid", "ignored"]
    remind_count: int
    notify_after: str | None
    recorded: bool
    status: str
    trace: Annotated[list[str], operator.add]


@dataclass
class AppContext:
    """Per-run dependencies (not checkpointed)."""

    llm: BaseChatModel
    gmail: GmailMCP | None = None
    timezone: ZoneInfo = field(default_factory=lambda: ZoneInfo("Europe/Vienna"))
    remind_after_hours: float = 24
    clock: Callable[[], datetime] | None = None

    def now(self) -> datetime:
        return self.clock() if self.clock else datetime.now(self.timezone)


# ---------------------------------------------------------------- helpers


def email_block(email: EmailMessage, limit: int = 12_000) -> str:
    """Email (+ parsed attachments) as one untrusted block, capped at ``limit`` characters."""
    head = [f"From: {email.sender}", f"Date: {email.date}", f"Subject: {email.subject}"]
    if email.forwarded_by:
        head.append(f"Forwarded by the user ({email.forwarded_by})")
    if email.attachments:
        head.append("Attachments: " + ", ".join(f"{a.name} ({a.content_type})" for a in email.attachments))
    parts = ["\n".join(head), email.body]
    for att in email.attachments:
        if att.text:
            parts.append(f"--- Attachment: {att.name}{f' [{att.note}]' if att.note else ''} ---\n{att.text}")
    text = "\n\n".join(parts)[:limit].replace("</email>", "</ email>")
    return f"<email>\n{text}\n</email>"


async def load_history(store: BaseStore, domain: str) -> PayeeHistory:
    item = await store.aget(PAYEES_NS, domain)
    return PayeeHistory(**item.value) if item else PayeeHistory()


def allowed_next(state: InvoiceState) -> list[str]:
    """Guardrail: which workers may run next given the current state."""
    if "triage" not in state:
        return ["triage"]
    if not state["triage"]["is_payment_request"]:
        return ["finish"]
    if "invoice" not in state:
        return ["extract"]
    if "validation" not in state:
        return ["validate"]
    if state.get("decision"):
        return ["finish"] if state.get("recorded") else ["record"]
    if state["validation"]["verdict"] == "not_required":
        return ["finish"]
    if "investigation" in state or state.get("remind_count"):
        return ["human_review"]
    return ["investigate", "human_review"]


def state_summary(state: InvoiceState) -> str:
    email = state["email"]
    lines = [f"Email: id={email['id']} from={email['sender']!r} subject={email['subject']!r}"]
    if email.get("forwarded_by"):
        lines.append(
            f"Forwarded to the mailbox by the user ({email['forwarded_by']}); judge the original sender above."
        )
    if t := state.get("triage"):
        lines.append(f"Triage: payment_request={t['is_payment_request']} category={t['category']} ({t['reason']})")
    if inv := state.get("invoice"):
        lines.append(
            f"Invoice: payee={inv['payee_name']} amount={inv['amount']} {inv['currency']} "
            f"due={inv['due_date']} iban={inv['iban']} method={inv['payment_method']}"
        )
    if v := state.get("validation"):
        problems = [f"{c['name']}={c['status']}: {c['detail']}" for c in v["checks"] if c["status"] != "pass"]
        lines.append(f"Validation: verdict={v['verdict']} problems={problems or 'none'}")
    if i := state.get("investigation"):
        lines.append(f"Investigation: risk={i['risk']} findings={i['findings']}")
    return "\n".join(lines)


def review_payload(state: InvoiceState) -> dict[str, Any]:
    """What the human sees (sent to Telegram by the app layer)."""
    return {
        "email": {k: state["email"].get(k) for k in ("id", "sender", "subject", "date", "forwarded_by")},
        "attachments": [a["name"] for a in state["email"].get("attachments", [])],
        "invoice": state["invoice"],
        "validation": state["validation"],
        "investigation": state.get("investigation"),
        "remind_count": state.get("remind_count", 0),
        "notify_after": state.get("notify_after"),
    }


# ---------------------------------------------------------------- supervisor


async def supervisor(
    state: InvoiceState, runtime: Runtime[AppContext]
) -> Command[Literal["triage", "extract", "validate", "investigate", "human_review", "record", "__end__"]]:
    allowed = allowed_next(state)
    if len(allowed) == 1:
        choice, reason = allowed[0], "deterministic"
    else:
        router = runtime.context.llm.with_structured_output(Route, strict=True)
        route: Route = await router.ainvoke(
            [
                SystemMessage(SUPERVISOR_SYSTEM),
                HumanMessage(f"{state_summary(state)}\n\nALLOWED next workers: {allowed}"),
            ]
        )
        # Fall back to the safest option (a human) if the model picks something not allowed.
        choice, reason = (route.next, route.reason) if route.next in allowed else (allowed[-1], "fallback")
    return Command(
        goto=END if choice == "finish" else choice,
        update={"trace": [f"supervisor -> {choice} ({reason})"]},
    )


# ---------------------------------------------------------------- workers


async def triage(state: InvoiceState, runtime: Runtime[AppContext]) -> dict:
    email = EmailMessage(**state["email"])
    llm = runtime.context.llm.with_structured_output(Triage, strict=True)
    result: Triage = await llm.ainvoke([SystemMessage(TRIAGE_SYSTEM), HumanMessage(email_block(email, 5000))])
    update: dict = {"triage": result.model_dump(), "trace": [f"triage: {result.category}"]}
    if not result.is_payment_request:
        update["status"] = "not_payment_request"
    return update


async def extract(state: InvoiceState, runtime: Runtime[AppContext]) -> dict:
    email = EmailMessage(**state["email"])
    llm = runtime.context.llm.with_structured_output(Invoice, strict=True)
    invoice: Invoice = await llm.ainvoke([SystemMessage(EXTRACT_SYSTEM), HumanMessage(email_block(email))])
    return {"invoice": invoice.model_dump(), "trace": [f"extract: {invoice.payee_name} {invoice.amount}"]}


async def validate(state: InvoiceState, runtime: Runtime[AppContext]) -> dict:
    email = EmailMessage(**state["email"])
    invoice = Invoice(**state["invoice"])
    history = await load_history(runtime.store, sender_domain(email.sender))
    validation = run_checks(email, invoice, history, runtime.context.now().date())
    update: dict = {"validation": validation.model_dump(), "trace": [f"validate: {validation.verdict}"]}
    if validation.verdict == "not_required":
        update["status"] = "not_required"
    return update


def investigator_tools(gmail: GmailMCP | None, store: BaseStore, domain: str, current_id: str) -> list:
    @tool
    async def payee_history() -> str:
        """Stored history for this sender: IBANs, payee names, amounts and references the user confirmed as paid."""
        history = await load_history(store, domain)
        return history.model_dump_json() if history.ibans else "No confirmed payments for this sender yet."

    tools = [payee_history]
    if gmail is None:
        return tools

    @tool
    async def search_mailbox(query: str) -> str:
        """Search the mailbox with Gmail search syntax (e.g. 'from:billing@example.com'). Returns up to 10 headers."""
        headers = [h for h in await gmail.search(query, max_results=10) if h.id != current_id]
        if not headers:
            return "No other emails found."
        return "\n".join(f"id={h.id} | {h.date} | {h.sender} | {h.subject}" for h in headers)

    @tool
    async def read_email(email_id: str) -> str:
        """Read an email by id (plain text, truncated)."""
        return email_block(await gmail.get_email(email_id), limit=4000)

    return [payee_history, search_mailbox, read_email]


async def investigate(state: InvoiceState, runtime: Runtime[AppContext]) -> dict:
    email = EmailMessage(**state["email"])
    domain = sender_domain(email.sender)
    agent = create_agent(
        runtime.context.llm,
        tools=investigator_tools(runtime.context.gmail, runtime.store, domain, email.id),
        system_prompt=INVESTIGATOR_SYSTEM,
        response_format=Investigation,
        middleware=[ModelCallLimitMiddleware(run_limit=8), ToolCallLimitMiddleware(run_limit=6)],
        name="fraud_investigator",
    )
    result = await agent.ainvoke({"messages": [HumanMessage(f"Investigate this invoice.\n\n{state_summary(state)}")]})
    investigation = result.get("structured_response") or Investigation(
        risk="medium", previous_invoices_found=0, iban_seen_before=None, findings=["Investigation incomplete."]
    )
    return {"investigation": investigation.model_dump(), "trace": [f"investigate: risk={investigation.risk}"]}


async def human_review(state: InvoiceState, runtime: Runtime[AppContext]) -> dict:
    # Everything before interrupt() re-runs on resume, so keep it side-effect free.
    answer = interrupt(review_payload(state))
    action = answer.get("action") if isinstance(answer, dict) else str(answer)
    if action in ("paid", "ignored"):
        return {"decision": action, "notify_after": None, "trace": [f"human_review: {action}"]}
    notify_after = runtime.context.now() + timedelta(hours=runtime.context.remind_after_hours)
    return {
        "remind_count": state.get("remind_count", 0) + 1,
        "notify_after": notify_after.isoformat(timespec="minutes"),
        "trace": [f"human_review: remind at {notify_after:%Y-%m-%d %H:%M}"],
    }


async def record(state: InvoiceState, runtime: Runtime[AppContext]) -> dict:
    decision = state["decision"]
    if decision == "paid":
        email = EmailMessage(**state["email"])
        invoice = Invoice(**state["invoice"])
        domain = sender_domain(email.sender)
        history = await load_history(runtime.store, domain)
        if invoice.iban and iban_utils.validate(invoice.iban)[0]:
            normalized = iban_utils.normalize(invoice.iban)
            if normalized not in history.ibans:
                history.ibans.append(normalized)
        if invoice.payee_name and invoice.payee_name not in history.payee_names:
            history.payee_names.append(invoice.payee_name)
        if invoice.amount:
            history.amounts = (history.amounts + [invoice.amount])[-24:]
        if ref := (invoice.payment_reference or invoice.invoice_number):
            history.paid_references = (history.paid_references + [ref])[-100:]
        await runtime.store.aput(PAYEES_NS, domain, history.model_dump())
    return {"recorded": True, "status": decision, "trace": [f"record: {decision}"]}


# ---------------------------------------------------------------- build


def build_graph(checkpointer=None, store: BaseStore | None = None):
    builder = StateGraph(InvoiceState, context_schema=AppContext)
    builder.add_node("supervisor", supervisor)
    for name, fn in [
        ("triage", triage),
        ("extract", extract),
        ("validate", validate),
        ("investigate", investigate),
        ("human_review", human_review),
        ("record", record),
    ]:
        builder.add_node(name, fn)
        builder.add_edge(name, "supervisor")
    builder.add_edge(START, "supervisor")
    return builder.compile(checkpointer=checkpointer, store=store)
