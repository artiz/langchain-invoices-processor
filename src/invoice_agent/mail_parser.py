"""Raw RFC 822 email -> compact Markdown for the LLM, using docling-rs.

* stdlib ``email`` handles MIME structure and headers;
* docling-rs converts the HTML body and every attachment (PDF, DOCX, XLSX, HTML, ...)
  to Markdown. PDFs use the text layer only, so no ML models / OCR are needed;
* token savers: layout tables unwrapped, compact tables, tracking URLs shortened to
  their host, images dropped, per-attachment and total size caps;
* forwarded emails: when the user forwards an invoice to themselves, the original
  sender is used for all checks (``forwarded_by`` keeps the forwarder).
"""

from __future__ import annotations

import email
import io
import logging
import re
from email import policy
from email.message import EmailMessage as MimeMessage
from email.utils import getaddresses, parseaddr
from html.parser import HTMLParser
from urllib.parse import urlparse

from docling_rs import DocumentConverter, DocumentStream, InputFormat, email_attachments

from .models import Attachment, EmailMessage

log = logging.getLogger(__name__)

MAX_BODY_CHARS = 8_000
MAX_ATTACHMENT_CHARS = 6_000
MAX_ATTACHMENTS = 5
MAX_PDF_PAGES = 4
MAX_ATTACHMENT_BYTES = 15_000_000

# Formats worth reading for invoices. Images would need OCR models, so they are listed but not read.
READABLE_FORMATS = {
    InputFormat.PDF, InputFormat.DOCX, InputFormat.DOC, InputFormat.XLSX, InputFormat.XLS, InputFormat.CSV,
    InputFormat.HTML, InputFormat.MD, InputFormat.ODT, InputFormat.ODS, InputFormat.EMAIL,
}  # fmt: skip
EXTENSIONS = {
    InputFormat.PDF: "pdf", InputFormat.DOCX: "docx", InputFormat.DOC: "doc", InputFormat.XLSX: "xlsx",
    InputFormat.XLS: "xls", InputFormat.CSV: "csv", InputFormat.HTML: "html", InputFormat.MD: "md",
    InputFormat.ODT: "odt", InputFormat.ODS: "ods", InputFormat.EMAIL: "eml",
}  # fmt: skip

FORWARD_MARKER = re.compile(
    r"-{2,}\s*(Forwarded message|Weitergeleitete Nachricht|Original Message|Ursprüngliche Nachricht)\s*-{2,}",
    re.I,
)
FORWARD_FROM = re.compile(r"^\W*(From|Von)\W*:\s*(.+)$", re.I | re.M)
EMAIL_ADDRESS = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

_converter: DocumentConverter | None = None


def converter() -> DocumentConverter:
    global _converter
    if _converter is None:
        _converter = DocumentConverter(
            do_ocr=False, do_table_structure=False, text_layer_only=True, compact_tables=True, document_timeout=30
        )
    return _converter


# ---------------------------------------------------------------- HTML


class _LayoutTableUnwrapper(HTMLParser):
    """Turns tables that contain other tables (email layout grids) into divs, keeping data tables intact."""

    def __init__(self, layout_tables: set[int] | None = None) -> None:
        super().__init__(convert_charrefs=False)
        self.layout_tables = layout_tables
        self.found_layout: set[int] = set()
        self.out: list[str] = []
        self._stack: list[int] = []  # open table indexes
        self._count = 0

    def _unwrap(self) -> bool:
        return self.layout_tables is not None and bool(self._stack) and self._stack[-1] in self.layout_tables

    def handle_starttag(self, tag, attrs):
        text = self.get_starttag_text() or f"<{tag}>"
        if tag == "table":
            if self._stack:
                self.found_layout.add(self._stack[-1])
            self._stack.append(self._count)
            self._count += 1
            self.out.append("<div>" if self._unwrap() else text)
        elif tag in ("tr", "td", "th", "tbody", "thead") and self._unwrap():
            self.out.append("<div>")
        elif tag not in ("img", "style", "script"):
            self.out.append(text)

    def handle_startendtag(self, tag, attrs):
        if tag != "img":
            self.out.append(self.get_starttag_text() or "")

    def handle_endtag(self, tag):
        if tag == "table":
            self.out.append("</div>" if self._unwrap() else "</table>")
            if self._stack:
                self._stack.pop()
        elif tag in ("tr", "td", "th", "tbody", "thead") and self._unwrap():
            self.out.append("</div>")
        else:
            self.out.append(f"</{tag}>")

    def handle_data(self, data):
        self.out.append(data)

    def handle_entityref(self, name):
        self.out.append(f"&{name};")

    def handle_charref(self, name):
        self.out.append(f"&#{name};")


def unwrap_layout_tables(html: str) -> str:
    html = re.sub(r"<(style|script|head)\b.*?</\1>", "", html, flags=re.I | re.S)
    scan = _LayoutTableUnwrapper()
    scan.feed(html)
    rewrite = _LayoutTableUnwrapper(scan.found_layout)
    rewrite.feed(html)
    return "".join(rewrite.out)


def _shorten_link(match: re.Match) -> str:
    text, url = match.group(1).strip(), match.group(2)
    if url.startswith("mailto:"):
        return text or url[7:]
    if not text:
        return ""
    host = urlparse(url).netloc or url[:40]
    return f"[{text}]({host})"


def compact_markdown(md: str) -> str:
    md = re.sub(r"\[([^\]]*)\]\(([^)\s]+)\)", _shorten_link, md)
    md = re.sub(r"^\|(\s*\|)+\s*$\n?", "", md, flags=re.M)  # empty table rows
    md = re.sub(r"[ \t]+\n", "\n", md)
    return re.sub(r"\n{3,}", "\n\n", md).strip()


def convert_bytes(data: bytes, name: str, fmt: InputFormat | None = None) -> str:
    if fmt == InputFormat.PDF or name.lower().endswith(".pdf"):
        result = converter().convert(DocumentStream(name=name, stream=io.BytesIO(data)), page_range=(1, MAX_PDF_PAGES))
    else:
        result = converter().convert(DocumentStream(name=name, stream=io.BytesIO(data)))
    md = result.document.export_to_markdown(
        escape_html=False, escape_underscores=False, image_placeholder="", compact_tables=True
    )
    return compact_markdown(md)


def html_to_markdown(html: str) -> str:
    return convert_bytes(unwrap_layout_tables(html).encode("utf-8"), "body.html", InputFormat.HTML)


# ---------------------------------------------------------------- forwards


def find_forwarded_sender(text: str) -> str | None:
    """Original 'From:' of an inline forward (Gmail, Outlook, German clients)."""
    marker = FORWARD_MARKER.search(text)
    if not marker:
        return None
    head = text[marker.end() : marker.end() + 800]
    match = FORWARD_FROM.search(head)
    if not match:
        return None
    line = re.sub(r"[*_\[\]]|\(mailto:[^)]*\)", "", match.group(2))
    address = EMAIL_ADDRESS.search(line)
    if not address:
        return None
    name = line[: line.find(address.group())].strip(" <\"'")
    return f"{name} <{address.group()}>" if name else address.group()


def _address(value: str) -> str:
    return parseaddr(value)[1].lower()


# ---------------------------------------------------------------- main entry


def _body_part(msg: MimeMessage, kind: str) -> str | None:
    part = msg.get_body(preferencelist=(kind,))
    if part is None:
        return None
    try:
        return part.get_content()
    except (LookupError, ValueError):
        return part.get_payload(decode=True).decode("utf-8", "replace")


def _attachments(raw: bytes, owner_emails: set[str], depth: int) -> tuple[list[Attachment], list[EmailMessage]]:
    attachments: list[Attachment] = []
    nested: list[EmailMessage] = []
    try:
        items = email_attachments(raw, max_entry_size=MAX_ATTACHMENT_BYTES, max_total_size=4 * MAX_ATTACHMENT_BYTES)
    except Exception as exc:  # malformed MIME: keep the body, skip attachments
        log.warning("listing attachments failed: %s", exc)
        return [], []
    for item in items:
        if item.inline or (item.content_type or "").startswith(("image/", "application/pkcs7")):
            continue
        att = Attachment(
            name=item.name or f"attachment-{item.index}", content_type=item.content_type or "", size=item.size
        )
        if len(attachments) >= MAX_ATTACHMENTS:
            att.note = "not read: too many attachments"
        elif item.data is None or item.format is None:
            att.note = "not read: too large or no payload"
        elif item.format not in READABLE_FORMATS:
            att.note = f"not read: {item.format.value} not supported"
        elif item.format == InputFormat.EMAIL and depth < 1:
            inner = parse_raw_email(f"{att.name}", item.data, owner_emails, depth=depth + 1)
            nested.append(inner)
            att.text = f"From: {inner.sender}\nSubject: {inner.subject}\n\n{inner.body}"[:MAX_ATTACHMENT_CHARS]
        else:
            try:
                name = att.name if "." in att.name else f"{att.name}.{EXTENSIONS[item.format]}"
                text = convert_bytes(item.data, name, item.format)
                att.text = text[:MAX_ATTACHMENT_CHARS]
                if len(text) > MAX_ATTACHMENT_CHARS:
                    att.note = f"truncated ({len(text)} chars)"
            except Exception as exc:
                att.note = f"not read: {type(exc).__name__}"
        attachments.append(att)
    return attachments, nested


def parse_raw_email(email_id: str, raw: bytes, owner_emails: set[str] = frozenset(), depth: int = 0) -> EmailMessage:
    msg: MimeMessage = email.message_from_bytes(raw, policy=policy.default)
    sender = str(msg.get("From", ""))
    to = str(msg.get("To", ""))

    plain = _body_part(msg, "plain")
    html = _body_part(msg, "html")
    body = ""
    if html:
        try:
            body = html_to_markdown(html)
        except Exception as exc:
            log.warning("HTML conversion failed for %s: %s", email_id, exc)
    if not body and plain:
        body = plain.strip()

    attachments, nested = _attachments(raw, owner_emails, depth)

    # Self-forward ("From: me, To: me" or a configured own address): judge the original sender instead.
    forwarded_by = None
    recipients = {_address(a) for _, a in getaddresses([to]) if a}
    if _address(sender) in owner_emails | recipients:
        original = find_forwarded_sender(plain or body) or (nested[0].sender if nested else None)
        if original and _address(original) != _address(sender):
            forwarded_by, sender = sender, original

    return EmailMessage(
        id=email_id,
        sender=sender,
        to=to,
        date=str(msg.get("Date", "")),
        subject=str(msg.get("Subject", "")),
        body=body[:MAX_BODY_CHARS],
        attachments=attachments,
        forwarded_by=forwarded_by,
    )
