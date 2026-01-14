"""Deterministic MER review checks (MVP).

This is the "middle layer" between:
- Integrations (fetching data from QBO / Google Sheets)
- Orchestration (agents deciding which checks to run)

No network calls here: functions accept already-fetched data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Iterable

from src.backend.v4.integrations.qbo_reports import ReportLineItem


@dataclass(frozen=True, slots=True)
class CheckResult:
    check_id: str
    passed: bool
    details: dict


_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def parse_mer_month_header(text: str) -> date | None:
    """Parse headers like 'Nov. 2025' or 'November 2025' into a date.

    Returns the first day of that month (used for sorting only).
    """

    if not text:
        return None

    s = text.strip().lower()

    # Pattern A: "Nov. 2025", "Nov 2025", "November 2025"
    m1 = re.search(r"^\s*([a-z]{3,9})\.?\s+(\d{4})\b", s)
    if m1:
        mon_token = (
            m1.group(1)[:4] if m1.group(1).startswith("sept") else m1.group(1)[:3]
        )
        mon = _MONTHS.get(mon_token)
        if not mon:
            return None
        year = int(m1.group(2))
        return date(year, mon, 1)

    # Pattern B: "27 Dec 2025" (treat as Dec 2025)
    m2 = re.search(r"^\s*\d{1,2}\s+([a-z]{3,9})\s+(\d{4})\b", s)
    if m2:
        mon_token = (
            m2.group(1)[:4] if m2.group(1).startswith("sept") else m2.group(1)[:3]
        )
        mon = _MONTHS.get(mon_token)
        if not mon:
            return None
        year = int(m2.group(2))
        return date(year, mon, 1)

    return None


def pick_latest_month_header(headers: Iterable[str]) -> str | None:
    """Given MER headers (e.g. Aug/Sep/Oct/Nov), return the latest month string."""

    best: tuple[date, str] | None = None
    for h in headers:
        d = parse_mer_month_header(h)
        if d is None:
            continue
        if best is None or d > best[0]:
            best = (d, h)
    return best[1] if best else None


def parse_money(value: str | None) -> Decimal | None:
    """Parse common accounting strings into Decimal.

    Handles:
    - commas
    - parentheses for negatives
    - currency symbols
    - blanks
    """

    if value is None:
        return None

    s = str(value).strip()
    if s == "" or s.lower() in {"-", "n/a", "na"}:
        return None

    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1].strip()

    # Remove currency symbols and commas
    s = re.sub(r"[^0-9.\-]", "", s)
    if s == "":
        return None

    try:
        amount = Decimal(s)
    except InvalidOperation:
        return None

    return -amount if negative else amount


def is_zero(amount: Decimal | None, *, tolerance: Decimal = Decimal("0.01")) -> bool:
    if amount is None:
        return False
    return abs(amount) <= tolerance


def check_clearing_accounts_zero(
    *,
    balance_sheet_items: Iterable[ReportLineItem],
    label_substring: str = "clearing account",
    tolerance: Decimal = Decimal("0.01"),
) -> CheckResult:
    """UC-01 (simplified): any Balance Sheet line containing 'clearing account' must be 0."""

    matches: list[dict] = []
    for item in balance_sheet_items:
        if label_substring.lower() in item.label.lower():
            amt = parse_money(item.amount)
            matches.append(
                {
                    "label": item.label,
                    "amount_raw": item.amount,
                    "amount": str(amt) if amt is not None else None,
                    "is_zero": is_zero(amt, tolerance=tolerance),
                }
            )

    applicable = bool(matches)
    passed = (not applicable) or all(m["is_zero"] for m in matches)
    return CheckResult(
        check_id="UC-01",
        passed=passed,
        details={
            "rule": "All clearing accounts must be zero at period end",
            "applicable": applicable,
            "label_substring": label_substring,
            "tolerance": str(tolerance),
            "matches": matches,
        },
    )


def check_undeposited_funds_zero(
    *,
    balance_sheet_items: Iterable[ReportLineItem],
    tolerance: Decimal = Decimal("0.01"),
) -> CheckResult:
    """UC-03: Undeposited Funds should be 0."""

    # QBO label can vary slightly; start with substring.
    candidates = [
        item for item in balance_sheet_items if "undeposited" in item.label.lower()
    ]
    matches: list[dict] = []
    for item in candidates:
        amt = parse_money(item.amount)
        matches.append(
            {
                "label": item.label,
                "amount_raw": item.amount,
                "amount": str(amt) if amt is not None else None,
                "is_zero": is_zero(amt, tolerance=tolerance),
            }
        )

    applicable = bool(matches)

    return CheckResult(
        check_id="UC-03",
        passed=(not applicable) or all(m["is_zero"] for m in matches),
        details={
            "rule": "Undeposited funds should be zero at period end",
            "applicable": applicable,
            "tolerance": str(tolerance),
            "matches": matches,
        },
    )


def check_petty_cash_matches(
    *,
    mer_amount: Decimal | None,
    qbo_amount: Decimal | None,
    tolerance: Decimal = Decimal("0.01"),
) -> CheckResult:
    """UC-04: Petty cash matches between MER and QBO Balance Sheet."""

    if mer_amount is None or qbo_amount is None:
        passed = False
    else:
        passed = abs(mer_amount - qbo_amount) <= tolerance

    return CheckResult(
        check_id="UC-04",
        passed=passed,
        details={
            "rule": "Petty cash amount should match between MER and QBO",
            "tolerance": str(tolerance),
            "mer_amount": str(mer_amount) if mer_amount is not None else None,
            "qbo_amount": str(qbo_amount) if qbo_amount is not None else None,
            "delta": str((mer_amount - qbo_amount))
            if mer_amount is not None and qbo_amount is not None
            else None,
        },
    )


def _collect_line_matches_by_substring(
    *,
    items: Iterable[tuple[str, str | None]],
    label_substring: str,
    tolerance: Decimal,
) -> list[dict]:
    matches: list[dict] = []
    for label, amount_raw in items:
        if label_substring.lower() in (label or "").lower():
            amt = parse_money(amount_raw)
            matches.append(
                {
                    "label": label,
                    "amount_raw": amount_raw,
                    "amount": str(amt) if amt is not None else None,
                    "is_zero": is_zero(amt, tolerance=tolerance),
                }
            )
    return matches


def check_reconciled_zero_by_substring(
    *,
    check_id: str,
    mer_lines: Iterable[tuple[str, str | None]],
    qbo_lines: Iterable[ReportLineItem],
    label_substring: str,
    tolerance: Decimal = Decimal("0.01"),
    rule: str,
) -> CheckResult:
    """Line-by-line check (legacy name): matched lines must be near-zero.

    This helper is retained for backward compatibility with earlier tests.

    Semantics:
    - Applicable if MER contains at least one matching line.
    - If applicable, QBO must also contain at least one matching line.
    - Pass if every matched line on both sides is within tolerance of zero.

    Note: This does NOT sum/aggregate multiple matches.
    """

    mer_matches = _collect_line_matches_by_substring(
        items=mer_lines, label_substring=label_substring, tolerance=tolerance
    )
    qbo_matches = _collect_line_matches_by_substring(
        items=((i.label, i.amount) for i in qbo_lines),
        label_substring=label_substring,
        tolerance=tolerance,
    )

    applicable = bool(mer_matches)
    qbo_found = bool(qbo_matches)

    passed = (not applicable) or (
        qbo_found
        and all(m["is_zero"] for m in mer_matches)
        and all(m["is_zero"] for m in qbo_matches)
    )

    return CheckResult(
        check_id=check_id,
        passed=passed,
        details={
            "rule": rule,
            "label_substring": label_substring,
            "tolerance": str(tolerance),
            "applicable": applicable,
            "qbo_found": qbo_found,
            "mer_matches": mer_matches,
            "qbo_matches": qbo_matches,
        },
    )


def check_zero_on_both_sides_by_substring(
    *,
    check_id: str,
    mer_lines: Iterable[tuple[str, str | None]],
    qbo_lines: Iterable[ReportLineItem],
    label_substring: str,
    tolerance: Decimal = Decimal("0.01"),
    rule: str,
) -> CheckResult:
    """Two-sided Balance Sheet check: flag if either side is non-zero.

    Updated semantics (per clarified requirements):
    - Source values come from MER Balance Sheet and QBO Balance Sheet (as-of end date).
    - We do not require MER and QBO totals to be equal; we require each side to be near-zero.
    - Applicable if either side contains at least one matching line.
    """

    mer_matches = _collect_line_matches_by_substring(
        items=mer_lines, label_substring=label_substring, tolerance=tolerance
    )
    qbo_matches = _collect_line_matches_by_substring(
        items=((i.label, i.amount) for i in qbo_lines),
        label_substring=label_substring,
        tolerance=tolerance,
    )

    mer_found = bool(mer_matches)
    qbo_found = bool(qbo_matches)
    applicable = mer_found or qbo_found

    passed = (not applicable) or (
        all(m["is_zero"] for m in mer_matches)
        and all(m["is_zero"] for m in qbo_matches)
    )

    return CheckResult(
        check_id=check_id,
        passed=passed,
        details={
            "rule": rule,
            "label_substring": label_substring,
            "tolerance": str(tolerance),
            "applicable": applicable,
            "mer_found": mer_found,
            "qbo_found": qbo_found,
            "mer_matches": mer_matches,
            "qbo_matches": qbo_matches,
        },
    )


def check_bank_balance_matches(
    *,
    mer_amount: Decimal | None,
    qbo_amount: Decimal | None,
    tolerance: Decimal = Decimal("0.01"),
) -> CheckResult:
    """UC-02 (MVP alternative): MER bank balance matches QBO book balance.

    Note: this does NOT verify 'reconciliation report statement ending balance'.
    It compares the MER Balance Sheet bank line item against the QBO Balance Sheet
    bank line item (book balance) as-of the same end date.
    """

    if mer_amount is None or qbo_amount is None:
        passed = False
    else:
        passed = abs(mer_amount - qbo_amount) <= tolerance

    return CheckResult(
        check_id="UC-02",
        passed=passed,
        details={
            "rule": "MER bank balance should match QBO book balance at period end",
            "tolerance": str(tolerance),
            "mer_amount": str(mer_amount) if mer_amount is not None else None,
            "qbo_amount": str(qbo_amount) if qbo_amount is not None else None,
            "delta": str((mer_amount - qbo_amount))
            if mer_amount is not None and qbo_amount is not None
            else None,
        },
    )
