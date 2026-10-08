from datetime import date

from invoice_agent.checks import run_checks, sender_domain
from invoice_agent.models import EmailMessage, Invoice, PayeeHistory

TODAY = date(2026, 10, 8)
EMAIL = EmailMessage(id="1", sender="Magenta <rechnung@magenta.at>", subject="Rechnung")
KNOWN = PayeeHistory(ibans=["AT821200052844072305"], amounts=[50.05, 48.36], paid_references=["111"])


def invoice(**overrides) -> Invoice:
    data = dict(
        vendor_name="Magenta",
        payee_name="T-Mobile Austria GmbH",
        amount=50.05,
        currency="EUR",
        invoice_date="2026-10-05",
        due_date="2026-10-17",
        iban="AT82 1200 0528 4407 2305",
        bic="BKAUATWW",
        payment_reference="222",
        invoice_number=None,
        customer_number=None,
        payment_method="bank_transfer",
    )
    return Invoice(**{**data, **overrides})


def statuses(validation):
    return {c.name: c.status for c in validation.checks}


def test_known_payee_is_payable():
    assert run_checks(EMAIL, invoice(), KNOWN, TODAY).verdict == "payable"


def test_first_invoice_needs_review():
    v = run_checks(EMAIL, invoice(), PayeeHistory(), TODAY)
    assert v.verdict == "needs_review"
    assert statuses(v)["payee_iban"] == "warn"


def test_changed_iban_is_suspicious():
    v = run_checks(EMAIL, invoice(iban="DE89 3704 0044 0532 0130 00"), KNOWN, TODAY)
    assert v.verdict == "suspicious"


def test_bad_checksum_is_invalid():
    v = run_checks(EMAIL, invoice(iban="AT82 1200 0528 4407 2306"), KNOWN, TODAY)
    assert v.verdict == "invalid"
    assert statuses(v)["iban"] == "fail"


def test_direct_debit_and_duplicates_need_no_action():
    assert run_checks(EMAIL, invoice(payment_method="direct_debit", iban=None), KNOWN, TODAY).verdict == "not_required"
    assert run_checks(EMAIL, invoice(payment_reference="111"), KNOWN, TODAY).verdict == "not_required"


def test_warnings():
    v = run_checks(
        EmailMessage(id="2", sender="someone@gmail.com"),
        invoice(due_date="2026-10-01", amount=500.0),
        KNOWN,
        TODAY,
    )
    s = statuses(v)
    assert (s["due_date"], s["amount_anomaly"], s["sender"]) == ("warn", "warn", "warn")
    assert v.verdict == "needs_review"


def test_sender_domain():
    assert sender_domain("Magenta <rechnung@magenta.at>") == "magenta.at"
    assert sender_domain("RECHNUNG@Magenta.AT") == "magenta.at"
