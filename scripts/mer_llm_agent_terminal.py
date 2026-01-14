#!/usr/bin/env python3

import argparse
import json
import os
import ssl
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests


DEFAULT_SITE_URL = "https://app-meragentprjk3.azurewebsites.net"
DEFAULT_TEAM_FILE = os.path.join("data", "agent_teams", "mer_review.json")
DEFAULT_TEAM_ID = "mer-review-team"
DEFAULT_USER_ID = "00000000-0000-0000-0000-000000000000"


@dataclass(frozen=True)
class Config:
    api_url: str
    user_id: str
    team_file: str
    team_id: str
    insecure_ws: bool


def _strip_trailing_slash(url: str) -> str:
    return url[:-1] if url.endswith("/") else url


def resolve_api_url(site_url: str) -> str:
    site_url = _strip_trailing_slash(site_url)
    r = requests.get(f"{site_url}/config", timeout=30)
    r.raise_for_status()
    data = r.json()
    api_url = data.get("API_URL")
    if not api_url:
        raise RuntimeError(f"/config did not return API_URL. Response: {data}")
    return _strip_trailing_slash(str(api_url))


def _headers(user_id: str) -> Dict[str, str]:
    return {
        # Backend uses EasyAuth header; if absent it falls back to sample_user.
        "x-ms-client-principal-id": user_id,
    }


def upload_team_config(cfg: Config) -> Dict[str, Any]:
    if not os.path.exists(cfg.team_file):
        raise FileNotFoundError(f"Team config not found: {cfg.team_file}")

    url = f"{cfg.api_url}/v4/upload_team_config"
    with open(cfg.team_file, "rb") as f:
        files = {"file": (os.path.basename(cfg.team_file), f, "application/json")}
        r = requests.post(
            url,
            params={"team_id": cfg.team_id},
            files=files,
            headers=_headers(cfg.user_id),
            timeout=60,
        )
    if r.status_code >= 400:
        raise RuntimeError(f"upload_team_config failed: HTTP {r.status_code} {r.text}")
    return r.json() if r.headers.get("content-type", "").startswith("application/json") else {"raw": r.text}


def select_team(cfg: Config) -> Dict[str, Any]:
    url = f"{cfg.api_url}/v4/select_team"
    r = requests.post(
        url,
        json={"team_id": cfg.team_id},
        headers={**_headers(cfg.user_id), "content-type": "application/json"},
        timeout=30,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"select_team failed: HTTP {r.status_code} {r.text}")
    return r.json() if r.headers.get("content-type", "").startswith("application/json") else {"raw": r.text}


def init_team(cfg: Config, team_switched: bool = True) -> Dict[str, Any]:
    url = f"{cfg.api_url}/v4/init_team"
    r = requests.get(
        url,
        params={"team_switched": bool(team_switched)},
        headers=_headers(cfg.user_id),
        timeout=30,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"init_team failed: HTTP {r.status_code} {r.text}")
    return r.json()


def process_request(cfg: Config, prompt: str, session_id: Optional[str]) -> Dict[str, Any]:
    url = f"{cfg.api_url}/v4/process_request"
    payload = {
        "session_id": session_id or str(uuid.uuid4()),
        "description": prompt,
    }
    r = requests.post(
        url,
        json=payload,
        headers={**_headers(cfg.user_id), "content-type": "application/json"},
        timeout=60,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"process_request failed: HTTP {r.status_code} {r.text}")
    return r.json()


def _to_ws_url(api_url: str, plan_id: str, user_id: str) -> str:
    # api_url already includes /api, matching frontend config.
    base = api_url
    if base.startswith("https://"):
        base = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://") :]

    # Frontend uses '/v4/socket' when api_url contains '/api'.
    return f"{_strip_trailing_slash(base)}/v4/socket/{plan_id}?user_id={user_id}"


def stream_via_websocket(cfg: Config, plan_id: str, timeout_s: int) -> int:
    try:
        import websockets
    except Exception as e:
        print("websockets package is missing:", e, file=sys.stderr)
        return 2

    ws_url = _to_ws_url(cfg.api_url, plan_id, cfg.user_id)
    print(f"\n[ws] connecting: {ws_url}")

    ssl_ctx = None
    if ws_url.startswith("wss://"):
        ssl_ctx = ssl.create_default_context()
        if cfg.insecure_ws:
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

    start = time.time()

    async def _run() -> int:
        final_seen = False
        try:
            async with websockets.connect(ws_url, ssl=ssl_ctx, open_timeout=20) as ws:
                print("[ws] connected")
                while True:
                    if time.time() - start > timeout_s:
                        print("[ws] timeout waiting for final message")
                        return 3
                    try:
                        raw = await ws.recv()
                    except Exception as e:
                        print(f"[ws] recv error: {e}")
                        return 4

                    try:
                        msg = json.loads(raw)
                    except Exception:
                        print("[ws] non-json:", raw)
                        continue

                    mtype = msg.get("type") or msg.get("message_type")
                    data = msg.get("data") if isinstance(msg.get("data"), dict) else msg

                    if mtype == "agent_message_streaming":
                        chunk = (data or {}).get("content")
                        if chunk:
                            print(chunk, end="", flush=True)
                        continue

                    if mtype == "agent_stream_start":
                        agent = (data or {}).get("agent_name")
                        print(f"\n\n[{agent or 'agent'}] ", end="", flush=True)
                        continue

                    if mtype == "agent_message":
                        agent = (data or {}).get("agent_name")
                        content = (data or {}).get("content")
                        if content:
                            print(f"\n\n[{agent or 'agent'}]\n{content}")
                        continue

                    if mtype == "agent_tool_message":
                        agent = (data or {}).get("agent_name")
                        calls = (data or {}).get("tool_calls")
                        print(f"\n\n[{agent or 'agent'}] tool calls: {json.dumps(calls, indent=2)}")
                        continue

                    if mtype == "user_clarification_request":
                        q = (data or {}).get("question")
                        rid = (data or {}).get("request_id")
                        print(f"\n\n[clarification requested] request_id={rid}\n{q}")
                        continue

                    if mtype == "error_message":
                        print(f"\n\n[error] {data}")
                        continue

                    if mtype == "final_result_message":
                        content = (data or {}).get("content")
                        summary = (data or {}).get("summary")
                        print("\n\n[final]")
                        if summary:
                            print(summary)
                        if content:
                            print(content)
                        final_seen = True
                        return 0

                    # Fallback: show raw type
                    if mtype:
                        print(f"\n\n[{mtype}] {json.dumps(msg)[:800]}")

        except ssl.SSLCertVerificationError as e:
            print("\n[ws] TLS certificate verification failed:")
            print(f"  {e}")
            print("\nIf you trust this endpoint, re-run with: --insecure-ws")
            return 5
        except Exception as e:
            print(f"\n[ws] connect failed: {repr(e)}")
            if not cfg.insecure_ws and "CERTIFICATE_VERIFY_FAILED" in str(e):
                print("\nIf you trust this endpoint, re-run with: --insecure-ws")
            return 6
        finally:
            if not final_seen:
                print("\n[ws] ended without final result")

    import asyncio

    return asyncio.run(_run())


def main() -> int:
    ap = argparse.ArgumentParser(
        description="LLM-powered agent runner (terminal) against deployed MACAE backend."
    )
    ap.add_argument(
        "--site-url",
        default=os.environ.get("MACAE_SITE_URL", DEFAULT_SITE_URL),
        help="Frontend site URL (used to fetch /config if --api-url not provided)",
    )
    ap.add_argument(
        "--api-url",
        default=os.environ.get("MACAE_API_URL"),
        help="Backend API base URL (should end with /api). If omitted, derived from --site-url /config.",
    )
    ap.add_argument(
        "--user-id",
        default=os.environ.get("MACAE_USER_ID", DEFAULT_USER_ID),
        help="Value for x-ms-client-principal-id header.",
    )
    ap.add_argument(
        "--team-file",
        default=os.environ.get("MACAE_TEAM_FILE", DEFAULT_TEAM_FILE),
        help="Path to team config JSON to upload.",
    )
    ap.add_argument(
        "--team-id",
        default=os.environ.get("MACAE_TEAM_ID", DEFAULT_TEAM_ID),
        help="Team ID to select.",
    )
    ap.add_argument(
        "--no-upload-team",
        action="store_true",
        help="Skip uploading the team config (assumes it already exists).",
    )
    ap.add_argument(
        "--prompt",
        required=True,
        help="Prompt to send to the agent (LLM decides tool calls server-side).",
    )
    ap.add_argument(
        "--session-id",
        default=None,
        help="Optional session_id. If omitted, a UUID is generated.",
    )
    ap.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="Seconds to wait for a final result over websocket.",
    )
    ap.add_argument(
        "--insecure-ws",
        action="store_true",
        help="Disable TLS cert verification for wss:// websocket (use only if you trust the endpoint).",
    )

    args = ap.parse_args()

    api_url = args.api_url
    if not api_url:
        print(f"[cfg] resolving api_url from {args.site_url}/config")
        api_url = resolve_api_url(args.site_url)

    cfg = Config(
        api_url=_strip_trailing_slash(api_url),
        user_id=args.user_id,
        team_file=args.team_file,
        team_id=args.team_id,
        insecure_ws=bool(args.insecure_ws),
    )

    print(f"[cfg] api_url={cfg.api_url}")
    print(f"[cfg] user_id={cfg.user_id}")
    print(f"[cfg] team_id={cfg.team_id}")

    if not args.no_upload_team:
        print(f"\n[http] uploading team config: {cfg.team_file}")
        upload_team_config(cfg)

    print("[http] selecting team")
    select_team(cfg)

    print("[http] init_team")
    init_resp = init_team(cfg, team_switched=True)
    print(f"[http] init_team -> team name: {(init_resp.get('team') or {}).get('name')}")

    print("\n[http] process_request")
    pr = process_request(cfg, prompt=args.prompt, session_id=args.session_id)
    plan_id = pr.get("plan_id")
    if not plan_id:
        print(f"process_request did not return plan_id: {pr}", file=sys.stderr)
        return 1

    print(f"[http] plan_id={plan_id}")

    return stream_via_websocket(cfg, plan_id=plan_id, timeout_s=args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
