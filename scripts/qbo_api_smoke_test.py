"""Smoke test: call a couple of QuickBooks Online APIs using saved OAuth tokens.

Prereqs:
- Run `python scripts/qbo_auth_local.py` first (creates `.env_qbo_tokens.json`)

Env vars:
- QBO_CLIENT_ID
- QBO_CLIENT_SECRET
- QBO_ENVIRONMENT (sandbox|production) [default: sandbox]
- QBO_TOKENS_PATH (optional)           [default: .env_qbo_tokens.json]

Optional (for Balance Sheet report):
- QBO_REPORT_END_DATE=YYYY-MM-DD

Run:
  python scripts/qbo_api_smoke_test.py
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Iterable

from dotenv import load_dotenv
from src.backend.v4.integrations.qbo_client import QBOClient

from src.backend.v4.integrations.qbo_reports import (
    extract_balance_sheet_items,
    find_first_amount,
)

load_dotenv()

# Convenience: allow loading credentials from `.env.example` if `.env` is missing.
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


def _load_tokens() -> dict[str, Any]:
    if not os.path.exists(TOKENS_PATH):
        raise SystemExit(
            f"Token file not found: {TOKENS_PATH}. Run `python scripts/qbo_auth_local.py` first."
        )
    with open(TOKENS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def main() -> None:
    _load_tokens()  # ensure file exists; connector loads it too
    qbo = QBOClient.from_env()

    print("Calling CompanyInfo...")
    company = qbo.get_company_info()

    company_info = (company.get("CompanyInfo") or {})
    print("✅ Company:", company_info.get("CompanyName"), "|", company_info.get("Id"))

    end_date = os.environ.get("QBO_REPORT_END_DATE")
    if end_date:
        print(f"\nCalling BalanceSheet report for end_date={end_date}...")
        report = qbo.get_balance_sheet(end_date=end_date)

        items = extract_balance_sheet_items(report)
        print(f"✅ Parsed {len(items)} balance sheet line items")

        for key in ["Undeposited Funds", "Petty Cash", "Clearing"]:
            amt = find_first_amount(items, key)
            print(f"{key}: {amt if amt is not None else '[not found in report]'}")

        print("\nTip: set QBO_REPORT_END_DATE=YYYY-MM-DD to match your MER period end.")

    else:
        print(
            "\nSkipped BalanceSheet report (set QBO_REPORT_END_DATE=YYYY-MM-DD to enable)."
        )


if __name__ == "__main__":
    main()
