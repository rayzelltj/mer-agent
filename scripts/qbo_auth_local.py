"""Minimal local OAuth2 (3-legged) flow for QuickBooks Online.

What this does:
- Starts a tiny local HTTP server on your redirect URI
- Opens the Intuit consent page in your browser
- Captures the auth `code` + `realmId` on the callback
- Exchanges `code` for access/refresh tokens
- Saves tokens to `.env_qbo_tokens.json` (ignored by this repo's .gitignore)

Prereqs (env vars):
- QBO_CLIENT_ID
- QBO_CLIENT_SECRET
- QBO_REDIRECT_URI         (must exactly match what's configured in Intuit Developer)
- QBO_ENVIRONMENT          (sandbox | production)  [default: sandbox]

Optional (recommended for production local testing):
- QBO_LOCAL_REDIRECT_URI   Local listener URI for the callback server.
    Use this if QBO_REDIRECT_URI is a public HTTPS URL (e.g., via ngrok) but you
    still want this script to listen on localhost.

    Example (production-friendly):
        QBO_REDIRECT_URI=https://<your-subdomain>.ngrok-free.app/qbo/callback
        QBO_LOCAL_REDIRECT_URI=http://localhost:8040/qbo/callback

Run:
  python scripts/qbo_auth_local.py
"""

from __future__ import annotations

import json
import os
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from intuitlib.client import AuthClient
from intuitlib.enums import Scopes

load_dotenv()

# Convenience: if the user only filled `.env.example` (recommended: copy to `.env`),
# fall back to loading it so local scripts still run.
if not os.environ.get("QBO_CLIENT_ID"):
    load_dotenv(dotenv_path=os.path.abspath(".env.example"), override=False)

TOKENS_PATH = os.environ.get("QBO_TOKENS_PATH", os.path.abspath(".env_qbo_tokens.json"))


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(
            f"Missing env var {name}. Put it in your .env/.env.example and export it before running."
        )
    return value


class _CallbackState:
    def __init__(self) -> None:
        self.code: str | None = None
        self.realm_id: str | None = None
        self.error: str | None = None


def main() -> None:
    client_id = _require_env("QBO_CLIENT_ID")
    client_secret = _require_env("QBO_CLIENT_SECRET")
    redirect_uri = _require_env("QBO_REDIRECT_URI")
    local_redirect_uri = os.environ.get("QBO_LOCAL_REDIRECT_URI") or redirect_uri
    environment = os.environ.get("QBO_ENVIRONMENT", "sandbox")

    parsed = urlparse(redirect_uri)
    if parsed.scheme not in {"http", "https"}:
        raise SystemExit("QBO_REDIRECT_URI must start with http:// or https://")

    local_parsed = urlparse(local_redirect_uri)
    if local_parsed.scheme not in {"http", "https"}:
        raise SystemExit("QBO_LOCAL_REDIRECT_URI must start with http:// or https://")
    if not local_parsed.hostname or not local_parsed.port:
        raise SystemExit(
            "QBO_LOCAL_REDIRECT_URI must include hostname and port, e.g. http://localhost:8040/qbo/callback"
        )

    state = _CallbackState()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            # Only accept the configured callback path
            if urlparse(self.path).path != local_parsed.path:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Not found")
                return

            query = parse_qs(urlparse(self.path).query)
            if "error" in query:
                state.error = query.get("error", [""])[0]
            state.code = query.get("code", [None])[0]
            state.realm_id = query.get("realmId", [None])[0]

            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h3>QBO connected.</h3><p>You can close this tab and return to the terminal.</p></body></html>"
            )

        def log_message(self, *_args, **_kwargs):
            # Quiet default HTTP server logging
            return

    server = HTTPServer((local_parsed.hostname, local_parsed.port), Handler)

    def run_server():
        server.serve_forever(poll_interval=0.1)

    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()

    auth_client = AuthClient(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        environment=environment,
    )

    scopes = [Scopes.ACCOUNTING]
    auth_url = auth_client.get_authorization_url(scopes)

    print("\n1) Opening Intuit consent page in your browser...")
    print("   If it doesn't open, copy/paste this URL:")
    print(auth_url)

    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    print("\n2) After you click 'Connect', you will be redirected back to:")
    print(f"   {redirect_uri}")
    if local_redirect_uri != redirect_uri:
        print("   (This script is listening locally on:")
        print(f"    {local_redirect_uri} )")
    print("   Waiting for callback...")

    timeout_s = int(os.environ.get("QBO_AUTH_TIMEOUT_SECONDS", "180"))
    start = time.time()
    while time.time() - start < timeout_s:
        if state.error:
            server.shutdown()
            raise SystemExit(f"OAuth error: {state.error}")
        if state.code and state.realm_id:
            break
        time.sleep(0.1)

    server.shutdown()

    if not state.code or not state.realm_id:
        raise SystemExit(
            "Timed out waiting for OAuth callback. Check that your Redirect URI in Intuit Developer Portal matches QBO_REDIRECT_URI exactly."
        )

    print("\n3) Exchanging auth code for tokens...")
    auth_client.get_bearer_token(state.code, realm_id=state.realm_id)

    payload = {
        "environment": environment,
        "realm_id": auth_client.realm_id,
        "access_token": auth_client.access_token,
        "refresh_token": auth_client.refresh_token,
        "id_token": auth_client.id_token,
        "saved_at_unix": int(time.time()),
    }

    with open(TOKENS_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    print("\n✅ Success. Tokens saved to:")
    print(f"   {TOKENS_PATH}")
    print("\nNext: run the smoke test:")
    print("  python scripts/qbo_api_smoke_test.py")


if __name__ == "__main__":
    main()
