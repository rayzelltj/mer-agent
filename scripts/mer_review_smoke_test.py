"""Smoke test: MER (Google Sheets) vs QBO Balance Sheet matching.

This prints:
- Which month column was selected (latest MER month)
- The exact cell/value pulled from Google Sheets
- The label/value pulled from QBO Balance Sheet
- The UC-04 petty-cash match result

It is intentionally a script (not an agent) so you can easily cross-reference
values against your Google Sheet and QBO sandbox.
"""

from __future__ import annotations

import os
import sys

# Allow running as: `python scripts/mer_review_smoke_test.py`
# by ensuring the repository root (parent of `scripts/`) is on sys.path.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from dotenv import load_dotenv

# Load .env as early as possible (before importing other modules that may
# also load dotenv and/or fall back to .env.example).
load_dotenv(override=False)

from src.backend.v4.integrations.google_sheets_reader import (
    GoogleSheetsReader,
    find_value_in_table,
    find_values_for_rows_containing,
)
from src.backend.v4.integrations.qbo_client import QBOClient
from src.backend.v4.integrations.qbo_reports import (
    extract_balance_sheet_items,
    find_first_amount,
)
from src.backend.v4.use_cases.mer_review_checks import (
    check_clearing_accounts_zero,
    check_undeposited_funds_zero,
    check_bank_balance_matches,
    check_petty_cash_matches,
    check_zero_on_both_sides_by_substring,
    parse_money,
    pick_latest_month_header,
)

if not os.environ.get("SPREADSHEET_ID"):
    load_dotenv(dotenv_path=os.path.abspath(".env.example"), override=False)


def main() -> int:
    end_date = os.environ.get("QBO_REPORT_END_DATE")
    if not end_date:
        print(
            "ERROR: QBO_REPORT_END_DATE is required for this smoke test (e.g. 2025-11-30).",
            file=sys.stderr,
        )
        return 2

    start_date = os.environ.get("QBO_REPORT_START_DATE")
    accounting_method = os.environ.get("QBO_REPORT_ACCOUNTING_METHOD")
    date_macro = os.environ.get("QBO_REPORT_DATE_MACRO")

    # For Balance Sheet checks, we only need the as-of end_date.
    # If the caller didn't provide a start_date, set it to end_date to avoid
    # confusing report headers while not changing the as-of nature of BS.
    if not start_date:
        start_date = end_date

    # Google Sheets inputs
    reader = GoogleSheetsReader.from_env()
    sheet = os.environ.get("SEARCH_SHEET")
    search_range = os.environ.get("SEARCH_RANGE")

    # Prefer explicit sheet selection, but default to "Balance Sheet" if present.
    if not sheet:
        titles = reader.list_sheet_titles()
        if "Balance Sheet" in titles:
            sheet = "Balance Sheet"
            print('SEARCH_SHEET not set; defaulting to "Balance Sheet"')
        else:
            print(
                "ERROR: SEARCH_SHEET is required (so we read the correct MER sheet tab).",
                file=sys.stderr,
            )
            print(f"Available sheets: {titles}")
            print(
                'Set it via env, e.g. `export SEARCH_SHEET="<sheet name>"`',
                file=sys.stderr,
            )
            return 3

    search_range = search_range or f"'{sheet}'!A1:Z1000"

    row_key = os.environ.get("MER_ROW_KEY", "Petty Cash")

    # Fetch table
    print("=== Google Sheets ===")
    print(f"SPREADSHEET_ID: {reader.spreadsheet_id}")
    print(f"RANGE: {search_range}")
    rows = reader.fetch_rows(a1_range=search_range)
    print(f"Fetched rows: {len(rows)}")

    if not rows:
        print("ERROR: No rows returned from Sheets range.", file=sys.stderr)
        return 4

    # Find the header row that actually contains month columns.
    header_row = None
    header_row_index = None
    for i, r in enumerate(rows[:25]):
        if pick_latest_month_header(r):
            header_row = r
            header_row_index = i
            break

    if header_row is None or header_row_index is None:
        print(
            "ERROR: Could not find a month header row in the first 25 rows.",
            file=sys.stderr,
        )
        print(f"First 25 rows preview: {rows[:25]}")
        return 5

    latest_month = pick_latest_month_header(header_row)
    if not latest_month:
        print(
            "ERROR: Could not infer latest month header from the header row.",
            file=sys.stderr,
        )
        print(f"Header row: {header_row}")
        return 6

    print(f"Header row index (month headers): {header_row_index}")
    print(f"Latest month header (selected): {latest_month}")

    # Pull MER values needed for UC-01/UC-03 (by substring) for the selected month.
    mer_undeposited_matches = find_values_for_rows_containing(
        rows=rows,
        row_substring="undeposited",
        col_header=latest_month,
        header_row_index=header_row_index,
    )
    mer_clearing_matches = find_values_for_rows_containing(
        rows=rows,
        row_substring="clearing",
        col_header=latest_month,
        header_row_index=header_row_index,
    )

    print("\n--- MER matches (latest month only) ---")
    print("UC-03 (undeposited) matches:")
    if mer_undeposited_matches:
        for m in mer_undeposited_matches:
            print(f"- {m.a1_cell}: {m.row_text} -> {m.value}")
    else:
        print("- (none)")

    print("UC-01 (clearing account) matches:")
    if mer_clearing_matches:
        for m in mer_clearing_matches:
            print(f"- {m.a1_cell}: {m.row_text} -> {m.value}")
    else:
        print("- (none)")

    lookup = find_value_in_table(
        rows=rows,
        row_key=row_key,
        col_header=latest_month,
        header_row_index=header_row_index,
    )
    print(f"Row key: {row_key}")
    print(f"Matched row cell: {lookup.matched_row_key_cell}")
    print(f"Matched column header: {lookup.matched_col_header}")
    print(f"Matched A1 cell (within returned range): {lookup.a1_cell}")
    print(f"Value (raw): {lookup.value}")

    mer_amount = parse_money(lookup.value)
    print(f"Value (parsed money): {mer_amount}")

    if lookup.value is None:
        print(
            "NOTE: MER row/column intersection not found; UC-04 will be skipped.",
            file=sys.stderr,
        )
        print(
            "Tip: set MER_ROW_KEY to the exact label in your sheet (or a distinctive substring).",
            file=sys.stderr,
        )

    # QBO
    print("\n=== QBO ===")
    try:
        qbo = QBOClient.from_env()
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        print(
            "Tip: set QBO_CLIENT_ID and QBO_CLIENT_SECRET in your .env (gitignored).",
            file=sys.stderr,
        )
        return 8
    # Print company info to make it easy to cross-reference the sandbox company.
    try:
        company = qbo.get_company_info()
        company_name = (
            company.get("CompanyInfo", {}).get("CompanyName")
            or company.get("CompanyInfo", {}).get("LegalName")
        )
        print(f"Company (from API): {company_name}")
    except Exception as e:
        print(f"NOTE: Could not fetch CompanyInfo ({e})")

    print(f"BalanceSheet start_date: {start_date}")
    print(f"BalanceSheet end_date: {end_date}")
    print(f"BalanceSheet accounting_method: {accounting_method}")
    print(f"BalanceSheet date_macro: {date_macro}")
    report = qbo.get_balance_sheet(
        end_date=end_date,
        start_date=start_date,
        accounting_method=accounting_method,
        date_macro=date_macro,
    )

    header = report.get("Header") if isinstance(report, dict) else None
    if isinstance(header, dict):
        print("Report Header (from API):")
        for k in [
            "ReportName",
            "StartPeriod",
            "EndPeriod",
            "ReportBasis",
            "AsOf",
            "AsOfDate",
            "DateMacro",
            "Option",
            "Currency",
            "Time",
        ]:
            if k in header:
                print(f"- {k}: {header.get(k)}")

    items = extract_balance_sheet_items(report)
    print(f"BalanceSheet items extracted: {len(items)}")

    print("\n--- Extracted Balance Sheet Items (label -> amount) ---")
    for item in items:
        print(f"- {item.label} -> {item.amount}")

    # UC-01: Clearing accounts must be zero
    print("\n=== UC-01: Clearing Accounts Zero ===")
    uc01 = check_zero_on_both_sides_by_substring(
        check_id="UC-01",
        mer_lines=[(m.row_text, m.value) for m in mer_clearing_matches],
        qbo_lines=items,
        label_substring="clearing",
        rule="Clearing-related balances should be zero on both MER and QBO balance sheets",
    )
    print(f"APPLICABLE: {uc01.details.get('applicable')}")
    print(f"MER FOUND lines: {uc01.details.get('mer_found')}")
    print(f"QBO FOUND lines: {uc01.details.get('qbo_found')}")
    print(f"MER matches: {len(uc01.details.get('mer_matches') or [])}")
    print(f"QBO matches: {len(uc01.details.get('qbo_matches') or [])}")
    print(f"PASSED: {uc01.passed}")

    # UC-03: Undeposited Funds should be zero
    print("\n=== UC-03: Undeposited Funds Zero ===")
    uc03 = check_zero_on_both_sides_by_substring(
        check_id="UC-03",
        mer_lines=[(m.row_text, m.value) for m in mer_undeposited_matches],
        qbo_lines=items,
        label_substring="undeposited",
        rule="Undeposited-related balances should be zero on both MER and QBO balance sheets",
    )
    print(f"APPLICABLE: {uc03.details.get('applicable')}")
    print(f"MER FOUND lines: {uc03.details.get('mer_found')}")
    print(f"QBO FOUND lines: {uc03.details.get('qbo_found')}")
    print(f"MER matches: {len(uc03.details.get('mer_matches') or [])}")
    print(f"QBO matches: {len(uc03.details.get('qbo_matches') or [])}")
    print(f"PASSED: {uc03.passed}")

    # UC-02: Bank balance matches (optional single-account check)
    # Provide:
    # - MER_BANK_ROW_KEY: the exact row label in MER Balance Sheet (e.g. "PayPal CAD Account CAD")
    # - QBO_BANK_LABEL_SUBSTRING: substring for QBO BS label (e.g. "paypal")
    print("\n=== UC-02: Bank Balance Matches (Optional) ===")
    mer_bank_row_key = os.environ.get("MER_BANK_ROW_KEY")
    qbo_bank_label_substring = os.environ.get("QBO_BANK_LABEL_SUBSTRING")
    if not mer_bank_row_key or not qbo_bank_label_substring:
        print("SKIPPED: set MER_BANK_ROW_KEY and QBO_BANK_LABEL_SUBSTRING to enable")
    else:
        mer_bank_lookup = find_value_in_table(
            rows=rows,
            row_key=mer_bank_row_key,
            col_header=latest_month,
            header_row_index=header_row_index,
        )
        mer_bank_amount = parse_money(mer_bank_lookup.value)
        qbo_bank_raw = find_first_amount(items, qbo_bank_label_substring)
        qbo_bank_amount = parse_money(qbo_bank_raw)
        print(f"MER bank row key: {mer_bank_row_key}")
        print(f"MER bank A1 cell: {mer_bank_lookup.a1_cell}")
        print(f"MER bank value (raw): {mer_bank_lookup.value}")
        print(f"QBO bank label substring: {qbo_bank_label_substring}")
        print(f"QBO bank value (raw first match): {qbo_bank_raw}")
        uc02 = check_bank_balance_matches(
            mer_amount=mer_bank_amount,
            qbo_amount=qbo_bank_amount,
        )
        print(f"PASSED: {uc02.passed}")
        print(f"DETAILS: {uc02.details}")

    # UC-04: Petty cash match (skip if MER didn't contain the row)
    print("\n=== UC-04: Petty Cash Match ===")
    qbo_raw = find_first_amount(items, "petty cash")
    qbo_amount = parse_money(qbo_raw)
    print(f"QBO petty cash (raw first match): {qbo_raw}")
    print(f"QBO petty cash (parsed): {qbo_amount}")
    if mer_amount is None:
        print("SKIPPED: MER petty cash not found in sheet range")
    else:
        uc04 = check_petty_cash_matches(mer_amount=mer_amount, qbo_amount=qbo_amount)
        print(f"PASSED: {uc04.passed}")
        print(f"DETAILS: {uc04.details}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
