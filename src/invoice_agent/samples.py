"""Load sample emails for `demo` and evals: a JSON case (inline body or an `eml` file) or a raw .eml."""

from __future__ import annotations

import json
from pathlib import Path

from .mail_parser import parse_raw_email
from .models import EmailMessage


def load_case(path: str | Path) -> tuple[EmailMessage, dict, dict]:
    """Returns (email, payee history seed, expected results)."""
    path = Path(path)
    if path.suffix == ".eml":
        return parse_raw_email(path.stem, path.read_bytes()), {}, {}
    case = json.loads(path.read_text())
    if "eml" in case:
        email = parse_raw_email(case["id"], (path.parent / case["eml"]).read_bytes())
    else:
        email = EmailMessage(**{k: v for k, v in case.items() if k in EmailMessage.model_fields})
    return email, case.get("history", {}), case.get("expected", {})
