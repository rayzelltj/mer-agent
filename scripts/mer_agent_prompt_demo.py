"""Local demo: prompt-like MER Balance Sheet review.

This is a lightweight, offline-friendly way to exercise the same backend call
used by the MCP tool (`mer_balance_sheet_review`) without requiring an Azure
Foundry agent runtime.

It:
- Extracts an `YYYY-MM-DD` date from a prompt (or takes --end-date)
- Calls the backend endpoint via the shared MCP-tool helper
- Prints a reviewer-style summary (never edits the MER sheet)

Usage:
  .venv_backend/bin/python scripts/mer_agent_prompt_demo.py --prompt "Review MER balance sheet for 2025-11-30"
  .venv_backend/bin/python scripts/mer_agent_prompt_demo.py --end-date 2025-11-30 --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _bootstrap_mcp_server_imports() -> None:
    # mer_review_service.py uses absolute imports like `from core.factory ...`
    # which assume `src/mcp_server` is on sys.path.
    mcp_server_root = _repo_root() / "src" / "mcp_server"
    sys.path.insert(0, str(mcp_server_root))


def _extract_iso_date(prompt: str) -> str | None:
    m = re.search(r"\b\d{4}-\d{2}-\d{2}\b", prompt)
    return m.group(0) if m else None


def _format_summary(tool_result: dict[str, Any]) -> str:
    if not tool_result.get("ok"):
        err = tool_result.get("error") or "Unknown error"
        req = tool_result.get("request") or {}
        return (
            "MER review failed.\n"
            f"- HTTP: {tool_result.get('status_code')}\n"
            f"- URL: {req.get('url')}\n"
            f"- Error: {err}\n"
        )

    payload = tool_result.get("response") or {}
    period_end = payload.get("period_end_date")
    mer = payload.get("mer") or {}
    qbo = payload.get("qbo") or {}
    results = payload.get("results") or []
    clarifications = payload.get("requires_clarification") or []

    failed = [r for r in results if r.get("status") == "failed"]
    unimplemented = [r for r in results if r.get("status") == "unimplemented"]
    passed = [r for r in results if r.get("status") == "passed"]
    skipped = [r for r in results if r.get("status") == "skipped"]

    lines: list[str] = []
    lines.append(f"MER Balance Sheet review (read-only) — period end {period_end}")
    lines.append(f"- MER: sheet={mer.get('sheet')} month={mer.get('selected_month_header')}")
    lines.append(f"- QBO: balance_sheet_items_extracted={qbo.get('balance_sheet_items_extracted')}")
    lines.append(f"- Results: failed={len(failed)} passed={len(passed)} skipped={len(skipped)} unimplemented={len(unimplemented)}")

    if failed:
        lines.append("\nFailed checks:")
        for r in failed[:10]:
            rid = r.get("rule_id")
            et = r.get("evaluation_type")
            details = r.get("details") or {}
            msg = details.get("reason") or details.get("rule") or ""
            lines.append(f"- {rid} ({et}): {msg}")

    if clarifications:
        lines.append("\nRequires clarification (to avoid assumptions):")
        for c in clarifications[:10]:
            lines.append(f"- {c.get('id')}: {c.get('question')}")

    lines.append("\nNotes:")
    lines.append("- This demo calls the same backend request shape as the MCP tool `mer_balance_sheet_review`.")
    lines.append("- It does not edit the MER sheet and does not invent tolerances.")

    return "\n".join(lines) + "\n"


async def _run(end_date: str, backend_base_url: str | None, show_json: bool) -> int:
    _bootstrap_mcp_server_imports()
    from services.mer_review_backend_client import (  # type: ignore
        call_mer_balance_sheet_review_backend,
    )

    tool_result = await call_mer_balance_sheet_review_backend(
        end_date=end_date,
        backend_base_url=backend_base_url,
    )

    print(_format_summary(tool_result))

    if show_json:
        print(json.dumps(tool_result, indent=2, sort_keys=True))

    return 0 if tool_result.get("ok") else 2


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--prompt", type=str, default=None)
    p.add_argument("--end-date", type=str, default=None)
    p.add_argument(
        "--backend-base-url",
        type=str,
        default=None,
        help="Override backend base URL (default http://127.0.0.1:8000/api/v4)",
    )
    p.add_argument("--json", action="store_true", help="Print raw JSON tool result")
    args = p.parse_args()

    end_date = args.end_date
    if not end_date and args.prompt:
        end_date = _extract_iso_date(args.prompt)

    if not end_date:
        raise SystemExit(
            "Provide either --end-date YYYY-MM-DD or --prompt containing YYYY-MM-DD"
        )

    return asyncio.run(_run(end_date, args.backend_base_url, args.json))


if __name__ == "__main__":
    raise SystemExit(main())
