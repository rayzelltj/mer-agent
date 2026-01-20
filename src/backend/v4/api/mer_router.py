"""MER Review API Router.

This module handles all MER (Month-End Review) related endpoints,
including balance sheet review checks driven by the YAML rulebook.
"""

import logging
import os
from datetime import date as _date
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.backend.v4.integrations.google_sheets_reader import GoogleSheetsReader
from src.backend.v4.integrations.qbo_client import QBOClient
from src.backend.v4.integrations.qbo_reports import extract_balance_sheet_items
from src.backend.v4.use_cases.mer_rule_engine import (
    MERBalanceSheetEvaluationContext,
    MERBalanceSheetRuleEngine,
    collect_action_items,
)
from src.backend.v4.use_cases.mer_review_checks import pick_latest_month_header

logger = logging.getLogger(__name__)

mer_router = APIRouter(tags=["MER Review"])


# ---------------------------------------------------------------------------
# Request/Response Models
# ---------------------------------------------------------------------------


class MERBalanceSheetReviewRequest(BaseModel):
    end_date: str
    mer_sheet: str | None = None
    mer_range: str | None = None
    mer_month_header: str | None = None
    rulebook_path: str | None = None
    mer_bank_row_key: str | None = None
    qbo_bank_label_substring: str | None = None
    client_maintenance_spreadsheet_id: str | None = None
    client_maintenance_sheet: str | None = None
    client_maintenance_range: str | None = None
    kyc_spreadsheet_id: str | None = None
    kyc_sheet: str | None = None
    kyc_range: str | None = None


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------


def _repo_root_from_this_file() -> Path:
    """Get repository root path from this file's location.

    mer_router.py is at: src/backend/v4/api/mer_router.py
    parents: api -> v4 -> backend -> src -> repo_root
    """
    return Path(__file__).resolve().parents[4]


def _load_rulebook_yaml(path: Path) -> dict[str, Any]:
    """Load and parse the MER rulebook YAML file."""
    if not path.exists():
        raise HTTPException(
            status_code=400,
            detail=f"Rulebook file not found: {path}",
        )
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        logger.error(f"Failed to load rulebook {path}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to parse rulebook YAML: {e}",
        )


def _decimal_from_rulebook_amount(amount_str: str | None) -> Decimal:
    """Parse a decimal amount from rulebook config (e.g., '0.00', '100.00')."""
    if not amount_str:
        return Decimal("0.00")
    try:
        return Decimal(str(amount_str).replace(",", "").strip())
    except Exception:
        return Decimal("0.00")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@mer_router.post("/mer/review/balance_sheet")
async def mer_review_balance_sheet(body: MERBalanceSheetReviewRequest):
    """Run MER Balance Sheet review checks driven by the YAML rulebook.

    This endpoint is intentionally deterministic + read-only:
    - Fetches data from QBO and Google Sheets
    - Executes implemented checks (currently the MVP checks)
    - Returns a structured JSON payload (does not edit MER)
    """

    # Validate end_date format early (YYYY-MM-DD)
    try:
        _date.fromisoformat(body.end_date)
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="end_date must be an ISO date (YYYY-MM-DD)",
        )

    repo_root = _repo_root_from_this_file()
    default_rulebook = (
        repo_root / "data" / "mer_rulebooks" / "balance_sheet_review_points.yaml"
    )
    rulebook_path = Path(body.rulebook_path) if body.rulebook_path else default_rulebook
    if not rulebook_path.is_absolute():
        rulebook_path = (repo_root / rulebook_path).resolve()

    rulebook = _load_rulebook_yaml(rulebook_path)

    policies = (rulebook.get("rulebook") or {}).get("policies") or {}
    tolerances = policies.get("tolerances") or {}
    zero_amount = (tolerances.get("zero_balance") or {}).get("amount")
    zero_tolerance = _decimal_from_rulebook_amount(zero_amount)

    amount_match_cfg = tolerances.get("amount_match") or {}
    amount_match_default = amount_match_cfg.get("default_amount")
    # Rulebook default is exact match (0.00) unless overridden.
    amount_match_tolerance = _decimal_from_rulebook_amount(amount_match_default)

    # Fetch MER sheet rows
    reader = GoogleSheetsReader.from_env()
    sheet = body.mer_sheet
    if not sheet:
        titles = reader.list_sheet_titles()
        if "Balance Sheet" in titles:
            sheet = "Balance Sheet"
        else:
            raise HTTPException(
                status_code=400,
                detail=f"mer_sheet is required. Available sheets: {titles}",
            )

    mer_range = body.mer_range or f"'{sheet}'!A1:Z1000"
    rows = reader.fetch_rows(a1_range=mer_range)
    if not rows:
        raise HTTPException(status_code=400, detail="No rows returned from Google Sheets")

    # Optional: Fetch Client Maintenance / KYC rows (used by inventory-based rules)
    client_maintenance_rows: list[list[str]] | None = None
    # Prefer explicit caller value, then environment override, then a provided default
    cm_spreadsheet_id = (
        body.client_maintenance_spreadsheet_id
        or os.environ.get("CLIENT_MAINTENANCE_SPREADSHEET_ID")
        or "1WdGczVyMQ-ywEJHX_A1OeUzobuqyZV4seeEwlnRYkIQ"
    )
    if cm_spreadsheet_id:
        cm_reader = GoogleSheetsReader.from_env_with_spreadsheet_id(cm_spreadsheet_id)
        # Default to the client's maintenance tab requested by the user
        cm_sheet = body.client_maintenance_sheet or "Account reconciliation list"
        cm_range = body.client_maintenance_range or f"'{cm_sheet}'!A1:Z1000"
        client_maintenance_rows = cm_reader.fetch_rows(a1_range=cm_range)

    # Optional: Fetch separate KYC rows (often a different spreadsheet than Client Maintenance)
    kyc_rows: list[list[str]] | None = None
    if body.kyc_spreadsheet_id:
        kyc_reader = GoogleSheetsReader.from_env_with_spreadsheet_id(body.kyc_spreadsheet_id)
        kyc_sheet = body.kyc_sheet
        kyc_range = body.kyc_range
        if not kyc_range:
            if not kyc_sheet:
                raise HTTPException(
                    status_code=400,
                    detail="kyc_range is required when kyc_sheet is not provided",
                )
            kyc_range = f"'{kyc_sheet}'!A1:Z2000"
        kyc_rows = kyc_reader.fetch_rows(a1_range=kyc_range)

    # Identify the month header to use
    header_row_index: int | None = None
    selected_month = body.mer_month_header
    if not selected_month:
        for i, r in enumerate(rows[:25]):
            candidate = pick_latest_month_header(r)
            if candidate:
                header_row_index = i
                selected_month = candidate
                break
        if selected_month is None or header_row_index is None:
            raise HTTPException(
                status_code=400,
                detail="Could not find a month header row in the first 25 rows (or parse latest month)",
            )
    else:
        # If caller provides a month header, we still need a header row index.
        for i, r in enumerate(rows[:25]):
            if any((c or "").strip() for c in r):
                header_row_index = i
                break
        if header_row_index is None:
            raise HTTPException(status_code=400, detail="Could not detect header row")

    # Fetch QBO Balance Sheet items
    qbo = QBOClient.from_env()
    report = qbo.get_balance_sheet(
        end_date=body.end_date,
        start_date=body.end_date,
        accounting_method=None,
        date_macro=None,
    )
    qbo_items = extract_balance_sheet_items(report)

    engine = MERBalanceSheetRuleEngine()
    ctx = MERBalanceSheetEvaluationContext(
        end_date=body.end_date,
        mer_rows=rows,
        mer_selected_month_header=selected_month,
        mer_header_row_index=header_row_index,
        client_maintenance_rows=client_maintenance_rows,
        kyc_rows=kyc_rows,
        qbo_balance_sheet_items=qbo_items,
        qbo_client=qbo,
        zero_tolerance=zero_tolerance,
        amount_match_tolerance=amount_match_tolerance,
        mer_bank_row_key=body.mer_bank_row_key,
        qbo_bank_label_substring=body.qbo_bank_label_substring,
    )
    results = engine.evaluate(rulebook=rulebook, ctx=ctx)

    logger.info(
        f"MER review completed: {len(results)} rules evaluated for period {body.end_date}"
    )

    return {
        "rulebook": {
            "id": (rulebook.get("rulebook") or {}).get("id"),
            "version": (rulebook.get("rulebook") or {}).get("version"),
            "path": str(rulebook_path),
        },
        "period_end_date": body.end_date,
        "mer": {
            "spreadsheet_id": reader.spreadsheet_id,
            "sheet": sheet,
            "range": mer_range,
            "selected_month_header": selected_month,
            "header_row_index": header_row_index,
        },
        "qbo": {
            "balance_sheet_items_extracted": len(qbo_items),
        },
        "policies": {
            "zero_tolerance": str(zero_tolerance),
            "amount_match_tolerance": str(amount_match_tolerance),
            "amount_match_requires_clarification": bool(
                (tolerances.get("amount_match") or {}).get("requires_clarification")
            ),
        },
        "requires_clarification": (rulebook.get("rulebook") or {}).get(
            "requires_clarification", []
        ),
        "action_items": collect_action_items(rulebook),
        "results": results,
    }
