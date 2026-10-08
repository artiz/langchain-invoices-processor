"""Telegram app layer with a fake bot: /start linking, notification, button -> resume."""

from pathlib import Path
from zoneinfo import ZoneInfo

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from test_graph import FakeLLM, make_invoice

from invoice_agent.app import NOTIFY_NS, InvoiceApp
from invoice_agent.config import Settings
from invoice_agent.graph import PAYEES_NS, AppContext, build_graph
from invoice_agent.models import EmailMessage


class FakeBot:
    def __init__(self):
        self.sent, self.edited, self.answers = [], [], []

    async def send_message(self, chat_id, text, buttons=None):
        self.sent.append((chat_id, text, buttons))
        return {"message_id": len(self.sent)}

    async def edit_message_text(self, chat_id, message_id, text):
        self.edited.append((chat_id, message_id, text))

    async def answer_callback(self, callback_id, text):
        self.answers.append(text)


def make_app(tmp_path: Path) -> tuple[InvoiceApp, FakeBot, InMemoryStore]:
    settings = Settings(
        openai_model="fake",
        openai_reasoning_effort=None,
        gmail_mcp_url="",
        gmail_query="",
        google_client_id=None,
        google_client_secret=None,
        google_refresh_token=None,
        google_oauth_port=0,
        telegram_bot_token="x",
        telegram_chat_id=None,
        poll_interval_seconds=60,
        remind_after_hours=24,
        timezone=ZoneInfo("Europe/Vienna"),
        data_dir=tmp_path,
    )
    store, bot = InMemoryStore(), FakeBot()
    context = AppContext(llm=FakeLLM(make_invoice("REF-1"), route="human_review"))
    return InvoiceApp(settings, build_graph(InMemorySaver(), store), store, context, bot=bot), bot, store


def message(chat_id, text):
    return {"message": {"chat": {"id": chat_id}, "text": text}}


def press(chat_id, data, message_id=1):
    return {
        "callback_query": {"id": "cb", "data": data, "message": {"chat": {"id": chat_id}, "message_id": message_id}}
    }


async def test_telegram_flow(tmp_path):
    app, bot, store = make_app(tmp_path)
    email = EmailMessage(id="m1", sender="rechnung@magenta.at", subject="Rechnung", body="...")

    # Interrupt before any chat is linked: queued, not sent.
    await app.process_email(email)
    assert bot.sent == []

    # /start links the chat and flushes the queue.
    await app.handle_update(message(42, "/start"))
    assert "Linked" in bot.sent[0][1]
    chat_id, text, buttons = bot.sent[1]
    assert chat_id == 42 and "T-Mobile Austria GmbH" in text
    assert [b["callback_data"] for b in buttons[0]] == ["paid:m1", "remind:m1", "ignored:m1"]

    # Strangers are ignored.
    await app.handle_update(message(7, "/start"))
    assert bot.sent[-1][1] == "This bot is private."
    await app.handle_update(press(7, "paid:m1"))
    assert bot.answers[-1] == "Not authorized"

    # Remind -> rescheduled for later, not resent now.
    await app.handle_update(press(42, "remind:m1"))
    assert "remind you at" in bot.edited[-1][2]
    note = (await store.aget(NOTIFY_NS, "m1")).value
    assert note["sent"] is False and note["notify_after"]
    sent_before = len(bot.sent)
    await app.flush_notifications()
    assert len(bot.sent) == sent_before

    # Paid -> graph finishes, notification removed, payee learned.
    await app.handle_update(press(42, "paid:m1"))
    assert "Marked as paid" in bot.edited[-1][2]
    assert await store.aget(NOTIFY_NS, "m1") is None
    assert (await store.aget(PAYEES_NS, "magenta.at")).value["ibans"] == ["AT821200052844072305"]

    # Double tap.
    await app.handle_update(press(42, "paid:m1"))
    assert bot.answers[-1] == "Already handled"
