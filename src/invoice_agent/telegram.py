"""Minimal Telegram Bot API client (long polling) and message formatting."""

from __future__ import annotations

import html
import re
from typing import Any

import httpx

from . import iban as iban_utils

ACTIONS = ("paid", "remind", "ignored")


class TelegramError(RuntimeError):
    pass


class TelegramBot:
    def __init__(self, token: str) -> None:
        self._client = httpx.AsyncClient(base_url=f"https://api.telegram.org/bot{token}/", timeout=60)

    async def call(self, method: str, **params: Any) -> Any:
        resp = await self._client.post(method, json={k: v for k, v in params.items() if v is not None})
        data = resp.json()
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description')}")
        return data["result"]

    async def get_updates(self, offset: int | None, timeout: int = 30) -> list[dict]:
        return await self.call(
            "getUpdates", offset=offset, timeout=timeout, allowed_updates=["message", "callback_query"]
        )

    async def send_message(self, chat_id: int, text: str, buttons: list[list[dict]] | None = None) -> dict:
        return await self.call(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup={"inline_keyboard": buttons} if buttons else None,
        )

    async def edit_message_text(self, chat_id: int, message_id: int, text: str) -> None:
        await self.call(
            "editMessageText",
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    async def answer_callback(self, callback_id: str, text: str) -> None:
        await self.call("answerCallbackQuery", callback_query_id=callback_id, text=text)

    async def close(self) -> None:
        await self._client.aclose()


# ---------------------------------------------------------------- formatting

HEADERS = {
    "payable": "🧾 <b>Invoice to pay</b>",
    "needs_review": "🧾 <b>Invoice to pay</b>, please double-check",
    "suspicious": "🚨 <b>Suspicious invoice</b>: do NOT pay before verifying",
    "invalid": "⚠️ <b>Invoice could not be validated</b>",
}
ICONS = {"pass": "✅", "warn": "⚠️", "fail": "❌"}


def _e(value: Any) -> str:
    return html.escape(str(value)) if value not in (None, "") else "—"


def format_review(payload: dict) -> str:
    email, inv, val = payload["email"], payload["invoice"], payload["validation"]
    inv_iban = iban_utils.format_iban(inv["iban"]) if inv.get("iban") else None
    amount = f"{inv['amount']:.2f} {inv.get('currency') or 'EUR'}" if inv.get("amount") is not None else None
    lines = [
        HEADERS.get(val["verdict"], "🧾 <b>Invoice</b>"),
        "",
        f"<b>Payee:</b> {_e(inv.get('payee_name') or inv.get('vendor_name'))}",
        f"<b>Amount:</b> {_e(amount)}",
        f"<b>Due:</b> {_e(inv.get('due_date'))}",
        f"<b>IBAN:</b> <code>{_e(inv_iban)}</code>",
        f"<b>BIC:</b> <code>{_e(inv.get('bic'))}</code>",
        f"<b>Reference:</b> <code>{_e(inv.get('payment_reference') or inv.get('invoice_number'))}</code>",
        "",
        f"<i>{_e(email['sender'])}: {_e(email['subject'])}</i>",
        *([f"↪️ forwarded by {_e(email['forwarded_by'])}"] if email.get("forwarded_by") else []),
        *([f"📎 {_e(', '.join(payload['attachments']))}"] if payload.get("attachments") else []),
        "",
        "<b>Checks</b>",
        *[f"{ICONS[c['status']]} {_e(c['name'])}: {_e(c['detail'])}" for c in val["checks"]],
    ]
    if investigation := payload.get("investigation"):
        lines += [
            "",
            f"<b>Investigation</b> (risk: {_e(investigation['risk'])})",
            *[f"• {_e(f)}" for f in investigation["findings"]],
        ]
    if payload.get("remind_count"):
        lines += ["", f"⏰ Reminder #{payload['remind_count']}"]
    lines += ["", f'<a href="https://mail.google.com/mail/u/0/#all/{_e(email["id"])}">Open in Gmail</a>']
    return "\n".join(lines)


def review_buttons(thread_id: str) -> list[list[dict]]:
    # callback_data is limited to 64 bytes; Gmail ids are 16 hex chars.
    return [
        [
            {"text": "✅ Paid", "callback_data": f"paid:{thread_id}"},
            {"text": "⏰ Remind me", "callback_data": f"remind:{thread_id}"},
            {"text": "🚫 Ignore", "callback_data": f"ignored:{thread_id}"},
        ]
    ]


def strip_html(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))
