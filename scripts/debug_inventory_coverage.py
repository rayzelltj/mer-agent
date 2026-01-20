from __future__ import annotations

import json
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

# Allow running as a script from repo root without installing the package.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.backend.v4.integrations.google_sheets_reader import GoogleSheetsReader
from src.backend.v4.integrations.qbo_client import QBOClient
from src.backend.v4.use_cases.mer_review_checks import pick_latest_month_header
from src.backend.v4.use_cases.mer_rule_engine import (
    MERBalanceSheetEvaluationContext,
    MERBalanceSheetRuleEngine,
)


def main() -> None:
    # --- Inputs ---
    client_maintenance_spreadsheet_id = "1WdGczVyMQ-ywEJHX_A1OeUzobuqyZV4seeEwlnRYkIQ"
    client_maintenance_sheet = "Account reconciliation list"
    client_maintenance_range = f"'{client_maintenance_sheet}'!A1:Z2000"

    mer_reader = GoogleSheetsReader.from_env()
    mer_titles = mer_reader.list_sheet_titles()
    mer_sheet = "Balance Sheet" if "Balance Sheet" in mer_titles else None
    if not mer_sheet:
        raise SystemExit(f"MER sheet 'Balance Sheet' not found. Available: {mer_titles}")

    mer_range = f"'{mer_sheet}'!A1:Z1000"
    mer_rows = mer_reader.fetch_rows(a1_range=mer_range)
    if not mer_rows:
        raise SystemExit("No MER rows returned")

    header_row_index: int | None = None
    selected_month: str | None = None
    for i, r in enumerate(mer_rows[:25]):
        candidate = pick_latest_month_header(r)
        if candidate:
            header_row_index = i
            selected_month = candidate
            break
    if header_row_index is None or selected_month is None:
        raise SystemExit("Could not detect MER month header in first 25 rows")

    cm_reader = GoogleSheetsReader.from_env_with_spreadsheet_id(client_maintenance_spreadsheet_id)
    cm_rows = cm_reader.fetch_rows(a1_range=client_maintenance_range)

    qbo = None
    qbo_error: str | None = None
    try:
        qbo = QBOClient.from_env()
    except Exception as e:
        qbo_error = f"{type(e).__name__}: {e}"

    # Minimal rulebook to run just the inventory check
    rulebook = {
        "rules": [
            {
                "rule_id": "BS-BANK-AND-CC-INVENTORY-COVERAGE",
                "title": "All bank/credit card accounts from maintenance sheet are included",
                "requires_external_sources": ["client_maintenance_kyc", "mer_google_sheet"],
                "evaluation": {"type": "inventory_accounts_must_exist_in_qbo_and_mer"},
            }
        ]
    }

    results = None
    if qbo is not None:
        ctx = MERBalanceSheetEvaluationContext(
            end_date=datetime.now().date().isoformat(),
            mer_rows=mer_rows,
            mer_selected_month_header=selected_month,
            mer_header_row_index=header_row_index,
            client_maintenance_rows=cm_rows,
            qbo_balance_sheet_items=[],
            qbo_client=qbo,
            zero_tolerance=Decimal("0.00"),  # unused by this check
            amount_match_tolerance=Decimal("0.00"),  # unused by this check
        )
        engine = MERBalanceSheetRuleEngine()
        results = engine.evaluate(rulebook=rulebook, ctx=ctx)

    # Always produce a transparent sheet-only extraction to unblock debugging.
    # This does NOT prove QBO coverage; it only shows what was read + MER representation.
    def _norm_name(s: str | None) -> str:
        return "".join(ch.lower() for ch in (s or "") if ch.isalnum())

    def _find_header_row(rows: list[list[str]]) -> int:
        for i, r in enumerate(rows[:25]):
            non_empty_cells = sum(1 for c in (r or []) if str(c or "").strip())
            if non_empty_cells < 2:
                continue
            joined = " ".join(str(c or "") for c in (r or []))
            nr = _norm_name(joined)
            if "account" in nr and ("type" in nr or "credit" in nr or "bank" in nr):
                return i
        return 0

    def _find_col(header: list[str], *needles: str) -> int | None:
        hn = [_norm_name(h) for h in header]
        nn = [_norm_name(n) for n in needles if _norm_name(n)]
        for idx, h in enumerate(hn):
            if not h:
                continue
            if all(n in h for n in nn):
                return idx
        return None

    cm_header_row_index = _find_header_row(cm_rows)
    cm_header = cm_rows[cm_header_row_index] if cm_rows else []
    qbo_name_col = (
        _find_col(cm_header, "account", "qbo")
        or _find_col(cm_header, "account", "xero")
        or _find_col(cm_header, "name", "qbo")
        or _find_col(cm_header, "name", "xero")
    )
    acct_col = qbo_name_col or _find_col(cm_header, "account") or _find_col(cm_header, "name")
    type_col = _find_col(cm_header, "type")

    extracted_inventory: list[dict[str, str | None]] = []
    if acct_col is not None:
        for r in cm_rows[cm_header_row_index + 1 :]:
            row = r or []
            acct = str(row[acct_col] if acct_col < len(row) else "").strip()
            if not acct:
                continue
            acct_type = str(row[type_col] if (type_col is not None and type_col < len(row)) else "").strip()
            extracted_inventory.append(
                {
                    "account_name": acct,
                    "account_type": acct_type,
                }
            )

    mer_start = header_row_index + 1
    mer_labels = [str((r or [""])[0] or "") for r in mer_rows[mer_start:]]
    sheet_only_findings: list[dict[str, str | bool | None]] = []
    for inv in extracted_inventory:
        name = str(inv.get("account_name") or "")
        needle = name.lower().strip()
        match = None
        if needle:
            for lbl in mer_labels:
                if needle in lbl.lower():
                    match = lbl
                    break
        sheet_only_findings.append(
            {
                "inventory_account_name": name,
                "inventory_account_type": str(inv.get("account_type") or ""),
                "represented_in_mer": bool(match),
                "matched_mer_line_label": match,
            }
        )
    out = {
        "inputs": {
            "client_maintenance_spreadsheet_id": client_maintenance_spreadsheet_id,
            "client_maintenance_range": client_maintenance_range,
            "mer_range": mer_range,
            "mer_selected_month_header": selected_month,
            "mer_header_row_index": header_row_index,
        },
        "qbo": {
            "configured": qbo is not None,
            "error": qbo_error,
            "note": "QBO must be configured (QBO_CLIENT_ID/QBO_CLIENT_SECRET + tokens) to compare inventory against Chart of Accounts.",
        },
        "client_maintenance": {
            "sheet_titles": cm_reader.list_sheet_titles(),
            "header_row_index": cm_header_row_index,
            "header": cm_header,
            "account_name_col_index": acct_col,
            "account_name_col_header": (cm_header[acct_col] if (acct_col is not None and acct_col < len(cm_header)) else None),
            "account_type_col_index": type_col,
            "sample_rows_top_10": cm_rows[:10],
            "extracted_inventory_count": len(extracted_inventory),
        },
        "mer": {
            "sheet_titles": mer_titles,
            "selected_sheet": mer_sheet,
            "header_row_index": header_row_index,
            "selected_month_header": selected_month,
            "header_row": mer_rows[header_row_index] if header_row_index < len(mer_rows) else None,
        },
        "sheet_only_findings": sheet_only_findings,
        "rule_engine_result": (results[0] if results else None),
    }

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = Path("tmp") / f"inventory_coverage_{ts}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")

    status = (results[0] or {}).get("status") if results else "needs_human_review"
    details = (results[0] or {}).get("details") if results else None
    print("Wrote:", path)
    print("Status:", status)
    if qbo_error:
        print("QBO not configured:", qbo_error)
        print("Sheet-only inventory count:", len(extracted_inventory))
        missing_mer = sum(1 for f in sheet_only_findings if not f.get("represented_in_mer"))
        print("Missing in MER (sheet-only check):", missing_mer)
    if isinstance(details, dict):
        print("inventory_count:", details.get("inventory_count"))
        print("missing_in_qbo_count:", details.get("missing_in_qbo_count"))
        print("missing_in_mer_count:", details.get("missing_in_mer_count"))
        print("account_name_col_header:", details.get("account_name_col_header"))


if __name__ == "__main__":
    main()
