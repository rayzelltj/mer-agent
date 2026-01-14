"""Shared backend client for MER review.

This module is intentionally dependency-light so it can be used by:
- the MCP server tool implementation
- local scripts/tests that want to call the same backend endpoint

It does NOT import FastMCP / MCPToolBase.
"""

from __future__ import annotations

import os
from typing import Any

import httpx


async def call_mer_balance_sheet_review_backend(
    *,
    end_date: str,
    mer_sheet: str | None = None,
    mer_range: str | None = None,
    mer_month_header: str | None = None,
    mer_bank_row_key: str | None = None,
    qbo_bank_label_substring: str | None = None,
    rulebook_path: str | None = None,
    backend_base_url: str | None = None,
) -> dict[str, Any]:
    base = (
        backend_base_url
        or os.environ.get("MER_REVIEW_BACKEND_BASE_URL")
        or "http://127.0.0.1:8000/api/v4"
    ).rstrip("/")

    url = f"{base}/mer/review/balance_sheet"
    payload: dict[str, Any] = {
        "end_date": end_date,
        "mer_sheet": mer_sheet,
        "mer_range": mer_range,
        "mer_month_header": mer_month_header,
        "mer_bank_row_key": mer_bank_row_key,
        "qbo_bank_label_substring": qbo_bank_label_substring,
        "rulebook_path": rulebook_path,
    }

    payload = {k: v for k, v in payload.items() if v is not None}
    timeout = float(os.environ.get("MER_REVIEW_HTTP_TIMEOUT_SECONDS", "60"))

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload)

    if resp.status_code >= 400:
        return {
            "ok": False,
            "status_code": resp.status_code,
            "error": resp.text,
            "request": {"url": url, "payload": payload},
        }

    return {
        "ok": True,
        "status_code": resp.status_code,
        "request": {"url": url, "payload": payload},
        "response": resp.json(),
    }
