from __future__ import annotations

from src.backend.v4.integrations.qbo_reports import (
    extract_balance_sheet_items,
    extract_aged_detail_items_over_threshold,
    extract_report_total_value,
    find_first_amount,
)


def test_extract_balance_sheet_items_flattens_nested_rows() -> None:
    report = {
        "Rows": {
            "Row": [
                {
                    "ColData": [
                        {"value": "Assets"},
                        {"value": ""},
                    ],
                    "Rows": {
                        "Row": [
                            {
                                "ColData": [
                                    {"value": "Undeposited Funds"},
                                    {"value": "123.45"},
                                ]
                            },
                            {
                                "ColData": [
                                    {"value": "Petty Cash"},
                                    {"value": "10.00"},
                                ]
                            },
                        ]
                    },
                }
            ]
        }
    }

    items = extract_balance_sheet_items(report)
    assert len(items) == 3
    assert find_first_amount(items, "undeposited") == "123.45"
    assert find_first_amount(items, "PETTY CASH") == "10.00"


def test_extract_balance_sheet_items_handles_missing_rows() -> None:
    assert extract_balance_sheet_items({}) == []
    assert extract_balance_sheet_items({"Rows": None}) == []


def test_extract_report_total_value_finds_total_column_and_matching_summary_row() -> None:
    report = {
        "Columns": {
            "Column": [
                {"ColTitle": "Vendor"},
                {"ColTitle": "Current"},
                {"ColTitle": "Total"},
            ]
        },
        "Rows": {
            "Row": [
                {
                    "Summary": {
                        "ColData": [
                            {"value": "Total Accounts Payable"},
                            {"value": ""},
                            {"value": "123.45"},
                        ]
                    }
                }
            ]
        },
    }

    val, evidence = extract_report_total_value(
        report,
        total_row_must_contain=["total", "payable"],
        prefer_column_titles=["Total"],
    )
    assert val == "123.45"
    assert evidence.get("matched_row_label") == "Total Accounts Payable"
    assert evidence.get("matched_col_title") == "Total"


def test_extract_report_total_value_returns_none_when_no_rows_match() -> None:
    report = {
        "Columns": {"Column": [{"ColTitle": "Total"}]},
        "Rows": {"Row": [{"ColData": [{"value": "Not a total"}, {"value": "9"}]}]},
    }
    val, evidence = extract_report_total_value(
        report,
        total_row_must_contain=["total", "receivable"],
        prefer_column_titles=["Total"],
    )
    assert val is None
    assert "reason" in evidence


def test_extract_aged_detail_items_over_threshold_selects_61_plus_buckets() -> None:
    report = {
        "Columns": {
            "Column": [
                {"ColTitle": "Name"},
                {"ColTitle": "Current"},
                {"ColTitle": "31 - 60"},
                {"ColTitle": "61 - 90"},
                {"ColTitle": "91 and over"},
                {"ColTitle": "Total"},
            ]
        },
        "Rows": {
            "Row": [
                {
                    "ColData": [
                        {"value": "Vendor A"},
                        {"value": ""},
                        {"value": "20.00"},
                        {"value": "50.00"},
                        {"value": ""},
                        {"value": "70.00"},
                    ]
                },
                {
                    "ColData": [
                        {"value": "Vendor B"},
                        {"value": ""},
                        {"value": ""},
                        {"value": ""},
                        {"value": "10.00"},
                        {"value": "10.00"},
                    ]
                },
                {
                    "Summary": {
                        "ColData": [
                            {"value": "Total Accounts Payable"},
                            {"value": ""},
                            {"value": ""},
                            {"value": ""},
                            {"value": ""},
                            {"value": "80.00"},
                        ]
                    }
                },
            ]
        },
    }

    res = extract_aged_detail_items_over_threshold(report, max_age_days=60, limit=100)
    items = res["items"]
    assert len(items) == 2
    assert res["total_over_threshold"] == "60.00"

    labels = sorted([i["label"] for i in items])
    assert labels == ["Vendor A", "Vendor B"]

    # Vendor A: only 61-90 counts (31-60 should NOT count for >60).
    a = next(i for i in items if i["label"] == "Vendor A")
    assert a["amount_over_threshold"] == "50.00"

    b = next(i for i in items if i["label"] == "Vendor B")
    assert b["amount_over_threshold"] == "10.00"
