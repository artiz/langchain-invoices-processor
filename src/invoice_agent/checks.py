"""Deterministic payability checks.

The LLM extracts data; these rules decide. A model can never talk the system
into "payable" when, e.g., the IBAN checksum fails or the IBAN changed.
"""

from __future__ import annotations

import re
import statistics
from datetime import date
from email.utils import parseaddr

from . import iban as iban_utils
from .models import Check, EmailMessage, Invoice, PayeeHistory, Validation

FREE_MAIL_DOMAINS = {
    "gmail.com",
    "googlemail.com",
    "outlook.com",
    "hotmail.com",
    "live.com",
    "yahoo.com",
    "gmx.at",
    "gmx.de",
    "gmx.net",
    "web.de",
    "icloud.com",
    "aon.at",
    "proton.me",
    "protonmail.com",
}

# Fail checks that indicate fraud rather than a broken invoice.
FRAUD_CHECKS = {"payee_iban"}


def sender_domain(sender: str) -> str:
    address = parseaddr(sender)[1] or sender
    return address.rsplit("@", 1)[-1].lower().strip(">") if "@" in address else address.lower()


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def run_checks(email: EmailMessage, invoice: Invoice, history: PayeeHistory, today: date) -> Validation:
    checks: list[Check] = []
    not_required = False

    def add(name: str, status: str, detail: str) -> None:
        checks.append(Check(name=name, status=status, detail=detail))

    # 1. Payment method: direct debit / already paid need no action.
    if invoice.payment_method in ("direct_debit", "card", "already_paid"):
        add("payment_method", "pass", f"{invoice.payment_method.replace('_', ' ')}: no transfer needed")
        not_required = True
    elif invoice.payment_method == "unknown":
        add("payment_method", "warn", "payment method not stated")
    else:
        add("payment_method", "pass", "bank transfer requested")

    # 2. IBAN format + checksum.
    normalized_iban = iban_utils.normalize(invoice.iban) if invoice.iban else None
    iban_ok = False
    if not normalized_iban:
        add("iban", "pass" if not_required else "fail", "no IBAN in email")
    else:
        iban_ok, reason = iban_utils.validate(normalized_iban)
        add("iban", "pass" if iban_ok else "fail", f"{iban_utils.format_iban(normalized_iban)}: {reason}")
        if iban_ok and normalized_iban[:2] not in iban_utils.SEPA_COUNTRIES:
            add("iban_country", "warn", f"non-SEPA country {normalized_iban[:2]}")

    # 3. Amount.
    if invoice.amount is None or invoice.amount <= 0:
        add("amount", "fail", "missing or non-positive amount")
    elif invoice.currency and invoice.currency.upper() != "EUR":
        add("amount", "warn", f"{invoice.amount:.2f} {invoice.currency} (not EUR)")
    else:
        add("amount", "pass", f"{invoice.amount:.2f} {invoice.currency or 'EUR'}")

    # 4. Due date.
    due = _parse_date(invoice.due_date)
    if due is None:
        add("due_date", "warn", "no due date")
    elif due < today:
        add("due_date", "warn", f"overdue since {due.isoformat()} ({(today - due).days} days)")
    else:
        add("due_date", "pass", f"due {due.isoformat()} (in {(due - today).days} days)")

    # 5. Duplicate: same payment reference already paid.
    ref = (invoice.payment_reference or invoice.invoice_number or "").strip()
    if ref and ref in history.paid_references:
        add("duplicate", "pass", f"reference {ref} already marked as paid")
        not_required = True

    # 6. Known payee: IBAN must match what was paid before for this sender.
    if iban_ok:
        if not history.ibans:
            add("payee_iban", "warn", "first invoice from this sender, IBAN not yet confirmed")
        elif normalized_iban in history.ibans:
            add("payee_iban", "pass", "IBAN matches previously paid invoices")
        else:
            known = ", ".join(iban_utils.format_iban(i) for i in history.ibans)
            add("payee_iban", "fail", f"IBAN differs from previously paid ({known})")

    # 7. Amount anomaly vs. history.
    if invoice.amount and len(history.amounts) >= 2:
        median = statistics.median(history.amounts)
        if median > 0 and invoice.amount > 2 * median:
            add("amount_anomaly", "warn", f"{invoice.amount:.2f} is >2x the usual {median:.2f}")

    # 8. Sender plausibility: businesses rarely invoice from free-mail accounts.
    domain = sender_domain(email.sender)
    if domain in FREE_MAIL_DOMAINS:
        add("sender", "warn", f"invoice sent from free-mail domain {domain}")
    elif not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", domain):
        add("sender", "warn", f"unparseable sender {email.sender!r}")
    else:
        add("sender", "pass", domain)

    fails = {c.name for c in checks if c.status == "fail"}
    if fails & FRAUD_CHECKS:
        verdict = "suspicious"
    elif fails:
        verdict = "invalid"
    elif not_required:
        verdict = "not_required"
    elif any(c.status == "warn" for c in checks):
        verdict = "needs_review"
    else:
        verdict = "payable"
    return Validation(verdict=verdict, checks=checks)
