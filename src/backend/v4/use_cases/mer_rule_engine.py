"""Rulebook-driven evaluation engine for MER reviews.

Goal
- Move evaluation-type dispatch (previously embedded in the API router) into a
  reusable, unit-testable engine.
- Keep outputs evidence-first and deterministic where possible.

This module intentionally avoids FastAPI types/exceptions.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable

from src.backend.v4.integrations.qbo_reports import (
    extract_aged_detail_items_over_threshold,
    extract_report_total_value,
    find_first_amount,
)
from src.backend.v4.use_cases.mer_review_checks import (
    check_bank_balance_matches,
    check_petty_cash_matches,
    check_zero_on_both_sides_by_substring,
    parse_money,
)
from src.backend.v4.integrations.google_sheets_reader import (
    find_value_in_table,
    find_values_for_rows_containing,
)


def _norm_text(s: str | None) -> str:
    return "".join(ch.lower() for ch in (s or "") if ch.isalnum())


def _col_to_a1(col_index_zero_based: int) -> str:
    if col_index_zero_based < 0:
        raise ValueError("col_index_zero_based must be >= 0")
    result = ""
    n = col_index_zero_based
    while True:
        n, rem = divmod(n, 26)
        result = chr(ord("A") + rem) + result
        if n == 0:
            break
        n -= 1
    return result


def _a1_cell(row_index_zero_based: int, col_index_zero_based: int) -> str:
    return f"{_col_to_a1(col_index_zero_based)}{row_index_zero_based + 1}"


def _find_col_index_by_header_contains(
    *, rows: list[list[str]], header_row_index: int | None, header_contains: str
) -> int | None:
    if header_row_index is None:
        return None
    if header_row_index < 0 or header_row_index >= len(rows):
        return None
    header = rows[header_row_index] or []
    needle = _norm_text(header_contains)
    if not needle:
        return None
    for j, cell in enumerate(header):
        if needle in _norm_text(cell):
            return j
    return None


def _resolve_mer_comments_col_index(
    *, rows: list[list[str]], header_row_index: int | None
) -> tuple[int | None, str]:
    """Resolve the MER comments column index.

    Preference order:
    1) Column F (index 5) if present in the header row (per workflow convention).
    2) A header-based lookup for a column whose header contains "comments".

    Returns (col_index, resolution_mode).
    """

    # Convention: MER Balance Sheet comments live in column F.
    fixed_col_index = 5
    if header_row_index is not None and 0 <= header_row_index < len(rows):
        header = rows[header_row_index] or []
        if len(header) > fixed_col_index:
            return fixed_col_index, "fixed_column_f"

    by_header = _find_col_index_by_header_contains(
        rows=rows,
        header_row_index=header_row_index,
        header_contains="comments",
    )
    if by_header is not None:
        return by_header, "header_contains_comments"

    return None, "missing"


def _looks_like_link(s: str | None) -> bool:
    t = (s or "").strip()
    if not t:
        return False
    tl = t.lower()
    return (
        "http://" in tl
        or "https://" in tl
        or "drive.google.com" in tl
        or "docs.google.com" in tl
        or "=hyperlink(" in tl
    )


def _is_non_line_item_label(label: str) -> bool:
    ll = (label or "").strip().lower()
    if not ll:
        return True
    # Heuristics: section headers / totals.
    if ll in {"assets", "liabilities", "equity", "liabilities and equity"}:
        return True
    if "total" in ll:
        return True
    return False


def qbo_report_permission_denied(err: Exception) -> bool:
    """Detect QBO Reports API permission denial for a report name.

    QBO commonly returns HTTP 400 with ValidationFault code 5020 and element
    ReportName when the app/user lacks entitlement for a specific report.
    """

    msg = str(err)
    return "Permission Denied" in msg and "ReportName" in msg and "5020" in msg


@dataclass(frozen=True, slots=True)
class MERBalanceSheetEvaluationContext:
    end_date: str
    mer_rows: list[list[str]]
    mer_selected_month_header: str
    mer_header_row_index: int
    qbo_balance_sheet_items: Any
    qbo_client: Any
    zero_tolerance: Decimal
    amount_match_tolerance: Decimal
    mer_bank_row_key: str | None = None
    qbo_bank_label_substring: str | None = None
    client_maintenance_rows: list[list[str]] | None = None
    kyc_rows: list[list[str]] | None = None


EvaluationHandler = Callable[[dict[str, Any], MERBalanceSheetEvaluationContext], dict[str, Any]]


def _extract_rule_required_sources(rule: dict[str, Any]) -> list[str]:
    req = rule.get("requires_external_sources")
    if not isinstance(req, list):
        return []
    out: list[str] = []
    for v in req:
        if isinstance(v, str) and v.strip():
            out.append(v.strip())
    return sorted(set(out))


def _extract_rule_action_items(rule: dict[str, Any]) -> list[str]:
    actions: set[str] = set()

    if bool(rule.get("manual_attestation_required")):
        actions.add("manual_attestation_required")

    sop = rule.get("sop_expectation")
    if isinstance(sop, dict) and bool(sop.get("required_step")):
        actions.add("required_manual_review_step")

    def _walk(obj: Any) -> None:
        if isinstance(obj, dict):
            act = obj.get("action")
            if isinstance(act, str) and act.strip():
                actions.add(act.strip())
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for v in obj:
                _walk(v)

    pa = rule.get("process_actions")
    if pa is not None:
        _walk(pa)

    return sorted(actions)


class EvaluationRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, EvaluationHandler] = {}

    def register(self, eval_type: str) -> Callable[[EvaluationHandler], EvaluationHandler]:
        def _decorator(fn: EvaluationHandler) -> EvaluationHandler:
            self._handlers[eval_type] = fn
            return fn

        return _decorator

    def get(self, eval_type: str) -> EvaluationHandler | None:
        return self._handlers.get(eval_type)

    def implemented_types(self) -> set[str]:
        return set(self._handlers.keys())


def collect_action_items(rulebook_doc: dict[str, Any]) -> list[dict[str, Any]]:
    rules = rulebook_doc.get("rules") or []
    if not isinstance(rules, list):
        return []

    def _walk_for_actions(obj: Any, out: set[str]) -> None:
        if isinstance(obj, dict):
            act = obj.get("action")
            if isinstance(act, str) and act.strip():
                out.add(act.strip())
            for v in obj.values():
                _walk_for_actions(v, out)
        elif isinstance(obj, list):
            for v in obj:
                _walk_for_actions(v, out)

    items: list[dict[str, Any]] = []
    limit = max(int(os.environ.get("MER_AGENT_ACTION_ITEMS_LIMIT", "10")), 0)

    for r in rules:
        if not isinstance(r, dict):
            continue
        rid = r.get("rule_id")
        if not rid:
            continue

        actions: set[str] = set()

        if bool(r.get("manual_attestation_required")):
            actions.add("manual_attestation_required")

        sop = r.get("sop_expectation")
        if isinstance(sop, dict) and bool(sop.get("required_step")):
            actions.add("required_manual_review_step")

        pa = r.get("process_actions")
        if pa is not None:
            _walk_for_actions(pa, actions)

        if actions:
            items.append(
                {
                    "rule_id": str(rid),
                    "title": str(r.get("title") or ""),
                    "actions": sorted(actions),
                }
            )

        if limit and len(items) >= limit:
            break

    return items


class MERBalanceSheetRuleEngine:
    """Evaluate Balance Sheet rules from the YAML rulebook."""

    def __init__(self, registry: EvaluationRegistry | None = None) -> None:
        self._registry = registry or _default_registry()

    @property
    def registry(self) -> EvaluationRegistry:
        return self._registry

    def evaluate(self, *, rulebook: dict[str, Any], ctx: MERBalanceSheetEvaluationContext) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []

        rules = rulebook.get("rules") or []
        if not isinstance(rules, list):
            return results

        for rule in rules:
            if not isinstance(rule, dict):
                continue

            if rule.get("enabled") is False:
                results.append(
                    {
                        "rule_id": rule.get("rule_id"),
                        "status": "skipped",
                        "reason": "disabled_by_rulebook",
                        "evaluation_type": ((rule.get("evaluation") or {}).get("type")),
                    }
                )
                continue

            rule_id = rule.get("rule_id")
            eval_type = (rule.get("evaluation") or {}).get("type")
            if not rule_id or not eval_type:
                continue

            handler = self._registry.get(str(eval_type))
            if handler is None:
                results.append(
                    {
                        "rule_id": rule_id,
                        "status": "unimplemented",
                        "evaluation_type": eval_type,
                    }
                )
                continue

            out = handler(rule, ctx)
            # Normalize the result payload shape.
            out.setdefault("rule_id", rule_id)
            out.setdefault("evaluation_type", eval_type)
            results.append(out)

        return results


def _default_registry() -> EvaluationRegistry:
    reg = EvaluationRegistry()

    # --- Human / external-evidence required handlers ---

    @reg.register("requires_external_reconciliation_verification")
    def _eval_requires_external_reconciliation_verification(
        rule: dict[str, Any], ctx: MERBalanceSheetEvaluationContext
    ) -> dict[str, Any]:
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

    @reg.register("needs_human_judgment")
    def _eval_needs_human_judgment(
        rule: dict[str, Any], ctx: MERBalanceSheetEvaluationContext
    ) -> dict[str, Any]:
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

    @reg.register("manual_process_required")
    def _eval_manual_process_required(
        rule: dict[str, Any], ctx: MERBalanceSheetEvaluationContext
    ) -> dict[str, Any]:
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

    @reg.register("needs_prior_cycle_context")
    def _eval_needs_prior_cycle_context(
        rule: dict[str, Any], ctx: MERBalanceSheetEvaluationContext
    ) -> dict[str, Any]:
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

    @reg.register("mer_lines_require_link_to_support")
    def _eval_mer_lines_require_link_to_support(
        rule: dict[str, Any], ctx: MERBalanceSheetEvaluationContext
    ) -> dict[str, Any]:
        # Convention: MER Balance Sheet comments are in column F.
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

    @reg.register("support_link_presence_check")
    def _eval_support_link_presence_check(
        rule: dict[str, Any], ctx: MERBalanceSheetEvaluationContext
    ) -> dict[str, Any]:
        # This evaluation type is used by multiple rules. If a rule requires
        # sources beyond the MER sheet, keep it as human review (so we don't
        # incorrectly mark it passed based on MER comments alone).
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

    @reg.register("inventory_accounts_must_exist_in_qbo_and_mer")
    def _eval_inventory_accounts_must_exist_in_qbo_and_mer(
        rule: dict[str, Any], ctx: MERBalanceSheetEvaluationContext
    ) -> dict[str, Any]:
        # Prefer explicit kyc_rows; fall back to client_maintenance_rows for backwards compat
        kyc_rows = ctx.kyc_rows or ctx.client_maintenance_rows
        if not kyc_rows:
            return {
                "status": "needs_human_review",
                "details": {
                    "rule": rule.get("title"),
                    "period_end_date": ctx.end_date,
                    "reason": "missing_client_maintenance_rows",
                    "required_sources": _extract_rule_required_sources(rule),
                    "action_items": (
                        _extract_rule_action_items(rule)
                        + [
                            "provide_client_maintenance_spreadsheet_id_and_range",
                            "confirm_inventory_tab_schema",
                        ]
                    ),
                },
            }

        def _norm_name(s: str | None) -> str:
            return "".join(ch.lower() for ch in (s or "") if ch.isalnum())

        header_row_index: int | None = None
        for i, r in enumerate(kyc_rows[:25]):
            row_joined = " ".join(str(c or "") for c in (r or []))
            nr = _norm_name(row_joined)
            if "account" in nr and ("type" in nr or "credit" in nr or "bank" in nr):
                header_row_index = i
                break

        if header_row_index is None:
            header_row_index = 0

        header = kyc_rows[header_row_index] if header_row_index < len(kyc_rows) else []
        header_norm = [_norm_name(str(c or "")) for c in (header or [])]

        def _find_col(*needles: str) -> int | None:
            for idx, hn in enumerate(header_norm):
                if not hn:
                    continue
                if all(_norm_name(n) in hn for n in needles if _norm_name(n)):
                    return idx
            return None

        account_col = _find_col("account") or _find_col("name")
        if account_col is None:
            # fallback: first non-empty header
            for j, hn in enumerate(header_norm):
                if hn:
                    account_col = j
                    break

        type_col = _find_col("type") or _find_col("account", "type")
        # optional column where clients may provide the expected QBO label or mapping
        qbo_label_col = _find_col("qbo") or _find_col("qbo", "label") or _find_col("expected", "qbo") or _find_col("qbo", "name")

        if account_col is None:
            return {
                "status": "failed",
                "details": {
                    "rule": rule.get("title"),
                    "reason": "Could not locate account column in client maintenance sheet",
                    "header_row_index": header_row_index,
                    "header": header,
                },
            }

        # Extract inventory entries.
        inventory: list[dict[str, str]] = []
        for r in kyc_rows[header_row_index + 1 :]:
            row = r or []
            acct = str(row[account_col] if account_col < len(row) else "").strip()
            if not acct:
                continue
            acct_type = str(row[type_col] if (type_col is not None and type_col < len(row)) else "").strip()
            expected_qbo_label = (
                str(row[qbo_label_col]).strip()
                if (qbo_label_col is not None and qbo_label_col < len(row))
                else ""
            )
            inventory.append({
                "account_name": acct,
                "account_type": acct_type,
                "expected_qbo_label": expected_qbo_label,
            })

        if not inventory:
            return {
                "status": "skipped",
                "details": {
                    "rule": rule.get("title"),
                    "reason": "No inventory entries found in client maintenance sheet",
                    "header_row_index": header_row_index,
                },
            }

        qbo_accounts = []
        try:
            qbo_accounts = ctx.qbo_client.get_accounts(max_results=1000)
        except Exception:
            # If QBO access is not available for accounts, fall back to human review.
            return {
                "status": "needs_human_review",
                "details": {
                    "rule": rule.get("title"),
                    "period_end_date": ctx.end_date,
                    "reason": "qbo_accounts_unavailable",
                    "required_sources": _extract_rule_required_sources(rule),
                    "action_items": (
                        _extract_rule_action_items(rule)
                        + ["ensure_qbo_chart_of_accounts_access"]
                    ),
                },
            }

        qbo_names = [str(a.get("Name") or "") for a in (qbo_accounts or []) if isinstance(a, dict)]
        qbo_norm = {_norm_name(n): n for n in qbo_names if _norm_name(n)}

        def _qbo_has_account(name: str) -> bool:
            nn = _norm_name(name)
            if not nn:
                return False
            if nn in qbo_norm:
                return True
            # fallback: substring match (client naming differences)
            return any(nn in qn or qn in nn for qn in qbo_norm.keys())

        def _mer_has_line(name: str) -> bool:
            needle = (name or "").strip().lower()
            if not needle:
                return False
            start = (ctx.mer_header_row_index or 0) + 1
            for row in ctx.mer_rows[start:]:
                label = str((row or [""])[0] or "")
                if needle in label.lower():
                    return True
            return False

        findings: list[dict[str, Any]] = []
        missing_qbo = 0
        missing_mer = 0

        for inv in inventory:
            name = inv.get("account_name") or ""
            # Prefer explicit expected_qbo_label when provided to find QBO account matches
            expected = inv.get("expected_qbo_label") or ""
            in_qbo = False
            qbo_match_detail = None
            if expected:
                in_qbo = _qbo_has_account(expected)
                qbo_match_detail = "expected_label_matched"
            else:
                in_qbo = _qbo_has_account(name)
                qbo_match_detail = "name_based_match"
            in_mer = _mer_has_line(name)
            if not in_qbo:
                missing_qbo += 1
            if not in_mer:
                missing_mer += 1
            findings.append(
                {
                    "inventory_account_name": name,
                    "inventory_account_type": inv.get("account_type") or "",
                    "expected_qbo_label": inv.get("expected_qbo_label") or "",
                    "exists_in_qbo_chart_of_accounts": in_qbo,
                    "qbo_match_method": qbo_match_detail,
                    "represented_in_mer": in_mer,
                }
            )

        status = "passed" if (missing_qbo == 0 and missing_mer == 0) else "failed"

        return {
            "status": status,
            "details": {
                "rule": rule.get("title"),
                "period_end_date": ctx.end_date,
                "inventory_count": len(inventory),
                "missing_in_qbo_count": missing_qbo,
                "missing_in_mer_count": missing_mer,
                "header_row_index": header_row_index,
                "account_name_col_index": account_col,
                "account_type_col_index": type_col,
                "findings": findings[:200],
                "note": "This is a best-effort inventory coverage check; exact column names and per-client mapping may need tuning.",
            },
        }

    @reg.register("balance_sheet_line_items_must_be_zero")
    def _eval_balance_sheet_line_items_must_be_zero(
        rule: dict[str, Any], ctx: MERBalanceSheetEvaluationContext
    ) -> dict[str, Any]:
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

    @reg.register("mer_line_amount_matches_qbo_line_amount")
    def _eval_mer_line_amount_matches_qbo_line_amount(
        rule: dict[str, Any], ctx: MERBalanceSheetEvaluationContext
    ) -> dict[str, Any]:
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

    @reg.register("mer_bank_balance_matches_qbo_bank_balance")
    def _eval_mer_bank_balance_matches_qbo_bank_balance(
        rule: dict[str, Any], ctx: MERBalanceSheetEvaluationContext
    ) -> dict[str, Any]:
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

    @reg.register("mer_credit_debit_accounts_book_balance_match_qbo")
    def _eval_mer_credit_debit_accounts_book_balance_match_qbo(
        rule: dict[str, Any], ctx: MERBalanceSheetEvaluationContext
    ) -> dict[str, Any]:
        params = rule.get("parameters") or {}
        include_tokens = params.get("qbo_include_label_contains_any")
        if not isinstance(include_tokens, list) or not include_tokens:
            include_tokens = [
                "bank",
                "chequing",
                "checking",
                "savings",
                "rbc",
                "paypal",
                "etsy",
                "clearing",
                "credit card",
                "visa",
                "mastercard",
                "amex",
                "discover",
                "line of credit",
                "loc",
            ]

        exclude_tokens = params.get("qbo_exclude_label_contains_any")
        if not isinstance(exclude_tokens, list) or not exclude_tokens:
            exclude_tokens = [
                "undeposited",
                "accounts receivable",
                "a/r",
                "accounts payable",
                "a/p",
                "inventory",
                "prepaid",
                "equipment",
                "furnish",
                "goodwill",
                "security deposit",
                "accumulated",
                "amortization",
                "depreciation",
                "gst",
                "hst",
                "pst",
                "sales tax",
                "income tax",
                "accrued",
                "vacation",
                "unearned",
                "wages",
                "petty cash",
            ]

        include_lowered = [str(k).strip().lower() for k in include_tokens if isinstance(k, str) and k.strip()]
        exclude_lowered = [str(k).strip().lower() for k in exclude_tokens if isinstance(k, str) and k.strip()]

        def _is_reconcilable_label(label: str) -> bool:
            ll = (label or "").strip().lower()
            if not ll:
                return False
            if "undeposited" in ll:
                return False
            if any(bad in ll for bad in exclude_lowered):
                return False
            return any(tok in ll for tok in include_lowered)

        items = ctx.qbo_balance_sheet_items or []
        candidates = [
            it
            for it in items
            if hasattr(it, "label") and _is_reconcilable_label(str(getattr(it, "label", "") or ""))
        ]

        missing_mer: list[str] = []
        mismatches: list[dict[str, Any]] = []

        for it in candidates:
            qbo_label = str(getattr(it, "label", "") or "")
            qbo_amount_raw = str(getattr(it, "amount", "") or "")
            qbo_amount = parse_money(qbo_amount_raw)

            mer_matches = find_values_for_rows_containing(
                rows=ctx.mer_rows,
                row_substring=qbo_label,
                col_header=ctx.mer_selected_month_header,
                header_row_index=ctx.mer_header_row_index,
            )
            if not mer_matches:
                missing_mer.append(qbo_label)
                continue

            for m in mer_matches:
                mer_amount = parse_money(m.value)
                delta = (
                    mer_amount - qbo_amount
                    if mer_amount is not None and qbo_amount is not None
                    else None
                )
                passed = (
                    mer_amount is not None
                    and qbo_amount is not None
                    and abs(delta or Decimal("0")) <= ctx.amount_match_tolerance
                )
                if not passed:
                    mismatches.append(
                        {
                            "qbo_label": qbo_label,
                            "qbo_amount": qbo_amount_raw,
                            "mer_a1_cell": m.a1_cell,
                            "mer_value_raw": m.value,
                            "delta": str(delta) if delta is not None else None,
                        }
                    )

        status = "passed"
        if not candidates:
            status = "skipped"
        elif mismatches or missing_mer:
            status = "failed"

        return {
            "status": status,
            "evidence": {
                "include_tokens": include_lowered,
                "exclude_tokens": exclude_lowered,
                "qbo_candidates_count": len(candidates),
                "missing_mer_count": len(missing_mer),
                "missing_mer_labels": missing_mer[:20],
                "mismatches_count": len(mismatches),
                "mismatches": mismatches[:20],
                "tolerance": str(ctx.amount_match_tolerance),
                "note": "MVP book-balance match only; does not prove statement reconciliation. Candidate selection is heuristic (external-statement-like accounts).",
            },
        }

    @reg.register("qbo_report_total_matches_balance_sheet_line")
    def _eval_qbo_report_total_matches_balance_sheet_line(
        rule: dict[str, Any], ctx: MERBalanceSheetEvaluationContext
    ) -> dict[str, Any]:
        qbo_reports_required = (rule.get("evaluation") or {}).get("qbo_reports_required") or []
        if not isinstance(qbo_reports_required, list) or not qbo_reports_required:
            return {
                "status": "skipped",
                "reason": "Missing evaluation.qbo_reports_required",
            }

        aging_report: dict[str, Any] | None = None
        bs_label_substring: str | None = None
        required_tokens: list[str] = []

        if "aged_payables_detail" in qbo_reports_required:
            try:
                aging_report = ctx.qbo_client.get_aged_payables_total(end_date=ctx.end_date)
            except Exception as e:
                if qbo_report_permission_denied(e):
                    return {
                        "status": "skipped",
                        "details": {
                            "rule": rule.get("title"),
                            "reason": "blocked_by_qbo_report_permission",
                            "report": "AgedPayables*",
                            "error": str(e),
                        },
                    }
                raise
            bs_label_substring = "accounts payable"
            required_tokens = ["total"]
        elif "aged_receivables_detail" in qbo_reports_required:
            try:
                aging_report = ctx.qbo_client.get_aged_receivables_total(end_date=ctx.end_date)
            except Exception as e:
                if qbo_report_permission_denied(e):
                    return {
                        "status": "skipped",
                        "details": {
                            "rule": rule.get("title"),
                            "reason": "blocked_by_qbo_report_permission",
                            "report": "AgedReceivables*",
                            "error": str(e),
                        },
                    }
                raise
            bs_label_substring = "accounts receivable"
            required_tokens = ["total"]
        else:
            return {
                "status": "skipped",
                "reason": f"Unsupported qbo_reports_required: {qbo_reports_required}",
            }

        total_raw, total_evidence = extract_report_total_value(
            aging_report or {},
            total_row_must_contain=required_tokens,
            prefer_column_titles=["Total"],
        )
        total_amount = parse_money(total_raw)

        bs_raw = find_first_amount(ctx.qbo_balance_sheet_items, bs_label_substring or "")
        bs_amount = parse_money(bs_raw)

        if total_amount is None or bs_amount is None:
            return {
                "status": "failed",
                "details": {
                    "rule": rule.get("title"),
                    "reason": "Could not parse totals from QBO reports",
                    "period_end_date": ctx.end_date,
                    "balance_sheet_label_substring": bs_label_substring,
                    "balance_sheet_amount_raw": bs_raw,
                    "aging_report_total_raw": total_raw,
                    "aging_report_evidence": total_evidence,
                },
            }

        delta = total_amount - bs_amount
        passed = abs(delta) <= ctx.amount_match_tolerance
        return {
            "status": "passed" if passed else "failed",
            "details": {
                "rule": rule.get("title"),
                "period_end_date": ctx.end_date,
                "balance_sheet_label_substring": bs_label_substring,
                "balance_sheet_amount_raw": bs_raw,
                "balance_sheet_amount": str(bs_amount),
                "aging_report_total_raw": total_raw,
                "aging_report_total": str(total_amount),
                "tolerance": str(ctx.amount_match_tolerance),
                "delta": str(delta),
                "aging_report_evidence": total_evidence,
            },
        }

    @reg.register("qbo_aging_items_older_than_threshold_require_explanation")
    def _eval_qbo_aging_items_older_than_threshold_require_explanation(
        rule: dict[str, Any], ctx: MERBalanceSheetEvaluationContext
    ) -> dict[str, Any]:
        params = rule.get("parameters") or {}
        max_age_days = params.get("max_age_days")
        try:
            max_age_days_int = int(str(max_age_days))
        except Exception:
            return {
                "status": "skipped",
                "reason": "parameters.max_age_days must be an integer",
            }

        limit = max(int(os.environ.get("MER_AGENT_AGING_ITEMS_LIMIT", "100")), 0)

        try:
            ap_report = ctx.qbo_client.get_aged_payables_detail(end_date=ctx.end_date)
            ar_report = ctx.qbo_client.get_aged_receivables_detail(end_date=ctx.end_date)
        except Exception as e:
            if qbo_report_permission_denied(e):
                return {
                    "status": "skipped",
                    "details": {
                        "rule": rule.get("title"),
                        "reason": "blocked_by_qbo_report_permission",
                        "reports": ["AgedPayables*", "AgedReceivables*"],
                        "error": str(e),
                    },
                }
            raise

        ap = extract_aged_detail_items_over_threshold(
            ap_report or {}, max_age_days=max_age_days_int, limit=limit
        )
        ar = extract_aged_detail_items_over_threshold(
            ar_report or {}, max_age_days=max_age_days_int, limit=limit
        )

        ap_items = ap.get("items") or []
        ar_items = ar.get("items") or []

        comments_col, comments_col_mode = _resolve_mer_comments_col_index(
            rows=ctx.mer_rows,
            header_row_index=ctx.mer_header_row_index,
        )

        def _mer_explanation_for(substring: str) -> dict[str, Any]:
            out: dict[str, Any] = {
                "mer_label_substring": substring,
                "matched_rows": [],
                "explanation_present": False,
            }
            if comments_col is None:
                out["reason"] = "missing_comments_column"
                return out

            start = (ctx.mer_header_row_index or 0) + 1
            for row_index in range(start, len(ctx.mer_rows)):
                row = ctx.mer_rows[row_index] or []
                label = (row[0] if row else "") or ""
                if substring.lower() not in (label or "").lower():
                    continue
                comment_raw = row[comments_col] if comments_col < len(row) else None
                comment_present = bool((comment_raw or "").strip())
                out["matched_rows"].append(
                    {
                        "mer_row_index": row_index,
                        "mer_label": label,
                        "comments_a1_cell": _a1_cell(row_index, comments_col),
                        "comment_present": comment_present,
                    }
                )
                if comment_present:
                    out["explanation_present"] = True

            if not out["matched_rows"]:
                out["reason"] = "no_matching_mer_row"
            return out

        ap_expl = _mer_explanation_for("accounts payable") if ap_items else None
        ar_expl = _mer_explanation_for("accounts receivable") if ar_items else None

        # Pass logic:
        # - If no findings: pass.
        # - If findings exist: require at least one relevant MER comment/link for that section.
        ap_ok = (ap_expl is None) or bool(ap_expl.get("explanation_present"))
        ar_ok = (ar_expl is None) or bool(ar_expl.get("explanation_present"))
        passed = ap_ok and ar_ok

        return {
            "status": "passed" if passed else "failed",
            "details": {
                "rule": rule.get("title"),
                "period_end_date": ctx.end_date,
                "max_age_days": max_age_days_int,
                "requires_explanation": True,
                "explanation_mode": "mer_comments_column",
                "comments_col_index": comments_col,
                "comments_col_resolution": comments_col_mode,
                "ap": {
                    "count": len(ap_items),
                    "total_over_threshold": ap.get("total_over_threshold"),
                    "items": ap_items,
                    "evidence": ap.get("evidence"),
                    "mer_explanation": ap_expl,
                },
                "ar": {
                    "count": len(ar_items),
                    "total_over_threshold": ar.get("total_over_threshold"),
                    "items": ar_items,
                    "evidence": ar.get("evidence"),
                    "mer_explanation": ar_expl,
                },
                "action": "If any open items are > threshold, add an explanation/comment/link in MER comments column (F) for AP and/or AR.",
            },
        }

    return reg
