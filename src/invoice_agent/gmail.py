"""Gmail access through the KateChat Gmail MCP server (streamable HTTP).

Only read-only MCP tools are exposed to the agent (allowlist): the LLM never sees
``send_email`` / ``create_draft``, so a prompt injection in an email cannot make
the agent send mail.
"""

from __future__ import annotations

import asyncio
import base64
import re
from dataclasses import dataclass

import httpx
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

from .google_auth import GoogleTokenProvider
from .mail_parser import parse_raw_email
from .models import EmailMessage

READ_ONLY_TOOLS = {"list_emails", "get_email", "get_raw_email", "search_emails"}
GMAIL_MESSAGES_API = "https://gmail.googleapis.com/gmail/v1/users/me/messages"


class GmailError(RuntimeError):
    pass


@dataclass
class EmailHeader:
    id: str
    sender: str
    date: str
    subject: str


def _as_text(result) -> str:
    """langchain-mcp-adapters may return a string or a list of content blocks."""
    if isinstance(result, str):
        return result
    if isinstance(result, tuple):
        result = result[0]
    if isinstance(result, list):
        return "\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in result)
    return str(result)


def parse_headers(text: str) -> list[EmailHeader]:
    headers = []
    for block in text.split("\n---\n"):
        fields = dict(re.findall(r"^(ID|From|Date|Subject): ?(.*)$", block.strip(), re.M))
        if "ID" in fields:
            headers.append(
                EmailHeader(
                    fields["ID"].strip(), fields.get("From", ""), fields.get("Date", ""), fields.get("Subject", "")
                )
            )
    return headers


class GmailMCP:
    def __init__(self, url: str, tokens: GoogleTokenProvider, owner_emails: frozenset[str] = frozenset()) -> None:
        self.url = url
        self.tokens = tokens
        self.owner_emails = owner_emails
        self._token: str | None = None
        self._tools: dict[str, BaseTool] = {}

    async def _get_tools(self) -> dict[str, BaseTool]:
        token = await self.tokens.get_access_token()
        if token != self._token or not self._tools:
            # Headers are fixed per client, so rebuild it whenever the access token rotates.
            client = MultiServerMCPClient(
                {
                    "gmail": {
                        "transport": "streamable_http",
                        "url": self.url,
                        "headers": {"Authorization": f"Bearer {token}"},
                    }
                }
            )
            tools = await client.get_tools()
            self._tools = {t.name: t for t in tools if t.name in READ_ONLY_TOOLS}
            self._token = token
        return self._tools

    async def _call(self, tool: str, args: dict) -> str:
        tools = await self._get_tools()
        text = _as_text(await tools[tool].ainvoke(args))
        if re.match(r"^(Error|Gmail API error)|access token is required", text, re.I):
            raise GmailError(text[:500])
        return text

    async def search(self, query: str, max_results: int = 20) -> list[EmailHeader]:
        text = await self._call("search_emails", {"query": query, "maxResults": max_results})
        return [] if text.startswith("No emails found") else parse_headers(text)

    async def get_raw(self, email_id: str) -> bytes:
        """RFC 822 source incl. attachments."""
        if "get_raw_email" in await self._get_tools():
            raw = await self._call("get_raw_email", {"emailId": email_id})
        else:
            # Fallback for MCP servers without get_raw_email: same token, same read-only scope.
            token = await self.tokens.get_access_token()
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.get(
                    f"{GMAIL_MESSAGES_API}/{email_id}",
                    params={"format": "raw"},
                    headers={"Authorization": f"Bearer {token}"},
                )
            if resp.status_code != 200:
                raise GmailError(f"Gmail API error {resp.status_code}: {resp.text[:300]}")
            raw = resp.json()["raw"]
        raw = raw.strip()
        return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))

    async def get_email(self, email_id: str) -> EmailMessage:
        raw = await self.get_raw(email_id)
        # docling-rs conversion is CPU-bound and synchronous.
        return await asyncio.to_thread(parse_raw_email, email_id, raw, self.owner_emails)
