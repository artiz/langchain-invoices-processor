from email.message import EmailMessage as MimeMessage
from pathlib import Path

from invoice_agent.mail_parser import compact_markdown, find_forwarded_sender, html_to_markdown, parse_raw_email

EMAILS = Path(__file__).parent.parent / "examples" / "emails"


def test_pdf_attachment_is_extracted():
    email = parse_raw_email("x", (EMAILS / "magenta_pdf_only.eml").read_bytes())
    assert email.sender == "Magenta <rechnung@magenta.at>"
    assert email.forwarded_by is None
    assert "im Anhang" in email.body
    assert "[Rechnung online](www.magenta.at)" in email.body  # tracking URL shortened to host
    [pdf] = email.attachments
    assert pdf.name == "Rechnung.pdf"
    assert "AT82 1200 0528 4407 2305" in pdf.text and "50,05 EUR" in pdf.text


def test_self_forward_uses_original_sender():
    email = parse_raw_email("x", (EMAILS / "self_forwarded.eml").read_bytes())
    assert email.sender == "Magenta <rechnung@magenta.at>"
    assert email.forwarded_by == "Max Mustermann <max.mustermann@gmail.com>"


def test_forward_from_stranger_is_not_trusted():
    raw = (
        (EMAILS / "self_forwarded.eml")
        .read_bytes()
        .replace(b"To: Max Mustermann <max.mustermann@gmail.com>", b"To: victim@example.com")
    )
    email = parse_raw_email("x", raw)
    assert email.sender == "Max Mustermann <max.mustermann@gmail.com>"
    assert email.forwarded_by is None


def test_owner_emails_allow_forwards_from_other_own_address():
    raw = (
        (EMAILS / "self_forwarded.eml")
        .read_bytes()
        .replace(b"To: Max Mustermann <max.mustermann@gmail.com>", b"To: max@work.example")
    )
    email = parse_raw_email("x", raw, owner_emails=frozenset({"max.mustermann@gmail.com"}))
    assert email.sender == "Magenta <rechnung@magenta.at>"


def test_layout_tables_are_unwrapped_and_data_tables_kept():
    html = (
        "<table><tr><td><img src='logo.png'></td></tr><tr><td>"
        "<table><tr><th>Fällig am</th><th>Betrag</th></tr><tr><td>17.10.2026</td><td>50.05</td></tr></table>"
        "</td></tr></table>"
    )
    md = html_to_markdown(html)
    assert "| Fällig am | Betrag |" in md and "| 17.10.2026 | 50.05 |" in md


def test_find_forwarded_sender_variants():
    assert find_forwarded_sender("---------- Weitergeleitete Nachricht ---------\nVon: A1 <rechnung@a1.at>") == (
        "A1 <rechnung@a1.at>"
    )
    assert find_forwarded_sender("-----Original Message-----\nFrom: billing@acme.com\nSent: ...") == "billing@acme.com"
    assert find_forwarded_sender("no forward here\nFrom: x@y.com") is None


def test_compact_markdown():
    assert (
        compact_markdown("[](https://fb.com/x) [Pay](https://pay.example.com/a?b=c)\n\n\n\nx")
        == "[Pay](pay.example.com)\n\nx"
    )


def test_plain_only_email():
    msg = MimeMessage()
    msg["From"], msg["To"], msg["Subject"] = "a@b.at", "c@d.at", "Hi"
    msg.set_content("Bitte 10 EUR überweisen.")
    assert parse_raw_email("x", bytes(msg)).body == "Bitte 10 EUR überweisen."
