"""MER Rule Handlers.

This module contains standalone evaluation handler functions for each 
rule evaluation type. These are used by the MERBalanceSheetRuleEngine
to evaluate rules from the YAML rulebook.

Each handler has the signature:
    def handler(rule: dict[str, Any], ctx: MERBalanceSheetEvaluationContext) -> dict[str, Any]
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from src.backend.v4.integrations.google_sheets_reader import find_values_for_rows_containing, find_value_in_table
from src.backend.v4.use_cases.mer_review_checks import (
    check_bank_balance_matches,
    check_petty_cash_matches,
    check_zero_on_both_sides_by_substring,
    parse_money,
)

if TYPE_CHECKING:
    from src.backend.v4.use_cases.mer_rule_engine import MERBalanceSheetEvaluationContext


# ---------------------------------------------------------------------------
# Private helper functions (originally inside _default_registry closure)
# ---------------------------------------------------------------------------


def _extract_rule_required_sources(rule: dict[str, Any]) -> list[str]:
    """Extract required_sources from rule definition."""
    return list((rule.get("evaluation") or {}).get("required_sources") or [])


def _extract_rule_action_items(rule: dict[str, Any]) -> list[str]:
    """Extract action_items from rule definition."""
    return list((rule.get("action_items") or []))


def _is_non_line_item_label(label: str) -> bool:
    """Check if a label is a header/section label rather than a line item."""
    if not label:
        return True
    label_l = label.strip().lower()
    # Common section headers that are not amounts
    non_item_keywords = [
        "total",
        "subtotal",
        "sub-total",
        "net income",
        "net loss",
        "liabilities",
        "assets",
        "equity",
        "owner",
        "retained earnings",
    ]
    return any(kw in label_l for kw in non_item_keywords)


def _a1_cell(row_index: int, col_index: int) -> str:
    """Convert 0-based row/col to A1 notation (e.g., A1, B2)."""

    def col_letter(c: int) -> str:
        result = ""
        while c >= 0:
            result = chr(ord("A") + c % 26) + result
            c = c // 26 - 1
        return result

    return f"{col_letter(col_index)}{row_index + 1}"


def _find_col_index_by_header_contains(
    rows: list[list[str]],
    header_row_index: int | None,
    header_contains: str,
) -> int | None:
    """Find column index where header contains the given substring."""
    if header_row_index is None or header_row_index >= len(rows):
        return None
    header = rows[header_row_index] or []
    target = header_contains.lower().strip()
    for i, cell in enumerate(header):
        if target in (cell or "").lower():
            return i
    return None


def _resolve_mer_comments_col_index(
    rows: list[list[str]],
    header_row_index: int | None,
) -> tuple[int | None, str]:
    """Resolve the MER comments column index.

    Returns:
        Tuple of (col_index, resolution_mode)
        resolution_mode is one of: "fixed_F", "header_comments", "not_found"
    """
    # Convention: column F (index 5) is often used for comments
    if header_row_index is not None and header_row_index < len(rows):
        header = rows[header_row_index] or []
        # First try: look for 'comments' header
        for i, cell in enumerate(header):
            if "comment" in (cell or "").lower():
                return (i, "header_comments")
    # Fallback: fixed column F
    return (5, "fixed_F")


def find_first_amount(items: list, label_substring: str) -> str | None:
    """Find the first item matching label_substring and return its amount."""
    target = label_substring.lower()
    for item in items or []:
        label = str(getattr(item, "label", "") or "")
        if target in label.lower():
            return str(getattr(item, "amount", "") or "")
    return None


def extract_report_total_value(
    report: dict[str, Any],
    total_row_must_contain: list[str] | None = None,
    prefer_column_titles: list[str] | None = None,
) -> tuple[str | None, dict[str, Any]]:
    """Extract total value from a QBO report.

    Returns:
        Tuple of (total_value_raw, evidence_dict)
    """
    if total_row_must_contain is None:
        total_row_must_contain = ["total"]
    if prefer_column_titles is None:
        prefer_column_titles = ["Total"]

    evidence: dict[str, Any] = {"strategy": "unknown", "details": {}}

    # Strategy: look in Rows for a row containing "total" and extract the value
    rows = report.get("Rows", {}).get("Row", []) or []

    for row in rows:
        row_type = row.get("type", "")
        summary = row.get("Summary", {})
        header = row.get("Header", {})

        # Check Section type with Summary
        if row_type == "Section" and summary:
            col_data = summary.get("ColData", []) or []
            for cd in col_data:
                val = cd.get("value", "")
                if val and any(kw.lower() in val.lower() for kw in total_row_must_contain):
                    # Found a total row - look for amount
                    for cd2 in col_data:
                        val2 = cd2.get("value", "")
                        if val2 and val2.replace(",", "").replace("-", "").replace(".", "").isdigit():
                            evidence = {
                                "strategy": "section_summary_coldata",
                                "details": {"row_type": row_type, "col_data": col_data},
                            }
                            return (val2, evidence)

        # Check Header for total
        if header:
            col_data = header.get("ColData", []) or []
            for cd in col_data:
                val = cd.get("value", "")
                if val and any(kw.lower() in val.lower() for kw in total_row_must_contain):
                    # Found header with total - look for amount
                    for cd2 in col_data:
                        val2 = cd2.get("value", "")
                        if val2 and val2.replace(",", "").replace("-", "").replace(".", "").isdigit():
                            evidence = {
                                "strategy": "header_coldata",
                                "details": {"header": header},
                            }
                            return (val2, evidence)

    evidence = {"strategy": "not_found", "details": {"searched_rows": len(rows)}}
    return (None, evidence)


def extract_aged_detail_items_over_threshold(
    report: dict[str, Any],
    max_age_days: int,
    limit: int = 100,
) -> dict[str, Any]:
    """Extract aged items older than threshold from a QBO aging report.

    Returns:
        Dict with keys: items, total_over_threshold, evidence
    """
    result: dict[str, Any] = {
        "items": [],
        "total_over_threshold": None,
        "evidence": {"strategy": "unknown"},
    }

    # Aging reports have columns like: Current, 1-30, 31-60, 61-90, 91+
    # We look for items with amounts in columns beyond max_age_days

    columns = report.get("Columns", {}).get("Column", []) or []
    col_titles = [str(c.get("ColTitle", "") or "") for c in columns]

    # Find which column indices represent > max_age_days
    aging_col_indices: list[int] = []
    for i, title in enumerate(col_titles):
        title_l = title.lower()
        # Look for patterns like "31-60", "61-90", "91 and over", etc.
        if any(
            seg in title_l
            for seg in ["31", "61", "91", "over", "90+", "91+", "older"]
        ):
            if max_age_days <= 30 and ("31" in title_l or "over" in title_l or "older" in title_l):
                aging_col_indices.append(i)
            elif max_age_days <= 60 and ("61" in title_l or "91" in title_l or "over" in title_l or "older" in title_l):
                aging_col_indices.append(i)
            elif max_age_days <= 90 and ("91" in title_l or "over" in title_l or "older" in title_l):
                aging_col_indices.append(i)

    if not aging_col_indices:
        result["evidence"] = {
            "strategy": "no_aging_columns_found",
            "col_titles": col_titles,
            "max_age_days": max_age_days,
        }
        return result

    # Walk through rows to find items
    rows = report.get("Rows", {}).get("Row", []) or []
    items: list[dict[str, Any]] = []
    total_amount = Decimal("0")

    for row in rows:
        row_type = row.get("type", "")
        if row_type == "Data":
            col_data = row.get("ColData", []) or []
            item_label = col_data[0].get("value", "") if col_data else ""

            for idx in aging_col_indices:
                if idx < len(col_data):
                    val = col_data[idx].get("value", "")
                    amount = parse_money(val)
                    if amount is not None and abs(amount) > Decimal("0.01"):
                        items.append(
                            {
                                "label": item_label,
                                "aging_column": col_titles[idx] if idx < len(col_titles) else f"col_{idx}",
                                "amount": val,
                            }
                        )
                        total_amount += amount
                        if len(items) >= limit:
                            break
            if len(items) >= limit:
                break

    result["items"] = items
    result["total_over_threshold"] = str(total_amount) if items else None
    result["evidence"] = {
        "strategy": "aging_column_scan",
        "aging_col_indices": aging_col_indices,
        "col_titles": col_titles,
    }
    return result


def qbo_report_permission_denied(e: Exception) -> bool:
    """Check if exception indicates QBO report permission denied."""
    err_str = str(e).lower()
    return "permission" in err_str or "403" in err_str or "access denied" in err_str


# ---------------------------------------------------------------------------
# Evaluation Handlers
# ---------------------------------------------------------------------------


def eval_requires_external_reconciliation_verification(
    rule: dict[str, Any], ctx: MERBalanceSheetEvaluationContext
) -> dict[str, Any]:
    """Handler for rules requiring external reconciliation verification."""
    return {
        "status": "needs_human_review",
        "details": {
            "rule": rule.get("title"),
            "period_end_date": ctx.end_date,
            "reason": "requires_external_reconciliation_verification",
            "required_sources": _extract_rule_required_sources(rule),
            "action_items": (
                _extract_rule_action_items(rule)
                + [
                    "provide_reconciliation_status_and_statement_date",
                    "attach_evidence_links_or_workpaper_reference",
                ]
            ),
            "notes": (
                "This check depends on reconciliation evidence (statement date / reconciled-through / status). "
                "If that evidence is not API-accessible, it must come from a reconciliation spreadsheet or manual attestation."
            ),
        },
    }


def eval_needs_human_judgment(
    rule: dict[str, Any], ctx: MERBalanceSheetEvaluationContext
) -> dict[str, Any]:
    """Handler for rules requiring human judgment."""
    return {
        "status": "needs_human_review",
        "details": {
            "rule": rule.get("title"),
            "period_end_date": ctx.end_date,
            "reason": "needs_human_judgment",
            "required_sources": _extract_rule_required_sources(rule),
            "action_items": (
                _extract_rule_action_items(rule)
                + [
                    "human_review_required",
                    "record_evidence_links_or_rationale",
                ]
            ),
        },
    }


def eval_manual_process_required(
    rule: dict[str, Any], ctx: MERBalanceSheetEvaluationContext
) -> dict[str, Any]:
    """Handler for rules requiring manual processes."""
    return {
        "status": "needs_human_review",
        "details": {
            "rule": rule.get("title"),
            "period_end_date": ctx.end_date,
            "reason": "manual_process_required",
            "required_sources": _extract_rule_required_sources(rule),
            "action_items": (
                _extract_rule_action_items(rule)
                + [
                    "follow_internal_process",
                    "document_outcome_and_link_evidence_if_any",
                ]
            ),
            "parameters": (rule.get("parameters") or {}),
        },
    }


def eval_needs_prior_cycle_context(
    rule: dict[str, Any], ctx: MERBalanceSheetEvaluationContext
) -> dict[str, Any]:
    """Handler for rules requiring prior cycle context."""
    return {
        "status": "needs_human_review",
        "details": {
            "rule": rule.get("title"),
            "period_end_date": ctx.end_date,
            "reason": "needs_prior_cycle_context",
            "required_sources": _extract_rule_required_sources(rule),
            "action_items": (
                _extract_rule_action_items(rule)
                + [
                    "provide_prior_cycle_reference",
                    "confirm_whether_item_is_new_or_preexisting",
                ]
            ),
        },
    }


def eval_mer_lines_require_link_to_support(
    rule: dict[str, Any], ctx: MERBalanceSheetEvaluationContext
) -> dict[str, Any]:
    """Handler for checking MER lines have supporting links/comments."""
    comments_col, comments_col_mode = _resolve_mer_comments_col_index(
        rows=ctx.mer_rows,
        header_row_index=ctx.mer_header_row_index,
    )
    month_col = _find_col_index_by_header_contains(
        rows=ctx.mer_rows,
        header_row_index=ctx.mer_header_row_index,
        header_contains=ctx.mer_selected_month_header,
    )

    if comments_col is None or month_col is None:
        return {
            "status": "failed",
            "details": {
                "rule": rule.get("title"),
                "reason": "Missing required MER column",
                "required": {
                    "month_header": ctx.mer_selected_month_header,
                    "comments_column": "F (preferred) or header contains 'comments'",
                },
                "found": {
                    "month_col_index": month_col,
                    "comments_col_index": comments_col,
                    "comments_col_resolution": comments_col_mode,
                    "header_row_index": ctx.mer_header_row_index,
                },
            },
        }

    missing: list[dict[str, Any]] = []
    applicable_count = 0

    start = (ctx.mer_header_row_index or 0) + 1
    for row_index in range(start, len(ctx.mer_rows)):
        row = ctx.mer_rows[row_index] or []
        label = (row[0] if row else "") or ""
        if _is_non_line_item_label(label):
            continue

        amount_raw = row[month_col] if month_col < len(row) else None
        amount = parse_money(amount_raw)
        if amount is None:
            continue
        if abs(amount) <= ctx.zero_tolerance:
            continue

        applicable_count += 1
        comment_raw = row[comments_col] if comments_col < len(row) else None
        comment_present = bool((comment_raw or "").strip())

        if not comment_present:
            missing.append(
                {
                    "mer_row_index": row_index,
                    "mer_label": label,
                    "mer_amount_raw": amount_raw,
                    "mer_amount": str(amount),
                    "comments_a1_cell": _a1_cell(row_index, comments_col),
                }
            )

    status = "passed" if not missing else "failed"
    if applicable_count == 0:
        status = "skipped"

    return {
        "status": status,
        "details": {
            "rule": rule.get("title"),
            "period_end_date": ctx.end_date,
            "selected_month_header": ctx.mer_selected_month_header,
            "comments_col_index": comments_col,
            "comments_col_resolution": comments_col_mode,
            "applicable_nonzero_lines": applicable_count,
            "missing_support_count": len(missing),
            "missing_support": missing[:50],
            "note": "Support is read from the MER Balance Sheet 'Comments' column; any non-empty comment/link counts as supported.",
        },
    }


def eval_support_link_presence_check(
    rule: dict[str, Any], ctx: MERBalanceSheetEvaluationContext
) -> dict[str, Any]:
    """Handler for checking support link presence on line items."""
    # If rule requires non-MER sources, keep as human review
    required_sources = set(_extract_rule_required_sources(rule))
    non_mer_sources = sorted([s for s in required_sources if s != "mer_google_sheet"])
    if non_mer_sources or bool(rule.get("manual_attestation_required")):
        return {
            "status": "needs_human_review",
            "details": {
                "rule": rule.get("title"),
                "period_end_date": ctx.end_date,
                "reason": "support_link_presence_check_requires_external_sources",
                "required_sources": sorted(required_sources),
                "action_items": (
                    _extract_rule_action_items(rule)
                    + ["attach_evidence_links_or_workpaper_reference"]
                ),
                "notes": "This rule requires external sources/attestation; automation only validates MER comments/links when MER-only.",
            },
        }

    comments_col, comments_col_mode = _resolve_mer_comments_col_index(
        rows=ctx.mer_rows,
        header_row_index=ctx.mer_header_row_index,
    )
    month_col = _find_col_index_by_header_contains(
        rows=ctx.mer_rows,
        header_row_index=ctx.mer_header_row_index,
        header_contains=ctx.mer_selected_month_header,
    )
    if comments_col is None or month_col is None:
        return {
            "status": "failed",
            "details": {
                "rule": rule.get("title"),
                "reason": "Missing required MER column",
                "required": {
                    "month_header": ctx.mer_selected_month_header,
                    "comments_column": "F (preferred) or header contains 'comments'",
                },
                "found": {
                    "month_col_index": month_col,
                    "comments_col_index": comments_col,
                    "comments_col_resolution": comments_col_mode,
                    "header_row_index": ctx.mer_header_row_index,
                },
            },
        }

    title = str(rule.get("title") or "").lower()
    rid = str(rule.get("rule_id") or "").upper()

    # For loan schedule link checks, scope to loan-like rows.
    loan_tokens = [
        "loan",
        "line of credit",
        "credit line",
        "loc",
        "note payable",
        "mortgage",
        "debt",
    ]
    is_loan_rule = ("loan" in rid) or ("loan" in title) or ("repayment" in title) or ("schedule" in title)

    missing: list[dict[str, Any]] = []
    applicable_count = 0

    start = (ctx.mer_header_row_index or 0) + 1
    for row_index in range(start, len(ctx.mer_rows)):
        row = ctx.mer_rows[row_index] or []
        label = (row[0] if row else "") or ""
        if _is_non_line_item_label(label):
            continue

        amount_raw = row[month_col] if month_col < len(row) else None
        amount = parse_money(amount_raw)
        if amount is None:
            continue
        if abs(amount) <= ctx.zero_tolerance:
            continue

        label_l = label.lower()
        if is_loan_rule and not any(tok in label_l for tok in loan_tokens):
            continue

        applicable_count += 1
        comment_raw = row[comments_col] if comments_col < len(row) else None
        comment_present = bool((comment_raw or "").strip())

        if not comment_present:
            missing.append(
                {
                    "mer_row_index": row_index,
                    "mer_label": label,
                    "mer_amount_raw": amount_raw,
                    "mer_amount": str(amount),
                    "comments_a1_cell": _a1_cell(row_index, comments_col),
                }
            )

    if applicable_count == 0:
        return {
            "status": "skipped",
            "details": {
                "rule": rule.get("title"),
                "period_end_date": ctx.end_date,
                "reason": "No applicable MER lines found",
                "scoping": "loan_lines_only" if is_loan_rule else "nonzero_lines",
            },
        }

    return {
        "status": "passed" if not missing else "failed",
        "details": {
            "rule": rule.get("title"),
            "period_end_date": ctx.end_date,
            "selected_month_header": ctx.mer_selected_month_header,
            "comments_col_index": comments_col,
            "comments_col_resolution": comments_col_mode,
            "missing_support_count": len(missing),
            "missing_support": missing[:50],
            "scoping": "loan_lines_only" if is_loan_rule else "nonzero_lines",
            "note": "Support is read from the MER Balance Sheet 'Comments' column; any non-empty comment/link counts as supported.",
        },
    }


def eval_balance_sheet_line_items_must_be_zero(
    rule: dict[str, Any], ctx: MERBalanceSheetEvaluationContext
) -> dict[str, Any]:
    """Handler for checking balance sheet lines are zero."""
    substrings = (
        ((rule.get("applies_to") or {}).get("qbo_balance_sheet_lines") or {})
        .get("label_contains_any")
        or []
    )
    if not isinstance(substrings, list) or not substrings:
        return {
            "status": "skipped",
            "reason": "No label_contains_any substrings configured",
        }

    substring = str(substrings[0])
    mer_matches = find_values_for_rows_containing(
        rows=ctx.mer_rows,
        row_substring=substring,
        col_header=ctx.mer_selected_month_header,
        header_row_index=ctx.mer_header_row_index,
    )

    check = check_zero_on_both_sides_by_substring(
        check_id=str(rule.get("rule_id") or ""),
        mer_lines=[(m.row_text, m.value) for m in mer_matches],
        qbo_lines=ctx.qbo_balance_sheet_items,
        label_substring=substring,
        tolerance=ctx.zero_tolerance,
        rule=rule.get("title") or "Balance sheet line items must be zero",
    )
    return {
        "status": "passed" if check.passed else "failed",
        "details": check.details,
    }


def eval_mer_line_amount_matches_qbo_line_amount(
    rule: dict[str, Any], ctx: MERBalanceSheetEvaluationContext
) -> dict[str, Any]:
    """Handler for checking MER line amount matches QBO."""
    substrings = (
        ((rule.get("applies_to") or {}).get("qbo_balance_sheet_lines") or {})
        .get("label_contains_any")
        or []
    )
    if not isinstance(substrings, list) or not substrings:
        return {
            "status": "skipped",
            "reason": "No label_contains_any configured",
        }

    substring = str(substrings[0])
    mer_candidates = find_values_for_rows_containing(
        rows=ctx.mer_rows,
        row_substring=substring,
        col_header=ctx.mer_selected_month_header,
        header_row_index=ctx.mer_header_row_index,
    )
    qbo_raw = find_first_amount(ctx.qbo_balance_sheet_items, substring)
    qbo_amount = parse_money(qbo_raw)

    if len(mer_candidates) != 1:
        return {
            "status": "failed",
            "details": {
                "rule": rule.get("title"),
                "reason": "MER match ambiguous or missing (expected exactly one match)",
                "mer_matches": [
                    {
                        "a1_cell": m.a1_cell,
                        "row_text": m.row_text,
                        "value": m.value,
                    }
                    for m in mer_candidates
                ],
                "qbo_first_match_raw": qbo_raw,
            },
        }

    mer_amount = parse_money(mer_candidates[0].value)
    check = check_petty_cash_matches(
        mer_amount=mer_amount,
        qbo_amount=qbo_amount,
        tolerance=ctx.amount_match_tolerance,
    )
    return {
        "status": "passed" if check.passed else "failed",
        "details": {
            **check.details,
            "mer_a1_cell": mer_candidates[0].a1_cell,
            "mer_row_text": mer_candidates[0].row_text,
            "qbo_label_substring": substring,
            "qbo_first_match_raw": qbo_raw,
        },
    }


def eval_mer_bank_balance_matches_qbo_bank_balance(
    rule: dict[str, Any], ctx: MERBalanceSheetEvaluationContext
) -> dict[str, Any]:
    """Handler for checking MER bank balance matches QBO."""
    params = rule.get("parameters") or {}
    mer_bank_row_key = params.get("mer_bank_row_key") or ctx.mer_bank_row_key
    qbo_bank_label_substring = params.get("qbo_bank_label_substring") or ctx.qbo_bank_label_substring

    if not mer_bank_row_key or not qbo_bank_label_substring:
        return {
            "status": "skipped",
            "reason": "Provide parameters.mer_bank_row_key + parameters.qbo_bank_label_substring (or request-level overrides)",
        }

    mer_lookup = find_value_in_table(
        rows=ctx.mer_rows,
        row_key=str(mer_bank_row_key),
        col_header=ctx.mer_selected_month_header,
        header_row_index=ctx.mer_header_row_index,
    )
    mer_amount = parse_money(mer_lookup.value)
    qbo_raw = find_first_amount(ctx.qbo_balance_sheet_items, str(qbo_bank_label_substring))
    qbo_amount = parse_money(qbo_raw)
    check = check_bank_balance_matches(
        mer_amount=mer_amount,
        qbo_amount=qbo_amount,
        tolerance=ctx.amount_match_tolerance,
    )
    return {
        "status": "passed" if check.passed else "failed",
        "details": {
            **check.details,
            "mer_row_key": str(mer_bank_row_key),
            "mer_a1_cell": mer_lookup.a1_cell,
            "qbo_label_substring": str(qbo_bank_label_substring),
            "qbo_first_match_raw": qbo_raw,
        },
    }


# ---------------------------------------------------------------------------
# Handler registry mapping
# ---------------------------------------------------------------------------


HANDLER_REGISTRY: dict[str, Any] = {
    "requires_external_reconciliation_verification": eval_requires_external_reconciliation_verification,
    "needs_human_judgment": eval_needs_human_judgment,
    "manual_process_required": eval_manual_process_required,
    "needs_prior_cycle_context": eval_needs_prior_cycle_context,
    "mer_lines_require_link_to_support": eval_mer_lines_require_link_to_support,
    "support_link_presence_check": eval_support_link_presence_check,
    "balance_sheet_line_items_must_be_zero": eval_balance_sheet_line_items_must_be_zero,
    "mer_line_amount_matches_qbo_line_amount": eval_mer_line_amount_matches_qbo_line_amount,
    "mer_bank_balance_matches_qbo_bank_balance": eval_mer_bank_balance_matches_qbo_bank_balance,
    # The remaining 4 handlers are more complex and use ctx.qbo_client
    # They will be registered separately in the engine module for now
}
