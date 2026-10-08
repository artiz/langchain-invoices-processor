"""Pydantic schemas: LLM structured outputs and graph payloads.

Structured-output schemas keep every field required (nullable instead of optional)
so they work with OpenAI strict JSON schema mode.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Attachment(BaseModel):
    name: str
    content_type: str = ""
    size: int = 0
    text: str = Field(default="", description="Markdown extracted by docling-rs (truncated).")
    note: str = ""


class EmailMessage(BaseModel):
    id: str
    sender: str = Field(description="Original sender; for self-forwards the forwarded message's sender.")
    to: str = ""
    date: str = ""
    subject: str = ""
    body: str = ""
    attachments: list[Attachment] = []
    forwarded_by: str | None = None


class Triage(BaseModel):
    """Is this email asking the recipient to pay something?"""

    is_payment_request: bool = Field(
        description="True only if the email asks the recipient to pay an amount (invoice, bill, payment reminder)."
    )
    category: Literal["invoice", "payment_reminder", "receipt", "direct_debit_notice", "newsletter", "other"]
    reason: str = Field(description="One short sentence explaining the decision.")


class Invoice(BaseModel):
    """Payment data extracted from an invoice email. Use null for anything not stated."""

    vendor_name: str | None = Field(description="Brand / company that issued the invoice, e.g. 'Magenta'.")
    payee_name: str | None = Field(description="Legal name of the payment recipient (Zahlungsempfänger).")
    amount: float | None = Field(description="Total amount due as a number, e.g. 50.05.")
    currency: str | None = Field(description="ISO 4217 currency code, e.g. EUR.")
    invoice_date: str | None = Field(description="Invoice date as YYYY-MM-DD.")
    due_date: str | None = Field(description="Due date (Fällig am) as YYYY-MM-DD.")
    iban: str | None = Field(description="IBAN exactly as written in the email.")
    bic: str | None
    payment_reference: str | None = Field(description="Zahlungsreferenz / Verwendungszweck.")
    invoice_number: str | None
    customer_number: str | None
    payment_method: Literal["bank_transfer", "direct_debit", "card", "already_paid", "unknown"] = Field(
        description="'bank_transfer' if the recipient must transfer money (Überweisung, Zahlschein), "
        "'direct_debit' if it will be collected automatically (SEPA-Lastschrift, Einzug, wird abgebucht)."
    )


class Check(BaseModel):
    name: str
    status: Literal["pass", "warn", "fail"]
    detail: str


Verdict = Literal["payable", "needs_review", "suspicious", "not_required", "invalid"]


class Validation(BaseModel):
    verdict: Verdict
    checks: list[Check]

    @property
    def problems(self) -> list[Check]:
        return [c for c in self.checks if c.status != "pass"]


class Investigation(BaseModel):
    """Result of the fraud investigator agent."""

    risk: Literal["low", "medium", "high"]
    previous_invoices_found: int = Field(description="How many earlier invoices from the same sender were found.")
    iban_seen_before: bool | None = Field(description="Whether the same IBAN appears in earlier legitimate emails.")
    findings: list[str] = Field(description="Short factual findings, max 5.")


WorkerName = Literal["triage", "extract", "validate", "investigate", "human_review", "record", "finish"]


class Route(BaseModel):
    next: WorkerName
    reason: str = Field(description="One short sentence.")


class PayeeHistory(BaseModel):
    """Long-term memory per sender domain, learned from invoices the user confirmed as paid."""

    payee_names: list[str] = []
    ibans: list[str] = []
    amounts: list[float] = []
    paid_references: list[str] = []
