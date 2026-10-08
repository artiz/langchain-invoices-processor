import pytest

from invoice_agent.iban import format_iban, normalize, validate


@pytest.mark.parametrize(
    "iban",
    ["AT82 1200 0528 4407 2305", "DE89370400440532013000", "at61 1904 3002 3457 3201"],
)
def test_valid(iban):
    assert validate(iban) == (True, "valid")


@pytest.mark.parametrize(
    ("iban", "reason"),
    [
        ("AT82 1200 0528 4407 2306", "checksum mismatch"),
        ("AT82 1200 0528 4407 230", "AT IBAN must have 20 characters, got 19"),
        ("XX82 1200 0528 4407 2305", "unsupported country XX"),
        ("not an iban", "malformed IBAN"),
    ],
)
def test_invalid(iban, reason):
    assert validate(iban) == (False, reason)


def test_format():
    assert normalize("at82-1200 0528") == "AT8212000528"
    assert format_iban("AT821200052844072305") == "AT82 1200 0528 4407 2305"
