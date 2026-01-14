"""Helpers for parsing QBO report payloads.

These functions are intentionally "dumb" and deterministic so they can be unit-tested
without calling QuickBooks.

QBO reports (e.g. BalanceSheet) return nested JSON with rows in `Rows.Row[*]`.
Leaf-ish rows usually contain `ColData` with a label and a value.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
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


def _norm(s: str | None) -> str:
    return "".join(ch.lower() for ch in (s or "") if ch.isalnum())


def _extract_report_column_titles(report: dict[str, Any]) -> list[str]:
    cols = report.get("Columns")
    if not isinstance(cols, dict):
        return []
    col_list = cols.get("Column")
    if not isinstance(col_list, list):
        return []
    titles: list[str] = []
    for c in col_list:
        if isinstance(c, dict):
            titles.append(str(c.get("ColTitle") or ""))
    return titles


def _iter_report_coldata_rows(rows: dict[str, Any] | None) -> Iterable[list[str]]:
    """Yield rows as a list of string values from ColData and Summary.ColData.

    QBO report rows can be nested. Each row may include:
    - ColData: list[{value: ...}, ...]
    - Summary: { ColData: [...] }
    - Rows: { Row: [...] }
    """

    if not rows:
        return

    row_list = rows.get("Row") or []
    for row in row_list:
        if not isinstance(row, dict):
            continue

        col = row.get("ColData")
        if isinstance(col, list) and col:
            yield [str((x.get("value") if isinstance(x, dict) else "") or "") for x in col]

        summary = row.get("Summary")
        if isinstance(summary, dict):
            scol = summary.get("ColData")
            if isinstance(scol, list) and scol:
                yield [str((x.get("value") if isinstance(x, dict) else "") or "") for x in scol]

        nested = row.get("Rows")
        if isinstance(nested, dict):
            yield from _iter_report_coldata_rows(nested)


def extract_report_total_value(
    report: dict[str, Any],
    *,
    total_row_must_contain: list[str],
    prefer_column_titles: list[str] | None = None,
) -> tuple[str | None, dict[str, Any]]:
    """Extract a total-like value from a QBO report.

    This is meant for multi-column reports like AgedPayablesDetail/AgedReceivablesDetail.

    Strategy:
    - Determine a "total" column index via report column titles (prefer 'Total').
    - Scan all ColData/Summary.ColData rows for a row label (first column)
      that contains all required tokens in `total_row_must_contain`.
    - Return the cell value at that chosen column index.

    Returns (value, evidence).
    """

    titles = _extract_report_column_titles(report)
    prefer = prefer_column_titles or ["Total", "Balance", "Amount"]
    col_index: int | None = None
    norm_titles = [_norm(t) for t in titles]
    for want in prefer:
        w = _norm(want)
        if not w:
            continue
        for i, nt in enumerate(norm_titles):
            if nt == w or (w and w in nt):
                col_index = i
                break
        if col_index is not None:
            break

    required = [_norm(t) for t in (total_row_must_contain or []) if _norm(t)]
    best: tuple[str, str, int] | None = None  # (label, value, col_index_used)

    rows = report.get("Rows")
    if not isinstance(rows, dict):
        return None, {"reason": "missing Rows"}

    for row_vals in _iter_report_coldata_rows(rows):
        if not row_vals:
            continue
        label = str(row_vals[0] or "")
        nlabel = _norm(label)
        if required and not all(tok in nlabel for tok in required):
            continue

        # Choose a column; fallback to last cell.
        idx = col_index if col_index is not None else (len(row_vals) - 1)
        if idx < 0 or idx >= len(row_vals):
            continue
        val = str(row_vals[idx] or "")
        if val.strip() == "":
            continue
        best = (label, val, idx)
        break

    if best is None:
        return None, {
            "reason": "no matching total row found",
            "required_tokens": total_row_must_contain,
            "column_titles": titles,
        }

    label, val, idx_used = best
    return val, {
        "matched_row_label": label,
        "matched_col_index": idx_used,
        "matched_col_title": titles[idx_used] if 0 <= idx_used < len(titles) else None,
        "required_tokens": total_row_must_contain,
    }


def _parse_decimal(value: str | None) -> Decimal | None:
    s = (value or "").strip()
    if not s:
        return None

    # Common QBO patterns: commas, parentheses for negatives.
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1]

    s = s.replace(",", "").replace("$", "").strip()
    if not s:
        return None

    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    return -d if neg else d


def _bucket_start_days(col_title: str) -> int | None:
    """Infer the lower-bound day value for an aging bucket column title.

    Examples:
      - "Current" -> 0
      - "1 - 30" -> 1
      - "31-60" -> 31
      - "61 - 90" -> 61
      - "91 and over" / "91+" -> 91
    """

    t = (col_title or "").strip().lower()
    if not t:
        return None
    if "current" in t:
        return 0

    m = re.search(r"(\d+)\s*-\s*(\d+)", t)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None

    m = re.search(r"(\d+)\s*(\+|and\s+over|over)", t)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None

    return None


def extract_aged_detail_items_over_threshold(
    report: dict[str, Any],
    *,
    max_age_days: int,
    limit: int = 100,
) -> dict[str, Any]:
    """Extract open AP/AR items older than `max_age_days` from an Aged*Detail report.

    This is a deterministic extractor that:
    - Finds bucket columns whose lower-bound day value is strictly greater than max_age_days.
    - For each row, sums those bucket amounts; if > 0, includes the row as an "over threshold" item.

    Returns a dict with:
      - items: list[dict]
      - total_over_threshold: str
      - evidence: dict
    """

    titles = _extract_report_column_titles(report)
    norm_titles = [t.strip() for t in titles]
    bucket_indexes: list[int] = []
    bucket_titles: list[str] = []

    for i, title in enumerate(norm_titles):
        start = _bucket_start_days(title)
        if start is None:
            continue
        if start > max_age_days:
            bucket_indexes.append(i)
            bucket_titles.append(title)

    rows = report.get("Rows")
    if not isinstance(rows, dict):
        return {
            "items": [],
            "total_over_threshold": "0",
            "evidence": {"reason": "missing Rows", "column_titles": titles},
        }

    items: list[dict[str, Any]] = []
    total = Decimal("0")

    for row_vals in _iter_report_coldata_rows(rows):
        if not row_vals:
            continue
        if not bucket_indexes:
            continue

        amt = Decimal("0")
        for idx in bucket_indexes:
            if 0 <= idx < len(row_vals):
                d = _parse_decimal(row_vals[idx])
                if d is not None:
                    amt += d

        if amt <= 0:
            continue

        # Skip obvious totals/summary labels; those are handled elsewhere.
        label0 = str(row_vals[0] or "")
        nlabel0 = _norm(label0)
        if nlabel0.startswith("total"):
            continue

        total += amt

        item: dict[str, Any] = {
            "label": label0,
            "amount_over_threshold": str(amt),
        }

        # Add a best-effort subset of fields by column title.
        for i, title in enumerate(norm_titles[: min(len(norm_titles), len(row_vals))]):
            key = _norm(title)
            if not key:
                continue
            val = str(row_vals[i] or "").strip()
            if val:
                item[key] = val

        items.append(item)
        if limit >= 0 and len(items) >= limit:
            break

    return {
        "items": items,
        "total_over_threshold": str(total),
        "evidence": {
            "max_age_days": max_age_days,
            "bucket_columns": bucket_titles,
            "bucket_col_indexes": bucket_indexes,
            "column_titles": titles,
            "truncated": bool(limit >= 0 and len(items) >= limit),
        },
    }
