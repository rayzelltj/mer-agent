from __future__ import annotations

from src.backend.v4.integrations.qbo_reports import (
    extract_balance_sheet_items,
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
