"""Helpers for parsing QBO report payloads.

These functions are intentionally "dumb" and deterministic so they can be unit-tested
without calling QuickBooks.

QBO reports (e.g. BalanceSheet) return nested JSON with rows in `Rows.Row[*]`.
Leaf-ish rows usually contain `ColData` with a label and a value.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True, slots=True)
class ReportLineItem:
    label: str
    amount: str


def iter_report_line_items(rows: dict[str, Any] | None) -> Iterable[ReportLineItem]:
    """Yield flattened report line items from a nested QBO `Rows` object."""

    if not rows:
        return

    row_list = rows.get("Row") or []
    for row in row_list:
        col = row.get("ColData")
        if isinstance(col, list) and len(col) >= 2:
            label = (col[0].get("value") or "").strip()
            amount = (col[1].get("value") or "").strip()
            if label:
                yield ReportLineItem(label=label, amount=amount)

        nested = row.get("Rows")
        if isinstance(nested, dict):
            yield from iter_report_line_items(nested)


def extract_balance_sheet_items(report: dict[str, Any]) -> list[ReportLineItem]:
    """Extract flattened line items from a BalanceSheet report response."""

    rows = report.get("Rows")
    if not isinstance(rows, dict):
        return []
    return list(iter_report_line_items(rows))


def find_first_amount(items: Iterable[ReportLineItem], name_substring: str) -> str | None:
    """Return the first amount whose label contains `name_substring` (case-insensitive)."""

    needle = name_substring.lower()
    for item in items:
        if needle in item.label.lower():
            return item.amount
    return None
