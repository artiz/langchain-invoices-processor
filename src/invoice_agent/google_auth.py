"""Google OAuth2 for the Gmail MCP server.

The KateChat Gmail MCP server expects a raw Google access token as
``Authorization: Bearer <token>``. Access tokens live ~1h, so we run a one-time
loopback OAuth flow (``invoice-agent auth``), keep the refresh token locally and
renew access tokens on demand.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
# Least privilege: the agent only reads mail, so it never gets send/compose scopes.
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class GoogleAuthError(RuntimeError):
    pass


class GoogleTokenProvider:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        token_file: Path,
        refresh_token: str | None = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_file = token_file
        self._env_refresh_token = refresh_token
        self._token: dict = json.loads(token_file.read_text()) if token_file.exists() else {}

    async def get_access_token(self) -> str:
        if self._token.get("access_token") and self._token.get("expires_at", 0) > time.time() + 60:
            return self._token["access_token"]
        refresh_token = self._env_refresh_token or self._token.get("refresh_token")
        if not refresh_token:
            raise GoogleAuthError("No Google refresh token. Run `uv run invoice-agent auth` first.")
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )
        if resp.status_code != 200:
            raise GoogleAuthError(f"Token refresh failed ({resp.status_code}): {resp.text}")
        data = resp.json()
        self._token.update(
            access_token=data["access_token"],
            expires_at=time.time() + int(data.get("expires_in", 3600)),
            refresh_token=data.get("refresh_token", refresh_token),
        )
        _save(self.token_file, self._token)
        return self._token["access_token"]


def _save(path: Path, token: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(token, indent=2))
    path.chmod(0o600)


def run_auth_flow(client_id: str, client_secret: str, token_file: Path, port: int) -> None:
    """Interactive loopback OAuth flow with PKCE. Stores the refresh token in ``token_file``."""
    redirect_uri = f"http://localhost:{port}/callback"
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    url = (
        AUTH_URL
        + "?"
        + urllib.parse.urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": " ".join(SCOPES),
                "access_type": "offline",
                "prompt": "consent",
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
    )

    result: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            params = dict(urllib.parse.parse_qsl(parsed.query))
            result.update(params)
            ok = "code" in params and params.get("state") == state
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = "Authorized. You can close this tab." if ok else f"Authorization failed: {params}"
            self.wfile.write(f"<h3>{msg}</h3>".encode())

        def log_message(self, *args) -> None:
            pass

    print(f"Redirect URI (must be allowed in your Google OAuth client): {redirect_uri}")
    print(f"\nOpen this URL to authorize Gmail read access:\n\n{url}\n")
    webbrowser.open(url)
    with HTTPServer(("localhost", port), Handler) as server:
        while "code" not in result and "error" not in result:
            server.handle_request()

    if result.get("state") != state or "code" not in result:
        raise GoogleAuthError(f"Authorization failed: {result.get('error', 'state mismatch')}")

    resp = httpx.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": result["code"],
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=20,
    )
    if resp.status_code != 200:
        raise GoogleAuthError(f"Code exchange failed ({resp.status_code}): {resp.text}")
    data = resp.json()
    if "refresh_token" not in data:
        raise GoogleAuthError("Google returned no refresh token; revoke app access and retry.")
    _save(
        token_file,
        {
            "access_token": data["access_token"],
            "expires_at": time.time() + int(data.get("expires_in", 3600)),
            "refresh_token": data["refresh_token"],
        },
    )
    print(f"Saved Google token to {token_file}")
