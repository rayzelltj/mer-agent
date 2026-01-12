from __future__ import annotations

from decimal import Decimal

from src.backend.v4.integrations.qbo_reports import ReportLineItem
from src.backend.v4.use_cases.mer_review_checks import (
    check_clearing_accounts_zero,
    check_undeposited_funds_zero,
    parse_mer_month_header,
    parse_money,
    pick_latest_month_header,
    check_petty_cash_matches,
    check_reconciled_zero_by_substring,
)


def test_parse_mer_month_header() -> None:
    assert parse_mer_month_header("Nov. 2025").isoformat() == "2025-11-01"
    assert parse_mer_month_header("September 2025").isoformat() == "2025-09-01"
    assert parse_mer_month_header("27 Dec 2025").isoformat() == "2025-12-01"
    assert parse_mer_month_header("not a month") is None


def test_pick_latest_month_header() -> None:
    headers = ["Aug. 2025", "Sep. 2025", "Oct. 2025", "Nov. 2025"]
    assert pick_latest_month_header(headers) == "Nov. 2025"


def test_parse_money() -> None:
    assert parse_money("1,234.56") == Decimal("1234.56")
    assert parse_money("(10.00)") == Decimal("-10.00")
    assert parse_money("$") is None
    assert parse_money("") is None


def test_check_clearing_accounts_zero_requires_all_zero() -> None:
    items = [
        ReportLineItem(label="AR Clearing Account", amount="0.00"),
        ReportLineItem(label="AP clearing account", amount="(0.01)"),
    ]
    res = check_clearing_accounts_zero(balance_sheet_items=items)
    assert res.passed is True

    items2 = [
        ReportLineItem(label="Clearing Account", amount="1.00"),
    ]
    res2 = check_clearing_accounts_zero(balance_sheet_items=items2)
    assert res2.passed is False


def test_check_undeposited_funds_zero() -> None:
    items = [
        ReportLineItem(label="Undeposited Funds", amount="0.00"),
    ]
    res = check_undeposited_funds_zero(balance_sheet_items=items)
    assert res.passed is True


def test_checks_not_applicable_when_line_missing() -> None:
    items: list[ReportLineItem] = [ReportLineItem(label="Cash", amount="1.00")]

    res_uc01 = check_clearing_accounts_zero(balance_sheet_items=items)
    assert res_uc01.passed is True
    assert res_uc01.details.get("applicable") is False

    res_uc03 = check_undeposited_funds_zero(balance_sheet_items=items)
    assert res_uc03.passed is True
    assert res_uc03.details.get("applicable") is False


def test_check_petty_cash_matches() -> None:
    res = check_petty_cash_matches(
        mer_amount=Decimal("10.00"), qbo_amount=Decimal("10.01"), tolerance=Decimal("0.01")
    )
    assert res.passed is True


def test_check_reconciled_zero_by_substring_requires_match_and_zero() -> None:
    mer_lines = [("Undeposited Funds", "0.00")]
    qbo_items = [ReportLineItem(label="Undeposited Funds", amount="0.00")]

    res = check_reconciled_zero_by_substring(
        check_id="UC-03",
        mer_lines=mer_lines,
        qbo_lines=qbo_items,
        label_substring="undeposited",
        rule="Undeposited funds should be zero and match",
    )
    assert res.passed is True
    assert res.details["applicable"] is True
    assert res.details["qbo_found"] is True


def test_check_reconciled_zero_by_substring_fails_if_mer_nonzero() -> None:
    mer_lines = [("Undeposited Funds", "10.00")]
    qbo_items = [ReportLineItem(label="Undeposited Funds", amount="0.00")]

    res = check_reconciled_zero_by_substring(
        check_id="UC-03",
        mer_lines=mer_lines,
        qbo_lines=qbo_items,
        label_substring="undeposited",
        rule="Undeposited funds should be zero and match",
    )
    assert res.passed is False


def test_check_reconciled_zero_by_substring_fails_if_qbo_missing() -> None:
    mer_lines = [("Undeposited Funds", "0.00")]
    qbo_items: list[ReportLineItem] = []

    res = check_reconciled_zero_by_substring(
        check_id="UC-03",
        mer_lines=mer_lines,
        qbo_lines=qbo_items,
        label_substring="undeposited",
        rule="Undeposited funds should be zero and match",
    )
    assert res.passed is False
