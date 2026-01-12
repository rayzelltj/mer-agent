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
    check_petty_cash_matches,
    check_reconciled_zero_by_substring,
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

    # COA + Trial Balance: identify clearing accounts from COA, then pull balances
    # from Trial Balance for the same period window.
    print("\n=== QBO COA (Chart of Accounts) ===")
    try:
        accounts = qbo.get_accounts()
    except Exception as e:
        print(f"NOTE: Could not fetch COA accounts ({e})")
        accounts = []

    print(f"COA accounts fetched: {len(accounts)}")
    if accounts:
        print("COA preview (first 10 by Name):")
        preview = sorted(
            [a for a in accounts if isinstance(a, dict)],
            key=lambda a: str(a.get("Name", "")),
        )[:10]
        for a in preview:
            name = a.get("Name")
            acct_id = a.get("Id")
            acct_type = a.get("AccountType")
            subtype = a.get("AccountSubType")
            active = a.get("Active")
            current_balance = a.get("CurrentBalanceWithSubAccounts")
            if current_balance is None:
                current_balance = a.get("CurrentBalance")
            print(
                f"- {name} (Id={acct_id}, Type={acct_type}, SubType={subtype}, Active={active}, CurrentBalance={current_balance})"
            )
    else:
        print("COA preview: (none)")

    clearing_accounts = [
        a
        for a in accounts
        if isinstance(a, dict)
        and "Name" in a
        and "clearing" in str(a.get("Name", "")).lower()
    ]
    print(f"COA clearing accounts (name contains 'clearing'): {len(clearing_accounts)}")
    for a in clearing_accounts:
        name = a.get("Name")
        acct_id = a.get("Id")
        acct_type = a.get("AccountType")
        subtype = a.get("AccountSubType")
        active = a.get("Active")
        current_balance = a.get("CurrentBalanceWithSubAccounts")
        if current_balance is None:
            current_balance = a.get("CurrentBalance")
        print(
            f"- {name} (Id={acct_id}, Type={acct_type}, SubType={subtype}, Active={active}, CurrentBalance={current_balance})"
        )

    print("\n=== QBO Trial Balance (account-level, period-accurate) ===")
    try:
        trial_report = qbo.get_trial_balance(
            end_date=end_date,
            start_date=start_date,
            accounting_method=accounting_method,
        )
        trial_header = (
            trial_report.get("Header") if isinstance(trial_report, dict) else None
        )
        if isinstance(trial_header, dict):
            print("TrialBalance Header (from API):")
            for k in [
                "ReportName",
                "StartPeriod",
                "EndPeriod",
                "ReportBasis",
                "AsOf",
                "AsOfDate",
                "DateMacro",
            ]:
                if k in trial_header:
                    print(f"- {k}: {trial_header.get(k)}")

        trial_items = extract_balance_sheet_items(trial_report)
        print(f"TrialBalance items extracted: {len(trial_items)}")
    except Exception as e:
        print(f"NOTE: Could not fetch TrialBalance ({e})")
        trial_items = []

    # UC-01: Clearing accounts must be zero
    print("\n=== UC-01: Clearing Accounts Zero ===")
    # Use COA to identify which accounts are “clearing accounts”, then match those
    # names against Trial Balance lines for the selected period.
    clearing_names = {
        str(a.get("Name")).strip().lower()
        for a in clearing_accounts
        if isinstance(a, dict) and a.get("Name")
    }

    qbo_clearing_lines = []
    if clearing_names and trial_items:
        for it in trial_items:
            lbl = (it.label or "").strip().lower()
            if lbl in clearing_names:
                qbo_clearing_lines.append(it)
    else:
        # Fallback: if COA did not return clearing accounts, at least attempt
        # a substring match on Trial Balance.
        qbo_clearing_lines = [
            it for it in trial_items if "clearing" in it.label.lower()
        ]

    if qbo_clearing_lines:
        print("QBO clearing lines (from TrialBalance):")
        for it in qbo_clearing_lines:
            print(f"- {it.label} -> {it.amount}")
    else:
        print("QBO clearing lines (from TrialBalance): (none)")

    uc01 = check_reconciled_zero_by_substring(
        check_id="UC-01",
        mer_lines=[(m.row_text, m.value) for m in mer_clearing_matches],
        qbo_lines=qbo_clearing_lines,
        label_substring="clearing",
        rule="Clearing accounts must match between MER and QBO and be zero",
    )
    print(f"APPLICABLE (MER has lines): {uc01.details.get('applicable')}")
    print(f"QBO FOUND lines: {uc01.details.get('qbo_found')}")
    print(f"MER total: {uc01.details.get('mer_total')}")
    print(f"QBO total: {uc01.details.get('qbo_total')}")
    print(f"DELTA: {uc01.details.get('delta')}")
    print(f"PASSED: {uc01.passed}")

    # UC-03: Undeposited Funds should be zero
    print("\n=== UC-03: Undeposited Funds Zero ===")
    uc03 = check_reconciled_zero_by_substring(
        check_id="UC-03",
        mer_lines=[(m.row_text, m.value) for m in mer_undeposited_matches],
        qbo_lines=items,
        label_substring="undeposited",
        rule="Undeposited funds must match between MER and QBO and be zero",
    )
    print(f"APPLICABLE (MER has lines): {uc03.details.get('applicable')}")
    print(f"QBO FOUND lines: {uc03.details.get('qbo_found')}")
    print(f"MER total: {uc03.details.get('mer_total')}")
    print(f"QBO total: {uc03.details.get('qbo_total')}")
    print(f"DELTA: {uc03.details.get('delta')}")
    print(f"PASSED: {uc03.passed}")

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
