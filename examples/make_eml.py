"""Generate the synthetic .eml samples (PDF invoices as attachments).

    uv run --with reportlab python examples/make_eml.py
"""

import io
from email.message import EmailMessage
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

OUT = Path(__file__).parent / "emails"


def invoice_pdf(reference: str) -> bytes:
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=A4)
    y = 800
    for line in [
        "T-Mobile Austria GmbH, Rennweg 97-99, 1030 Wien",
        f"Rechnung Nr. {reference}",
        "Kundennummer 1.20000001",
        "Rechnungsdatum 05.10.2026",
        "Faellig am 17.10.2026",
        "Grundgebuehr Magenta Internet 250    45,05 EUR",
        "Servicepauschale                    5,00 EUR",
        "Gesamtbetrag                       50,05 EUR",
        "Zahlart: Ueberweisung",
        "Bitte ueberweisen Sie auf IBAN AT82 1200 0528 4407 2305, BIC BKAUATWW",
        f"Zahlungsreferenz {reference}",
    ]:
        pdf.drawString(60, y, line)
        y -= 22
    pdf.showPage()
    pdf.drawString(60, 800, "Seite 2: Einzelverbindungsnachweis")
    pdf.save()
    return buf.getvalue()


def pdf_only() -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = "Magenta <rechnung@magenta.at>"
    msg["To"] = "max.mustermann@example.com"
    msg["Subject"] = "Ihre Magenta Rechnung"
    msg["Date"] = "Mon, 05 Oct 2026 20:24:11 +0200"
    msg.set_content("Sehr geehrter Herr Mustermann,\nIhre aktuelle Rechnung finden Sie im Anhang.\nIhr Magenta Team")
    msg.add_alternative(
        "<html><body><table width='100%'><tr><td><img src='logo.png'></td></tr><tr><td>"
        "<p>Sehr geehrter Herr Mustermann,</p><p>Ihre aktuelle Rechnung finden Sie im Anhang.</p>"
        "<p><a href='https://www.magenta.at/rechnung?utm_source=mail&amp;cid=123456789'>Rechnung online</a></p>"
        "</td></tr></table></body></html>",
        subtype="html",
    )
    msg.add_attachment(invoice_pdf("918443780999"), maintype="application", subtype="pdf", filename="Rechnung.pdf")
    return msg


def self_forward() -> EmailMessage:
    me = "Max Mustermann <max.mustermann@gmail.com>"
    msg = EmailMessage()
    msg["From"] = me
    msg["To"] = me
    msg["Subject"] = "Fwd: Magenta Rechnung für Kundennummer: 1.20000001"
    msg["Date"] = "Thu, 08 Oct 2026 10:15:00 +0200"
    msg.set_content(
        "bitte bezahlen\n\n"
        "---------- Forwarded message ---------\n"
        "From: Magenta <rechnung@magenta.at>\n"
        "Date: Mon, Oct 5, 2026 at 8:24 PM\n"
        "Subject: Magenta Rechnung für Kundennummer: 1.20000001\n"
        "To: <max.mustermann@gmail.com>\n\n"
        "Rechnungsdatum 05.10.2026, Fällig am 17.10.2026, Betrag 50.05 EUR\n"
        "Zahlart: Überweisung, IBAN: AT82 1200 0528 4407 2305, Zahlungsreferenz: 918443781234\n"
    )
    msg.add_alternative(
        "<div dir='ltr'>bitte bezahlen<br><br><div class='gmail_quote'>"
        "<div class='gmail_attr'>---------- Forwarded message ---------<br>"
        "From: <strong class='gmail_sendername'>Magenta</strong> "
        "<span>&lt;<a href='mailto:rechnung@magenta.at'>rechnung@magenta.at</a>&gt;</span><br>"
        "Date: Mon, Oct 5, 2026 at 8:24 PM<br>Subject: Magenta Rechnung für Kundennummer: 1.20000001<br></div>"
        "<table><tr><th>Rechnungsdatum</th><th>Fällig am</th><th>Betrag (€)</th></tr>"
        "<tr><td>05.10.2026</td><td>17.10.2026</td><td>50.05</td></tr></table>"
        "<p>Zahlart: Überweisung | IBAN: AT82 1200 0528 4407 2305 | Zahlungsreferenz: 918443781234</p>"
        "</div></div>",
        subtype="html",
    )
    msg.add_attachment(invoice_pdf("918443781234"), maintype="application", subtype="pdf", filename="Rechnung.pdf")
    return msg


if __name__ == "__main__":
    (OUT / "magenta_pdf_only.eml").write_bytes(bytes(pdf_only()))
    (OUT / "self_forwarded.eml").write_bytes(bytes(self_forward()))
    print("written")
