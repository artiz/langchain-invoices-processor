"""Application layer: Gmail polling, Telegram bot and graph resume.

Each Gmail message is one LangGraph thread (``thread_id = message id``). When a
thread stops at ``interrupt()``, the review payload is queued as a notification
in the Store; the Telegram callback ``<action>:<thread_id>`` resumes it.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import AsyncIterator

from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.store.base import BaseStore
from langgraph.store.sqlite.aio import AsyncSqliteStore
from langgraph.types import Command, StateSnapshot

from .checks import sender_domain
from .config import Settings
from .gmail import GmailMCP
from .google_auth import GoogleTokenProvider
from .graph import AppContext, build_graph, review_payload
from .models import EmailMessage
from .telegram import ACTIONS, TelegramBot, format_review, review_buttons

log = logging.getLogger(__name__)

NOTIFY_NS = ("notifications",)
TELEGRAM_NS = ("telegram",)

HELP = "Commands:\n/check: scan Gmail now\n/pending: invoices waiting for you"


def make_llm(settings: Settings) -> ChatOpenAI:
    kwargs = {"reasoning_effort": settings.openai_reasoning_effort} if settings.openai_reasoning_effort else {}
    # Responses API: required for tool calling together with reasoning_effort on GPT-5.x models.
    return ChatOpenAI(model=settings.openai_model, use_responses_api=True, **kwargs)


def tracing_callbacks() -> list:
    """Langfuse tracing when LANGFUSE_PUBLIC_KEY is set (pip extra `langfuse`)."""
    if not os.getenv("LANGFUSE_PUBLIC_KEY"):
        return []
    try:
        from langfuse.langchain import CallbackHandler
    except ImportError:
        log.warning("LANGFUSE_PUBLIC_KEY set but langfuse is not installed: uv sync --extra langfuse")
        return []
    return [CallbackHandler()]


class InvoiceApp:
    def __init__(
        self,
        settings: Settings,
        graph,
        store: BaseStore,
        context: AppContext,
        gmail: GmailMCP | None = None,
        bot: TelegramBot | None = None,
    ) -> None:
        self.settings = settings
        self.graph = graph
        self.store = store
        self.context = context
        self.gmail = gmail
        self.bot = bot
        self.callbacks = tracing_callbacks()
        self._poll_lock = asyncio.Lock()

    def config(self, thread_id: str) -> dict:
        return {
            "configurable": {"thread_id": thread_id},
            "callbacks": self.callbacks,
            "metadata": {"langfuse_session_id": thread_id},
            "recursion_limit": 30,
        }

    # ------------------------------------------------------------ graph runs

    async def process_email(self, email: EmailMessage) -> StateSnapshot:
        await self.graph.ainvoke(
            {"email": email.model_dump(), "trace": []}, self.config(email.id), context=self.context
        )
        return await self._after_run(email.id)

    async def resume(self, thread_id: str, action: str) -> StateSnapshot:
        await self.graph.ainvoke(Command(resume={"action": action}), self.config(thread_id), context=self.context)
        return await self._after_run(thread_id)

    async def _after_run(self, thread_id: str) -> StateSnapshot:
        snap = await self.graph.aget_state(self.config(thread_id))
        if snap.interrupts:
            payload = snap.interrupts[0].value
            await self.store.aput(
                NOTIFY_NS,
                thread_id,
                {"payload": payload, "notify_after": payload.get("notify_after"), "sent": False},
            )
        elif await self.store.aget(NOTIFY_NS, thread_id):
            await self.store.adelete(NOTIFY_NS, thread_id)
        log.info("thread %s: %s", thread_id, " | ".join(snap.values.get("trace", [])))
        return snap

    async def poll_gmail(self) -> int:
        """Process new candidate emails. Returns the number of newly processed emails."""
        if not self.gmail:
            return 0
        async with self._poll_lock:
            processed = 0
            for header in reversed(await self.gmail.search(self.settings.gmail_query, max_results=25)):
                config = self.config(header.id)
                snap = await self.graph.aget_state(config)
                try:
                    if not snap.values:
                        await self.process_email(await self.gmail.get_email(header.id))
                        processed += 1
                    elif snap.next and not snap.interrupts:
                        # A previous run crashed mid-graph: continue from the last checkpoint.
                        log.info("resuming unfinished thread %s at %s", header.id, snap.next)
                        await self.graph.ainvoke(None, config, context=self.context)
                        await self._after_run(header.id)
                except Exception:
                    log.exception("failed to process email %s (%s)", header.id, header.subject)
            await self.flush_notifications()
            return processed

    # ------------------------------------------------------------ telegram

    async def chat_id(self) -> int | None:
        if self.settings.telegram_chat_id:
            return self.settings.telegram_chat_id
        item = await self.store.aget(TELEGRAM_NS, "chat")
        return item.value["chat_id"] if item else None

    async def pending(self) -> list:
        return await self.store.asearch(NOTIFY_NS, limit=100)

    async def flush_notifications(self) -> None:
        chat_id = await self.chat_id()
        if not self.bot or not chat_id:
            return
        now = datetime.now(self.settings.timezone)
        for item in await self.pending():
            note = item.value
            if note["sent"] or (note["notify_after"] and datetime.fromisoformat(note["notify_after"]) > now):
                continue
            await self.bot.send_message(chat_id, format_review(note["payload"]), review_buttons(item.key))
            await self.store.aput(NOTIFY_NS, item.key, {**note, "sent": True})

    async def handle_update(self, update: dict) -> None:
        if callback := update.get("callback_query"):
            await self._handle_callback(callback)
        elif message := update.get("message"):
            await self._handle_message(message)

    async def _handle_callback(self, callback: dict) -> None:
        message = callback.get("message") or {}
        chat_id = message.get("chat", {}).get("id")
        if chat_id != await self.chat_id():
            await self.bot.answer_callback(callback["id"], "Not authorized")
            return
        action, _, thread_id = (callback.get("data") or "").partition(":")
        if action not in ACTIONS:
            return
        if not (await self.graph.aget_state(self.config(thread_id))).interrupts:
            await self.bot.answer_callback(callback["id"], "Already handled")
            return
        await self.bot.answer_callback(callback["id"], "Got it")
        snap = await self.resume(thread_id, action)
        values = snap.values
        if action == "paid":
            footer = f"✅ Marked as paid. IBAN remembered for {sender_domain(values['email']['sender'])}."
        elif action == "ignored":
            footer = "🚫 Ignored."
        else:
            footer = f"⏰ I'll remind you at {values.get('notify_after', '').replace('T', ' ')}."
        await self.bot.edit_message_text(
            chat_id, message["message_id"], f"{format_review(review_payload(values))}\n\n<b>{footer}</b>"
        )

    async def _handle_message(self, message: dict) -> None:
        chat_id = message["chat"]["id"]
        text = (message.get("text") or "").strip()
        linked = await self.chat_id()
        if text.startswith("/start"):
            if linked is None:
                await self.store.aput(TELEGRAM_NS, "chat", {"chat_id": chat_id})
                await self.bot.send_message(chat_id, f"Linked ✅ I'll notify you here about invoices to pay.\n\n{HELP}")
                await self.flush_notifications()
            elif linked == chat_id:
                await self.bot.send_message(chat_id, f"Already linked.\n\n{HELP}")
            else:
                await self.bot.send_message(chat_id, "This bot is private.")
            return
        if chat_id != linked:
            return  # guardrail: ignore everyone except the owner
        if text.startswith("/check"):
            await self.bot.send_message(chat_id, "Scanning Gmail…")
            count = await self.poll_gmail()
            await self.bot.send_message(chat_id, f"Done: {count} new email(s) processed.")
        elif text.startswith("/pending"):
            items = await self.pending()
            if not items:
                await self.bot.send_message(chat_id, "Nothing pending 🎉")
            for item in items:
                await self.bot.send_message(chat_id, format_review(item.value["payload"]), review_buttons(item.key))
        else:
            await self.bot.send_message(chat_id, HELP)

    # ------------------------------------------------------------ loops

    async def gmail_loop(self) -> None:
        while True:
            try:
                count = await self.poll_gmail()
                log.info("gmail poll done: %d new", count)
            except Exception:
                log.exception("gmail poll failed")
            await asyncio.sleep(self.settings.poll_interval_seconds)

    async def telegram_loop(self) -> None:
        offset = None
        if not await self.chat_id():
            log.warning("Telegram chat not linked yet: send /start to your bot")
        while True:
            try:
                for update in await self.bot.get_updates(offset):
                    offset = update["update_id"] + 1
                    try:
                        await self.handle_update(update)
                    except Exception:
                        log.exception("failed to handle telegram update")
            except Exception:
                log.exception("telegram polling failed")
                await asyncio.sleep(5)

    async def reminder_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                await self.flush_notifications()
            except Exception:
                log.exception("sending reminders failed")

    async def run_forever(self) -> None:
        loops = [self.reminder_loop()]
        if self.gmail:
            loops.append(self.gmail_loop())
        if self.bot:
            loops.append(self.telegram_loop())
        await asyncio.gather(*loops)


@asynccontextmanager
async def open_app(settings: Settings, use_gmail: bool = True) -> AsyncIterator[InvoiceApp]:
    async with (
        AsyncSqliteSaver.from_conn_string(str(settings.checkpoint_db)) as saver,
        AsyncSqliteStore.from_conn_string(str(settings.store_db)) as store,
    ):
        await store.setup()
        gmail = None
        if use_gmail and settings.gmail_enabled:
            tokens = GoogleTokenProvider(
                settings.google_client_id,
                settings.google_client_secret,
                settings.google_token_file,
                settings.google_refresh_token,
            )
            gmail = GmailMCP(settings.gmail_mcp_url, tokens, settings.owner_emails)
        bot = TelegramBot(settings.telegram_bot_token) if settings.telegram_bot_token else None
        context = AppContext(
            llm=make_llm(settings),
            gmail=gmail,
            timezone=settings.timezone,
            remind_after_hours=settings.remind_after_hours,
        )
        app = InvoiceApp(settings, build_graph(saver, store), store, context, gmail, bot)
        try:
            yield app
        finally:
            if bot:
                await bot.close()
