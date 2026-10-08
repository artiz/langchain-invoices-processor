# langchain-invoices-processor

A LangGraph **supervisor/worker** agent that watches Gmail through an **MCP server**, finds emails that ask
you to pay something (invoice, bill, dunning letter with an IBAN), checks whether they are really payable,
and asks you in **Telegram** (Human-in-the-Loop via `interrupt()` and a persistent checkpointer) whether you've paid.

Gmail access goes through the Gmail MCP server of [KateChat](https://github.com/artiz/kate-chat)
(`https://katechat.tech/mcp/gmail`).

```
Gmail ──MCP (raw MIME)──▶ docling-rs (body + attachments → Markdown) ──▶ LangGraph thread per email (SQLite checkpointer)
                               │
                 ┌──────── supervisor (LLM router + guardrails) ────────┐
                 ▼          ▼           ▼             ▼                 ▼
              triage     extract     validate     investigate      human_review ──interrupt()──▶ Telegram
             (LLM, SO)  (LLM, SO)  (deterministic) (ReAct agent,        ▲                     [✅ Paid] [⏰ Remind] [🚫 Ignore]
                                                    Gmail MCP tools)    └──── Command(resume=…) ◀───────┘
                                                                                   │
                                                                               record ──▶ Store (payee memory)
```

## Concepts shown

| Concept | Where |
|---|---|
| Supervisor / worker graph, routing with `Command(goto=…)` | `graph.py: supervisor` |
| Guarded LLM routing: code computes the allowed next steps and the LLM only chooses among them; it falls back to a human if the LLM picks something else | `graph.py: allowed_next` |
| Structured output (Pydantic, strict JSON schema) | `models.py`, `triage` / `extract` |
| Human-in-the-Loop: `interrupt()` + `Command(resume=…)`, resumed hours later from another process | `human_review`, `app.py` |
| Persistence: `AsyncSqliteSaver` checkpointer, one thread per Gmail message; crashed runs continue from the last checkpoint | `app.py: poll_gmail` |
| Long-term memory across threads: LangGraph `Store` keeps known IBANs per sender | `record`, `load_history` |
| Tool-calling sub-agent (`langchain.agents.create_agent`) with call-limit middleware | `investigate` |
| MCP via `langchain-mcp-adapters` (streamable HTTP, Bearer token) | `gmail.py` |
| Email + attachment parsing with [docling-rs](https://pypi.org/project/docling-rs/) (no ML models, PDF text layer) | `mail_parser.py` |
| Guardrails | see below |
| Evals on a labelled dataset | `evals/run.py` |
| Observability: optional Langfuse callback handler | `app.py: tracing_callbacks` |

### Guardrails

- **The LLM extracts the data, but code decides.** IBAN mod-97 checksum, due date, duplicate payment reference,
  "IBAN differs from the one you paid before" (→ `suspicious`), amount spikes, free-mail senders (`checks.py`).
- **Read-only tool allowlist**: the agent only gets `list_emails/get_email/search_emails` from MCP and never
  `send_email`. The OAuth scope is `gmail.readonly`.
- **Prompt injection**: email content is wrapped in `<email>` tags and marked as untrusted. Routing is limited by
  `allowed_next`, so an email can't skip the human review (see `examples/emails/prompt_injection.json`).
- **Human approval** is always required before anything is marked as paid. The bot only answers the linked chat.
- Call limits on the investigator agent, a recursion limit on the graph, and the bot token is kept out of the logs.

### Email parsing (docling-rs)

The agent fetches the raw RFC 822 message (MCP tool `get_raw_email`, or the Gmail API with the same read-only
token if the MCP server doesn't have that tool yet) and turns it into compact Markdown:

- stdlib `email` reads the MIME structure and headers; **docling-rs** converts the HTML body and all attachments
  (PDF, DOCX, XLSX, ODT, CSV, HTML, attached `.eml`) to Markdown. PDFs use the text layer only
  (`text_layer_only=True`, first 4 pages), so no OCR or ML model download is needed.
- Fewer tokens: nested layout tables of HTML newsletters are unwrapped (the inner data tables stay real tables),
  compact tables, tracking URLs shortened to their host, images and empty links dropped,
  each attachment capped at 6k chars.
- **Self-forwards**: if you forward an invoice to yourself (From = To, or an address in `OWNER_EMAILS`), the
  original `From:` of the forwarded message is used for checks, payee memory and the investigator's mailbox search.

```bash
uv run invoice-agent parse <gmail-message-id>      # what the LLM sees, with size/token estimate
uv run invoice-agent parse examples/emails/self_forwarded.eml
```

## Setup

```bash
uv sync                       # add --extra langfuse for tracing
cp .env.example .env          # OPENAI_API_KEY, TELEGRAM_BOT_TOKEN, GOOGLE_CLIENT_ID/SECRET
```

### Gmail (OAuth for the MCP server)

The KateChat Gmail MCP server forwards the `Authorization: Bearer <google access token>` to the Gmail API.
Access tokens expire after 1h, so the agent stores a refresh token and renews the access token itself:

1. Google Cloud Console → Gmail API enabled → OAuth client:
   * **Desktop app** (simplest, any localhost port works), or
   * reuse the KateChat **Web application** client and add `http://localhost:8765/callback` as an authorized redirect URI.
   * If the consent screen is in *Testing*, add your Google account as a test user.
2. `uv run invoice-agent auth` opens the consent page and saves `.data/google_token.json`.

### Telegram

Create a bot with @BotFather and set `TELEGRAM_BOT_TOKEN`. Start the daemon and send `/start` to the bot:
the first chat that does so gets linked (or set `TELEGRAM_CHAT_ID`). After that the bot ignores all other chats.

## Run

```bash
uv run invoice-agent run          # daemon: poll Gmail every 5 min, Telegram bot, reminders
uv run invoice-agent check        # scan once and exit (pending reviews survive in SQLite)
uv run invoice-agent demo examples/emails/magenta_iban_changed.json   # no Gmail/Telegram needed
uv run invoice-agent demo examples/emails/magenta_pdf_only.eml         # data only in the PDF attachment
uv run invoice-agent graph        # Mermaid diagram
```

Telegram commands: `/check` (scan now), `/pending` (resend open invoices).

## Tests and evals

```bash
uv run pytest                  # IBAN, checks, mail parsing, graph flow with a fake LLM, Telegram flow with a fake bot
uv run python evals/run.py     # real LLM on 11 labelled synthetic emails
```

```
case                     payreq  verdict       notify  risk    result
bad_iban_checksum        True    invalid       True    high    PASS
direct_debit             False   None          False   None    PASS
lookalike_domain         True    needs_review  True    medium  PASS
magenta_first_invoice    True    needs_review  True    medium  PASS
magenta_iban_changed     True    suspicious    True    high    PASS
magenta_invoice          True    payable       True    None    PASS   # known payee: supervisor skips investigation
magenta_pdf_attachment   True    payable       True    None    PASS   # data only in the PDF
newsletter               False   None          False   None    PASS
prompt_injection         True    needs_review  True    medium  PASS
receipt                  False   None          False   None    PASS
self_forwarded_invoice   True    payable       True    None    PASS   # judged by the original sender
11/11 passed (model: gpt-5.4-mini)
```

## Layout

```
src/invoice_agent/
  graph.py        state, supervisor, workers, build_graph
  checks.py       deterministic payability rules
  iban.py         ISO 13616 validation
  gmail.py        MCP client (read-only allowlist), raw message fetch
  mail_parser.py  MIME + docling-rs → compact Markdown, forward detection
  google_auth.py  loopback OAuth + PKCE, refresh tokens
  telegram.py     Bot API client, message + buttons
  app.py          polling, notifications queue, resume on button press
  cli.py          auth | run | check | demo | parse | graph
  samples.py      loads example cases (JSON or .eml)
evals/run.py      dataset eval
examples/emails/  synthetic test emails (Magenta-style invoice, phishing, newsletter, PDF-only, self-forward, …)
examples/make_eml.py  regenerates the .eml samples (uv run --with reportlab python examples/make_eml.py)
```
