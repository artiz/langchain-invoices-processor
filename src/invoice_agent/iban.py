"""IBAN normalisation and ISO 13616 mod-97 validation (no external deps)."""

from __future__ import annotations

import re

# Lengths for SEPA countries plus a few common others.
IBAN_LENGTHS = {
    "AD": 24,
    "AT": 20,
    "BE": 16,
    "BG": 22,
    "CH": 21,
    "CY": 28,
    "CZ": 24,
    "DE": 22,
    "DK": 18,
    "EE": 20,
    "ES": 24,
    "FI": 18,
    "FR": 27,
    "GB": 22,
    "GI": 23,
    "GR": 27,
    "HR": 21,
    "HU": 28,
    "IE": 22,
    "IS": 26,
    "IT": 27,
    "LI": 21,
    "LT": 20,
    "LU": 20,
    "LV": 21,
    "MC": 27,
    "MT": 31,
    "NL": 18,
    "NO": 15,
    "PL": 28,
    "PT": 25,
    "RO": 24,
    "SE": 24,
    "SI": 19,
    "SK": 24,
    "SM": 27,
    "UA": 29,
    "VA": 22,
}

SEPA_COUNTRIES = set(IBAN_LENGTHS) - {"UA"}


def normalize(iban: str) -> str:
    return re.sub(r"[\s\-.]", "", iban).upper()


def format_iban(iban: str) -> str:
    n = normalize(iban)
    return " ".join(n[i : i + 4] for i in range(0, len(n), 4))


def validate(iban: str) -> tuple[bool, str]:
    """Return (is_valid, reason)."""
    n = normalize(iban)
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{8,30}", n):
        return False, "malformed IBAN"
    country = n[:2]
    expected = IBAN_LENGTHS.get(country)
    if expected is None:
        return False, f"unsupported country {country}"
    if len(n) != expected:
        return False, f"{country} IBAN must have {expected} characters, got {len(n)}"
    rearranged = n[4:] + n[:4]
    digits = "".join(str(int(ch, 36)) for ch in rearranged)
    if int(digits) % 97 != 1:
        return False, "checksum mismatch"
    return True, "valid"
