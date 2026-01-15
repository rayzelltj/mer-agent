#!/usr/bin/env python3

import argparse
import asyncio
import json
import os
import random
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date as _date
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv


load_dotenv(override=False)


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RULEBOOK = REPO_ROOT / "data" / "mer_rulebooks" / "balance_sheet_review_points.yaml"

# Ensure imports like `from src.backend...` work when executing from `scripts/`.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load_azd_env_into_process() -> None:
    """Best-effort load of azd env vars into this process.

    This lets the script work without manually sourcing `azd env get-values`.
    """

    if os.environ.get("AZURE_OPENAI_ENDPOINT") and os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME"):
        return

    try:
        timeout_s = float(os.environ.get("MER_AGENT_AZD_TIMEOUT_SECONDS", "5"))
        proc = subprocess.run(
            ["azd", "env", "get-values"],
            text=True,
            capture_output=True,
            timeout=timeout_s,
            check=True,
        )
        out = proc.stdout
    except Exception:
        return

    # Lines are like: KEY="value"
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_val = line.split("=", 1)
        key = key.strip()
        raw_val = raw_val.strip()
        if not key:
            continue
        if raw_val.startswith('"') and raw_val.endswith('"'):
            val = raw_val[1:-1]
        else:
            val = raw_val
        # Don't clobber explicit env vars
        os.environ.setdefault(key, val)


def _decimal_from_amount_str(amount_str: str | None) -> Decimal:
    try:
        return Decimal(str(amount_str))
    except Exception:
        return Decimal("0.00")


def _load_rulebook_yaml(path: Path) -> dict:
    import yaml  # type: ignore

    if not path.exists():
        raise FileNotFoundError(f"Rulebook not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}

def _normalize_date_from_text(text: str) -> Optional[str]:
    # Simple YYYY-MM-DD extractor
    m = re.search(r"(\d{4}-\d{2}-\d{2})", text)
    if not m:
        return None
    try:
        _date.fromisoformat(m.group(1))
        return m.group(1)
    except Exception:
        return None


def _wants_only_five_bullets(prompt: str) -> bool:
    # Keep this intentionally simple and robust to phrasing variations.
    p = prompt.lower()
    return ("5-bullet" in p or "five-bullet" in p) and "only" in p


def _requested_bullet_count(prompt: str) -> Optional[int]:
    p = prompt.lower()

    # Common forms:
    # - "summarize in 3 bullets"
    # - "output ONLY a 5-bullet summary"
    # - "3-bullet summary"
    m = re.search(r"\b(\d+)\s*-?\s*bullets?\b", p)
    if m:
        try:
            n = int(m.group(1))
            return n if 1 <= n <= 10 else None
        except Exception:
            return None

    # Word numbers (minimal set; can extend if needed)
    if "three bullets" in p or "3 bullets" in p:
        return 3
    if "five bullets" in p or "5 bullets" in p:
        return 5
    return None


def _explicit_tool_directive(prompt: str, tool_name: str) -> bool:
    # e.g. "Call mer_balance_sheet_review" or "call qbo_balance_sheet".
    return re.search(rf"\bcall\s+{re.escape(tool_name)}\b", prompt, flags=re.IGNORECASE) is not None


def _clip(s: str, max_len: int) -> str:
    if max_len >= 0 and len(s) > max_len:
        return s[:max_len] + "…"
    return s


def _format_generic_tool_bullets(last_tool_results: list[dict], *, bullets: int) -> str:
    qbo = next((r for r in last_tool_results if r.get("tool") == "qbo_balance_sheet"), None)
    mer_entries = next((r for r in last_tool_results if r.get("tool") == "mer_balance_sheet_entries"), None)

    parts: list[str] = []

    # Period (best effort)
    end_date = None
    if qbo and isinstance(qbo.get("result"), dict):
        end_date = qbo["result"].get("end_date")
    if not end_date and mer_entries and isinstance(mer_entries.get("result"), dict):
        end_date = mer_entries["result"].get("end_date")
    if end_date:
        parts.append(f"- Period end: {end_date}")

    # QBO preview
    if qbo and isinstance(qbo.get("result"), dict):
        res = qbo["result"]
        items = res.get("items") or []
        top_lines: list[str] = []
        try:
            parsed = []
            for it in items:
                if not isinstance(it, dict):
                    continue
                label = str(it.get("label") or "").strip()
                amt = _decimal_from_amount_str(it.get("amount"))
                if label:
                    parsed.append((abs(amt), label, it.get("amount")))
            parsed.sort(reverse=True, key=lambda x: x[0])
            for _, label, raw in parsed[:3]:
                top_lines.append(f"{label}={raw}")
        except Exception:
            pass
        preview = "; ".join(top_lines) if top_lines else "(preview unavailable)"
        preview = _clip(preview.replace("\n", " "), 180)
        parts.append(
            f"- QBO balance sheet: items={res.get('count')} (preview_count={res.get('preview_count')}); top: {preview}"
        )

    # MER sheet preview
    if mer_entries and isinstance(mer_entries.get("result"), dict):
        res = mer_entries["result"]
        mer = res.get("mer") or {}
        entries = res.get("entries") or []
        entry_preview: list[str] = []
        for e in entries[:3]:
            if isinstance(e, dict):
                label = str(e.get("label") or "").strip()
                val = str(e.get("value") or "").strip()
                if label and val:
                    entry_preview.append(f"{label}={val}")
        preview = "; ".join(entry_preview) if entry_preview else "(no entries returned)"
        preview = _clip(preview.replace("\n", " "), 180)
        parts.append(
            f"- MER sheet: month={mer.get('selected_month_header')} entries={res.get('count')}; preview: {preview}"
        )

    # If we still have room, add a short note about limits.
    parts.append("- Note: previews are truncated; increase limits via env vars/tools args if needed")

    bullets = max(1, min(int(bullets), len(parts)))
    return "\n".join(parts[:bullets])


def _format_mer_review_bullets(result: dict, *, bullets: int) -> str:
    summary = result.get("summary") or {}
    failed = result.get("failed") or []
    action_items = result.get("action_items") or []
    mer = result.get("mer") or {}
    policies = result.get("policies") or {}
    qbo = result.get("qbo") or {}
    req = result.get("requires_clarification") or []

    failed_preview: str
    if failed:
        failed_preview = "; ".join(
            [
                f"{f.get('rule_id')}: {((f.get('evidence') or {}).get('message') or (f.get('evaluation_type') or 'failed'))}"
                for f in failed[:3]
            ]
        )
    else:
        failed_preview = "None"

    failed_preview = _clip(str(failed_preview).replace("\n", " "), 160)

    # Clarification items are dicts; only include their ids to keep output short.
    clarification_ids: list[str] = []
    if isinstance(req, list):
        for x in req:
            if isinstance(x, dict) and x.get("id"):
                clarification_ids.append(str(x.get("id")))
            elif isinstance(x, str):
                clarification_ids.append(x)
            if len(clarification_ids) >= 3:
                break

    clarification_preview = ", ".join(clarification_ids) if clarification_ids else "None"

    action_preview: str
    if isinstance(action_items, list) and action_items:
        parts: list[str] = []
        for x in action_items[:3]:
            if not isinstance(x, dict):
                continue
            rid = str(x.get("rule_id") or "").strip()
            acts = x.get("actions")
            act_s = ""
            if isinstance(acts, list):
                act_s = ",".join([str(a) for a in acts if a])
            elif isinstance(acts, str):
                act_s = acts
            if rid and act_s:
                parts.append(f"{rid}: {act_s}")
            elif rid:
                parts.append(rid)
        action_preview = "; ".join(parts) if parts else "None"
    else:
        action_preview = "None"
    action_preview = _clip(str(action_preview).replace("\n", " "), 160)

    candidates = [
        f"- Period end: {result.get('period_end_date')} (MER month: {mer.get('selected_month_header')})",
        (
            "- Checks: "
            f"passed={summary.get('passed')} failed={summary.get('failed')} "
            f"unimplemented={summary.get('unimplemented')} skipped={summary.get('skipped')}"
        ),
        f"- Failed (preview): {failed_preview}",
        f"- Action items: {action_preview}",
        f"- Clarification needed: {clarification_preview}",
        (
            f"- QBO extracted items: {qbo.get('balance_sheet_items_extracted')}; "
            f"tolerances: zero={policies.get('zero_tolerance')} amount_match={policies.get('amount_match_tolerance')}"
        ),
    ]

    # Exactly N bullets, no extra lines.
    bullets = max(1, min(int(bullets), len(candidates)))
    return "\n".join(candidates[:bullets])


def tool_qbo_balance_sheet(end_date: str) -> dict:
    from src.backend.v4.integrations.qbo_client import QBOClient
    from src.backend.v4.integrations.qbo_reports import extract_balance_sheet_items

    _date.fromisoformat(end_date)

    qbo = QBOClient.from_env()
    report = qbo.get_balance_sheet(end_date=end_date, start_date=end_date)
    items = extract_balance_sheet_items(report)

    # Keep tool output compact to reduce token usage (helps avoid 429 TPM limits).
    preview_limit = int(os.environ.get("MER_AGENT_QBO_PREVIEW_LIMIT", "50"))
    preview = items[: max(preview_limit, 0)]
    return {
        "end_date": end_date,
        "count": len(items),
        "preview_count": len(preview),
        "items": [{"label": i.label, "amount": i.amount} for i in preview],
    }


def tool_mer_balance_sheet_review(
    *,
    end_date: str,
    mer_sheet: Optional[str] = None,
    mer_range: Optional[str] = None,
    mer_month_header: Optional[str] = None,
    rulebook_path: Optional[str] = None,
) -> dict:
    """Deterministic MER Balance Sheet review (same logic as our local backend endpoint).

    Returns a structured JSON payload with a mix of implemented and unimplemented rules.
    """

    from src.backend.v4.integrations.google_sheets_reader import (
        GoogleSheetsReader,
        find_values_for_rows_containing,
    )
    from src.backend.v4.integrations.qbo_client import QBOClient
    from src.backend.v4.integrations.qbo_reports import (
        extract_balance_sheet_items,
        find_first_amount,
        extract_aged_detail_items_over_threshold,
        extract_report_total_value,
    )
    from src.backend.v4.use_cases.mer_review_checks import (
        check_bank_balance_matches,
        check_petty_cash_matches,
        check_zero_on_both_sides_by_substring,
        parse_money,
        pick_latest_month_header,
    )

    _date.fromisoformat(end_date)

    rulebook = _load_rulebook_yaml(Path(rulebook_path) if rulebook_path else DEFAULT_RULEBOOK)

    policies = (rulebook.get("rulebook") or {}).get("policies") or {}
    tolerances = policies.get("tolerances") or {}
    zero_amount = ((tolerances.get("zero_balance") or {}).get("amount"))
    zero_tolerance = _decimal_from_amount_str(zero_amount)

    # amount_match explicitly requires clarification in the rulebook; default exact match
    amount_match_tolerance = Decimal("0.00")

    # Fetch MER sheet
    reader = GoogleSheetsReader.from_env()

    sheet = mer_sheet
    if not sheet:
        titles = reader.list_sheet_titles()
        if "Balance Sheet" in titles:
            sheet = "Balance Sheet"
        else:
            raise ValueError(f"mer_sheet is required. Available sheets: {titles}")

    mer_range = mer_range or f"'{sheet}'!A1:Z1000"
    rows = reader.fetch_rows(a1_range=mer_range)
    if not rows:
        raise ValueError("No rows returned from Google Sheets")

    # Identify month header
    header_row_index: int | None = None
    selected_month = mer_month_header

    if not selected_month:
        for i, r in enumerate(rows[:25]):
            candidate = pick_latest_month_header(r)
            if candidate:
                header_row_index = i
                selected_month = candidate
                break
        if selected_month is None or header_row_index is None:
            raise ValueError("Could not find a month header row in first 25 rows")
    else:
        for i, r in enumerate(rows[:25]):
            if any((c or "").strip() for c in r):
                header_row_index = i
                break
        if header_row_index is None:
            raise ValueError("Could not detect header row")

    # Fetch QBO balance sheet
    qbo = QBOClient.from_env()
    report = qbo.get_balance_sheet(end_date=end_date, start_date=end_date)
    qbo_items = extract_balance_sheet_items(report)

    implemented_types = {
        "balance_sheet_line_items_must_be_zero",
        "mer_line_amount_matches_qbo_line_amount",
        "mer_bank_balance_matches_qbo_bank_balance",
        "qbo_report_total_matches_balance_sheet_line",
        "qbo_aging_items_older_than_threshold_require_explanation",
    }

    results: list[dict] = []

    def _qbo_report_permission_denied(err: Exception) -> bool:
        # QBO returns code 5020 with element ReportName for some reports when the app/user
        # lacks entitlement/permission. We treat this as a "skipped" (blocked) rule so
        # the remainder of the review can still run.
        msg = str(err)
        return (
            "Permission Denied" in msg
            and ("\"code\":\"5020\"" in msg or "'code':'5020'" in msg or "code\":\"5020" in msg)
            and "ReportName" in msg
        )

    def _collect_action_items(rulebook_doc: dict) -> list[dict]:
        rules = (rulebook_doc.get("rules") or [])
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

        items: list[dict] = []
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

    def _truncate_evidence(obj: Any) -> Any:
        """Keep tool payloads small to avoid Azure OpenAI TPM 429s."""
        max_list = int(os.environ.get("MER_AGENT_EVIDENCE_LIST_LIMIT", "5"))
        max_str = int(os.environ.get("MER_AGENT_EVIDENCE_STRING_LIMIT", "350"))
        if isinstance(obj, dict):
            out: dict[str, Any] = {}
            for k, v in obj.items():
                out[k] = _truncate_evidence(v)
            return out
        if isinstance(obj, list):
            if max_list >= 0 and len(obj) > max_list:
                return {
                    "truncated": True,
                    "total": len(obj),
                    "items": [_truncate_evidence(x) for x in obj[:max_list]],
                }
            return [_truncate_evidence(x) for x in obj]
        if isinstance(obj, str):
            if max_str >= 0 and len(obj) > max_str:
                return obj[:max_str] + "…"
        return obj

    for rule in (rulebook.get("rules") or []):
        if not isinstance(rule, dict):
            continue
        rule_id = rule.get("rule_id")
        eval_type = ((rule.get("evaluation") or {}).get("type"))
        if not rule_id or not eval_type:
            continue

        if eval_type not in implemented_types:
            results.append({"rule_id": rule_id, "status": "unimplemented", "evaluation_type": eval_type})
            continue

        if eval_type == "balance_sheet_line_items_must_be_zero":
            substrings = (
                ((rule.get("applies_to") or {}).get("qbo_balance_sheet_lines") or {})
                .get("label_contains_any")
                or []
            )
            if not isinstance(substrings, list) or not substrings:
                results.append({"rule_id": rule_id, "status": "skipped", "evaluation_type": eval_type})
                continue

            substring = str(substrings[0])
            mer_matches = find_values_for_rows_containing(
                rows=rows,
                row_substring=substring,
                col_header=selected_month,
                header_row_index=header_row_index,
            )

            mer_lines = [(m.row_text, m.value) for m in mer_matches]
            check = check_zero_on_both_sides_by_substring(
                check_id=str(rule_id),
                mer_lines=mer_lines,
                qbo_lines=qbo_items,
                label_substring=substring,
                tolerance=zero_tolerance,
                rule=str(rule.get("description") or "Line items must be zero"),
            )

            results.append(
                {
                    "rule_id": rule_id,
                    "status": "passed" if check.passed else "failed",
                    "evaluation_type": eval_type,
                    "evidence": _truncate_evidence(check.details),
                }
            )
            continue

        if eval_type == "mer_line_amount_matches_qbo_line_amount":
            mer_rule = (rule.get("applies_to") or {}).get("mer_line") or {}
            qbo_rule = (rule.get("applies_to") or {}).get("qbo_balance_sheet_line") or {}

            mer_row_key = mer_rule.get("row_key")
            qbo_label = qbo_rule.get("label_contains")

            mer_matches = []
            if mer_row_key:
                mer_matches = find_values_for_rows_containing(
                    rows=rows,
                    row_substring=str(mer_row_key),
                    col_header=selected_month,
                    header_row_index=header_row_index,
                )

            mer_amount = parse_money(mer_matches[0].value) if mer_matches else None
            qbo_amount_raw = find_first_amount(qbo_items, name_substring=str(qbo_label or ""))
            qbo_amount = parse_money(qbo_amount_raw)

            check = check_petty_cash_matches(
                mer_amount=mer_amount,
                qbo_amount=qbo_amount,
                tolerance=amount_match_tolerance,
            )
            # Override check_id to the rule_id for reporting consistency
            results.append(
                {
                    "rule_id": rule_id,
                    "status": "passed" if check.passed else "failed",
                    "evaluation_type": eval_type,
                    "evidence": _truncate_evidence({**check.details, "check_id": str(rule_id)}),
                }
            )
            continue

        if eval_type == "mer_bank_balance_matches_qbo_bank_balance":
            mer_bank_row_key = ((rule.get("applies_to") or {}).get("mer_line") or {}).get("row_key")
            qbo_bank_substring = (
                ((rule.get("applies_to") or {}).get("qbo_balance_sheet_lines") or {})
                .get("label_contains_any")
                or []
            )
            qbo_bank_sub = str(qbo_bank_substring[0]) if qbo_bank_substring else ""

            mer_matches = []
            if mer_bank_row_key:
                mer_matches = find_values_for_rows_containing(
                    rows=rows,
                    row_substring=str(mer_bank_row_key),
                    col_header=selected_month,
                    header_row_index=header_row_index,
                )

            mer_amount = parse_money(mer_matches[0].value) if mer_matches else None
            qbo_amount_raw = find_first_amount(qbo_items, name_substring=qbo_bank_sub)
            qbo_amount = parse_money(qbo_amount_raw)

            check = check_bank_balance_matches(
                mer_amount=mer_amount,
                qbo_amount=qbo_amount,
                tolerance=amount_match_tolerance,
            )

            results.append(
                {
                    "rule_id": rule_id,
                    "status": "passed" if check.passed else "failed",
                    "evaluation_type": eval_type,
                    "evidence": _truncate_evidence(check.details),
                }
            )
            continue

        if eval_type == "qbo_report_total_matches_balance_sheet_line":
            qbo_reports_required = (
                (rule.get("evaluation") or {}).get("qbo_reports_required") or []
            )
            if not isinstance(qbo_reports_required, list) or not qbo_reports_required:
                results.append({"rule_id": rule_id, "status": "skipped", "evaluation_type": eval_type})
                continue

            aging_report = None
            bs_label_substring = None
            required_tokens: list[str] = []
            if "aged_payables_detail" in qbo_reports_required:
                try:
                    # In some tenants, AgedPayablesDetail is denied but AgedPayables works.
                    aging_report = qbo.get_aged_payables_total(end_date=end_date)
                except Exception as e:
                    if _qbo_report_permission_denied(e):
                        results.append(
                            {
                                "rule_id": rule_id,
                                "status": "skipped",
                                "evaluation_type": eval_type,
                                "evidence": _truncate_evidence(
                                    {
                                        "reason": "blocked_by_qbo_report_permission",
                                        "report": "AgedPayables*",
                                        "error": str(e),
                                    }
                                ),
                            }
                        )
                        continue
                    raise
                bs_label_substring = "accounts payable"
                # AgedPayables often uses a row label literally called "TOTAL".
                required_tokens = ["total"]
            elif "aged_receivables_detail" in qbo_reports_required:
                try:
                    aging_report = qbo.get_aged_receivables_total(end_date=end_date)
                except Exception as e:
                    if _qbo_report_permission_denied(e):
                        results.append(
                            {
                                "rule_id": rule_id,
                                "status": "skipped",
                                "evaluation_type": eval_type,
                                "evidence": _truncate_evidence(
                                    {
                                        "reason": "blocked_by_qbo_report_permission",
                                        "report": "AgedReceivables*",
                                        "error": str(e),
                                    }
                                ),
                            }
                        )
                        continue
                    raise
                bs_label_substring = "accounts receivable"
                required_tokens = ["total"]
            else:
                results.append({"rule_id": rule_id, "status": "skipped", "evaluation_type": eval_type})
                continue

            total_raw, total_evidence = extract_report_total_value(
                aging_report or {},
                total_row_must_contain=required_tokens,
                prefer_column_titles=["Total"],
            )

            total_amount = parse_money(total_raw)
            bs_raw = find_first_amount(qbo_items, str(bs_label_substring or ""))
            bs_amount = parse_money(bs_raw)

            if total_amount is None or bs_amount is None:
                results.append(
                    {
                        "rule_id": rule_id,
                        "status": "failed",
                        "evaluation_type": eval_type,
                        "evidence": _truncate_evidence(
                            {
                                "reason": "Could not parse totals from QBO reports",
                                "balance_sheet_amount_raw": bs_raw,
                                "aging_report_total_raw": total_raw,
                                "aging_report_evidence": total_evidence,
                            }
                        ),
                    }
                )
                continue

            delta = total_amount - bs_amount
            passed = abs(delta) <= amount_match_tolerance
            results.append(
                {
                    "rule_id": rule_id,
                    "status": "passed" if passed else "failed",
                    "evaluation_type": eval_type,
                    "evidence": _truncate_evidence(
                        {
                            "balance_sheet_label_substring": bs_label_substring,
                            "balance_sheet_amount_raw": bs_raw,
                            "balance_sheet_amount": str(bs_amount),
                            "aging_report_total_raw": total_raw,
                            "aging_report_total": str(total_amount),
                            "tolerance": str(amount_match_tolerance),
                            "delta": str(delta),
                            "aging_report_evidence": total_evidence,
                        }
                    ),
                }
            )
            continue

        if eval_type == "qbo_aging_items_older_than_threshold_require_explanation":
            params = rule.get("parameters") or {}
            max_age_days = params.get("max_age_days")
            if max_age_days is None:
                results.append({"rule_id": rule_id, "status": "skipped", "evaluation_type": eval_type})
                continue
            try:
                max_age_days_int = int(max_age_days)
            except Exception:
                results.append({"rule_id": rule_id, "status": "skipped", "evaluation_type": eval_type})
                continue

            limit = max(int(os.environ.get("MER_AGENT_AGING_ITEMS_LIMIT", "100")), 0)

            try:
                ap_report = qbo.get_aged_payables_detail(end_date=end_date)
                ar_report = qbo.get_aged_receivables_detail(end_date=end_date)
            except Exception as e:
                if _qbo_report_permission_denied(e):
                    results.append(
                        {
                            "rule_id": rule_id,
                            "status": "skipped",
                            "evaluation_type": eval_type,
                            "evidence": _truncate_evidence(
                                {
                                    "reason": "blocked_by_qbo_report_permission",
                                    "reports": ["AgedPayables*", "AgedReceivables*"],
                                    "error": str(e),
                                }
                            ),
                        }
                    )
                    continue
                raise

            ap = extract_aged_detail_items_over_threshold(
                ap_report or {}, max_age_days=max_age_days_int, limit=limit
            )
            ar = extract_aged_detail_items_over_threshold(
                ar_report or {}, max_age_days=max_age_days_int, limit=limit
            )

            ap_items = ap.get("items") or []
            ar_items = ar.get("items") or []
            has_findings = bool(ap_items) or bool(ar_items)

            results.append(
                {
                    "rule_id": rule_id,
                    "status": "failed" if has_findings else "passed",
                    "evaluation_type": eval_type,
                    "evidence": _truncate_evidence(
                        {
                            "period_end_date": end_date,
                            "max_age_days": max_age_days_int,
                            "requires_explanation": True,
                            "explanation_mode": "manual",
                            "ap": {
                                "count": len(ap_items),
                                "total_over_threshold": ap.get("total_over_threshold"),
                                "items": ap_items,
                                "evidence": ap.get("evidence"),
                            },
                            "ar": {
                                "count": len(ar_items),
                                "total_over_threshold": ar.get("total_over_threshold"),
                                "items": ar_items,
                                "evidence": ar.get("evidence"),
                            },
                            "action": "Provide an explanation/comment/link for each > threshold open AP/AR item",
                        }
                    ),
                }
            )
            continue

    # Compact the payload: keep summary + failed evidence, omit huge unimplemented list.
    passed = sum(1 for r in results if r.get("status") == "passed")
    failed = sum(1 for r in results if r.get("status") == "failed")
    unimplemented = sum(1 for r in results if r.get("status") == "unimplemented")
    skipped = sum(1 for r in results if r.get("status") == "skipped")
    max_failed = int(os.environ.get("MER_AGENT_FAILED_RULES_LIMIT", "10"))
    failed_results = [
        {
            "rule_id": r.get("rule_id"),
            "evaluation_type": r.get("evaluation_type"),
            "evidence": r.get("evidence"),
        }
        for r in results
        if r.get("status") == "failed"
    ][: max(max_failed, 0)]

    max_skipped = int(os.environ.get("MER_AGENT_SKIPPED_RULES_LIMIT", "10"))
    skipped_results = [
        {
            "rule_id": r.get("rule_id"),
            "evaluation_type": r.get("evaluation_type"),
            "evidence": r.get("evidence"),
        }
        for r in results
        if r.get("status") == "skipped"
    ][: max(max_skipped, 0)]

    max_implemented = int(os.environ.get("MER_AGENT_IMPLEMENTED_RULES_LIMIT", "50"))
    implemented_results = [
        {
            "rule_id": r.get("rule_id"),
            "status": r.get("status"),
            "evaluation_type": r.get("evaluation_type"),
            "evidence": r.get("evidence"),
        }
        for r in results
        if r.get("evaluation_type") in implemented_types and r.get("status") != "unimplemented"
    ][: max(max_implemented, 0)]

    return {
        "period_end_date": end_date,
        "mer": {
            "sheet": sheet,
            "range": mer_range,
            "selected_month_header": selected_month,
        },
        "qbo": {"balance_sheet_items_extracted": len(qbo_items)},
        "policies": {
            "zero_tolerance": str(zero_tolerance),
            "amount_match_tolerance": str(amount_match_tolerance),
            "amount_match_requires_clarification": bool(
                (((tolerances.get("amount_match") or {}).get("requires_clarification")))
            ),
        },
        "requires_clarification": list(
            ((rulebook.get("rulebook") or {}).get("requires_clarification", []) or [])
        )[: max(int(os.environ.get("MER_AGENT_CLARIFICATION_LIMIT", "10")), 0)],
        "summary": {
            "passed": passed,
            "failed": failed,
            "unimplemented": unimplemented,
            "skipped": skipped,
            "total_considered": len(results),
        },
        "failed": failed_results,
        "skipped": skipped_results,
        "implemented": implemented_results,
        "action_items": _collect_action_items(rulebook),
    }


def tool_mer_balance_sheet_entries(
    *,
    end_date: str,
    mer_sheet: Optional[str] = None,
    mer_range: Optional[str] = None,
    mer_month_header: Optional[str] = None,
    limit: int = 30,
) -> dict:
    """Fetch MER Balance Sheet line entries from Google Sheets for a given period.

    This is a read-only helper for LLM tool-calling: it returns compact label/value pairs.
    """

    from src.backend.v4.integrations.google_sheets_reader import (
        GoogleSheetsReader,
    )
    from src.backend.v4.use_cases.mer_review_checks import pick_latest_month_header

    _date.fromisoformat(end_date)

    reader = GoogleSheetsReader.from_env()

    sheet = mer_sheet
    if not sheet:
        titles = reader.list_sheet_titles()
        if "Balance Sheet" in titles:
            sheet = "Balance Sheet"
        else:
            raise ValueError(f"mer_sheet is required. Available sheets: {titles}")

    mer_range = mer_range or f"'{sheet}'!A1:Z1000"
    rows = reader.fetch_rows(a1_range=mer_range)
    if not rows:
        raise ValueError("No rows returned from Google Sheets")

    # Determine header row and month header
    header_row_index: int | None = None
    selected_month = mer_month_header

    if not selected_month:
        for i, r in enumerate(rows[:25]):
            candidate = pick_latest_month_header(r)
            if candidate:
                header_row_index = i
                selected_month = candidate
                break
        if selected_month is None or header_row_index is None:
            raise ValueError("Could not find a month header row in first 25 rows")
    else:
        for i, r in enumerate(rows[:25]):
            if any((c or "").strip() for c in r):
                header_row_index = i
                break
        if header_row_index is None:
            raise ValueError("Could not detect header row")

    header = rows[header_row_index]
    month_col: int | None = None

    # Try exact match first
    for j, c in enumerate(header):
        if (c or "").strip() == (selected_month or "").strip():
            month_col = j
            break

    # Fall back: find a column containing the year and a month token
    if month_col is None and selected_month:
        year_match = re.search(r"(\d{4})", selected_month)
        year = year_match.group(1) if year_match else ""
        month_token = (selected_month.split()[0] if selected_month.split() else "").strip(".")
        for j, c in enumerate(header):
            cell = (c or "")
            if year and year in cell and month_token and month_token.lower() in cell.lower():
                month_col = j
                break

    if month_col is None:
        raise ValueError(f"Could not find month column for header '{selected_month}'")

    # Collect label/value pairs; assume first column is label.
    entries: list[dict] = []
    for r in rows[header_row_index + 1 :]:
        if not r:
            continue
        label = (r[0] if len(r) > 0 else "") or ""
        value = (r[month_col] if len(r) > month_col else "") or ""
        if not str(label).strip():
            continue
        if not str(value).strip():
            continue
        entries.append({"label": str(label).strip(), "value": str(value).strip()})
        if limit >= 0 and len(entries) >= limit:
            break

    return {
        "end_date": end_date,
        "mer": {
            "sheet": sheet,
            "range": mer_range,
            "selected_month_header": selected_month,
            "month_col_index": month_col,
        },
        "count": len(entries),
        "entries": entries,
    }


def _make_tools_schema() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "qbo_balance_sheet",
                "description": "Fetch QuickBooks Online Balance Sheet for a given period end date (YYYY-MM-DD).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "end_date": {"type": "string", "description": "ISO date YYYY-MM-DD"}
                    },
                    "required": ["end_date"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "mer_balance_sheet_entries",
                "description": "Fetch MER Balance Sheet entries from Google Sheets for a period (read-only).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "end_date": {"type": "string", "description": "ISO date YYYY-MM-DD"},
                        "mer_sheet": {"type": "string"},
                        "mer_range": {"type": "string"},
                        "mer_month_header": {"type": "string"},
                        "limit": {"type": "integer", "description": "Max entries to return"},
                    },
                    "required": ["end_date"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "mer_balance_sheet_review",
                "description": "Run deterministic MER Balance Sheet review checks (reads Google Sheet + QBO; never writes).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "end_date": {"type": "string", "description": "ISO date YYYY-MM-DD"},
                        "mer_sheet": {"type": "string"},
                        "mer_range": {"type": "string"},
                        "mer_month_header": {"type": "string"},
                        "rulebook_path": {"type": "string"},
                    },
                    "required": ["end_date"],
                    "additionalProperties": False,
                },
            },
        },
    ]


def _run_tool(name: str, args: dict) -> dict:
    if name == "qbo_balance_sheet":
        return tool_qbo_balance_sheet(end_date=args["end_date"])
    if name == "mer_balance_sheet_entries":
        return tool_mer_balance_sheet_entries(**args)
    if name == "mer_balance_sheet_review":
        return tool_mer_balance_sheet_review(**args)
    raise ValueError(f"Unknown tool: {name}")


async def run_agent(prompt: str, max_steps: int) -> str:
    _load_azd_env_into_process()

    verbose = os.environ.get("MER_AGENT_VERBOSE", "0") == "1"

    def _log(msg: str) -> None:
        if verbose:
            print(f"[mer-agent] {msg}", file=sys.stderr)

    # Fast-path: if the user explicitly instructs calling the deterministic tool(s),
    # we can run them directly and avoid Azure OpenAI TPM/RPM rate limits entirely.
    tool_only = os.environ.get("MER_AGENT_TOOL_ONLY", "0") == "1"
    wants_5_only = _wants_only_five_bullets(prompt)
    bullet_count = _requested_bullet_count(prompt) or (5 if wants_5_only else 5)

    if tool_only or _explicit_tool_directive(prompt, "mer_balance_sheet_review"):
        end_date = _normalize_date_from_text(prompt)
        if not end_date:
            return "- Missing end_date (YYYY-MM-DD)"
        result = tool_mer_balance_sheet_review(end_date=end_date)
        want_full_json = os.environ.get("MER_AGENT_PRINT_TOOL_JSON", "0") == "1"
        want_full_json = want_full_json or ("full tool result json" in prompt.lower())
        want_full_json = want_full_json or ("print the full tool result" in prompt.lower())

        bullets = _format_mer_review_bullets(result, bullets=bullet_count)
        if not want_full_json:
            return bullets

        payload = json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True)
        return payload + "\n\n" + bullets

    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
    deployment = os.environ.get("MER_AGENT_LLM_DEPLOYMENT") or os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME")
    api_version = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-11-20")
    auth_mode = os.environ.get("MER_AGENT_AUTH", "aad").lower().strip()
    api_key = os.environ.get("AZURE_OPENAI_API_KEY")

    if not endpoint or not deployment:
        raise RuntimeError(
            "Missing AZURE_OPENAI_ENDPOINT or AZURE_OPENAI_DEPLOYMENT_NAME. "
            "Run `azd env get-values` or set them in your environment."
        )

    from openai import AzureOpenAI
    from openai import RateLimitError

    print(f"[config] using deployment={deployment} api_version={api_version}", file=sys.stderr)
    print(f"[config] using endpoint={endpoint}", file=sys.stderr)

    if auth_mode in {"api_key", "apikey", "key"}:
        if not api_key:
            raise RuntimeError(
                "MER_AGENT_AUTH=api_key but AZURE_OPENAI_API_KEY is not set. "
                "Either set AZURE_OPENAI_API_KEY or use MER_AGENT_AUTH=aad."
            )
        _log("auth=api_key")
        client = AzureOpenAI(
            azure_endpoint=endpoint,
            api_version=api_version,
            api_key=api_key,
        )
    else:
        # AAD auth can hang locally if a credential tries to prompt.
        # Fail fast by excluding common interactive sources and prefetching a token with a timeout.
        from azure.identity import DefaultAzureCredential

        _log("auth=aad")
        credential = DefaultAzureCredential(
            exclude_interactive_browser_credential=True,
            exclude_visual_studio_code_credential=True,
            exclude_shared_token_cache_credential=True,
            exclude_powershell_credential=True,
            # After `az login`, AzureCliCredential is usually the most reliable local auth.
            exclude_azure_cli_credential=False,
        )

        token_timeout_s = float(os.environ.get("MER_AGENT_AUTH_TIMEOUT_SECONDS", "10"))

        async def _get_token_once() -> str:
            loop = asyncio.get_running_loop()

            def _blocking() -> str:
                return credential.get_token("https://cognitiveservices.azure.com/.default").token

            return await asyncio.wait_for(
                loop.run_in_executor(None, _blocking), timeout=token_timeout_s
            )

        _log("acquiring AAD token...")
        try:
            token = await _get_token_once()
        except asyncio.TimeoutError:
            raise RuntimeError(
                "Timed out acquiring Azure token. "
                "Try MER_AGENT_AUTH=api_key with AZURE_OPENAI_API_KEY, or run `az login`."
            )
        except Exception as e:
            raise RuntimeError(f"Failed to acquire Azure token: {e}")
        _log("AAD token acquired")

        client = AzureOpenAI(
            azure_endpoint=endpoint,
            api_version=api_version,
            azure_ad_token=token,
        )

    tools = _make_tools_schema()

    system = (
        "You are an accounting review agent for Enkel. "
        "You can fetch a QBO Balance Sheet and you can run a MER Balance Sheet review. "
        "You can also fetch MER Balance Sheet entries from Google Sheets. "
        "When the user asks to fetch or review, call the appropriate tool. "
        "If multiple tools are needed, try to call them all in one response. "
        "Never invent numbers: rely on tool output. "
        "If the user did not give a period end date, ask for it. "
        "Be concise."
    )

    messages: list[dict] = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]

    last_tool_results: list[dict] = []

    def _fallback_summary(reason: str) -> str:
        # Only supports our current two tools; keep it compact.
        mer = next((r for r in last_tool_results if r.get("tool") == "mer_balance_sheet_review"), None)
        qbo = next((r for r in last_tool_results if r.get("tool") == "qbo_balance_sheet"), None)
        mer_entries = next((r for r in last_tool_results if r.get("tool") == "mer_balance_sheet_entries"), None)

        lines: list[str] = []
        lines.append(f"({reason})")

        if mer and isinstance(mer.get("result"), dict):
            res = mer["result"]
            summary = res.get("summary") or {}
            lines.append(f"- Period end: {res.get('period_end_date')}")
            lines.append(
                "- Checks: "
                f"passed={summary.get('passed')} failed={summary.get('failed')} "
                f"unimplemented={summary.get('unimplemented')} skipped={summary.get('skipped')}"
            )
            failed = res.get("failed") or []
            if failed:
                preview = "; ".join(
                    [
                        f"{f.get('rule_id')}: {((f.get('evidence') or {}).get('message') or (f.get('evaluation_type') or 'failed'))}"
                        for f in failed[:5]
                    ]
                )
                lines.append(f"- Failed (preview): {preview}")
            req = res.get("requires_clarification") or []
            if req:
                lines.append(f"- Clarification needed: {', '.join(map(str, req[:5]))}")

        if qbo and isinstance(qbo.get("result"), dict):
            res = qbo["result"]
            lines.append(f"- QBO items: {res.get('count')} (preview sent: {res.get('preview_count')})")

        if mer_entries and isinstance(mer_entries.get("result"), dict):
            res = mer_entries["result"]
            mer_meta = res.get("mer") or {}
            lines.append(
                f"- MER entries: {res.get('count')} (month: {mer_meta.get('selected_month_header')})"
            )

        return "\n".join(lines)

    def _chat_with_retries() -> Any:
        # Keep retries short and explicit; Azure often tells you a wait time.
        max_attempts = int(os.environ.get("MER_AGENT_LLM_MAX_RETRIES", "4"))
        base_sleep = float(os.environ.get("MER_AGENT_LLM_RETRY_SLEEP_SECONDS", "75"))
        max_tokens = int(os.environ.get("MER_AGENT_LLM_MAX_TOKENS", "400"))
        http_timeout = float(os.environ.get("MER_AGENT_HTTP_TIMEOUT_SECONDS", "30"))

        def _retry_after_seconds(err: Exception) -> Optional[float]:
            # Azure OpenAI usually embeds: "Please retry after 60 seconds."
            msg = str(err)
            m = re.search(r"retry after\s+(\d+)\s+seconds", msg, flags=re.IGNORECASE)
            if m:
                try:
                    return float(m.group(1))
                except Exception:
                    return None
            return None

        for attempt in range(1, max_attempts + 1):
            try:
                _log("calling Azure OpenAI...")
                print(f"[llm] request (attempt {attempt}/{max_attempts})", file=sys.stderr)
                # The OpenAI SDK provides precise type hints for messages/tools; in this script
                # we build them dynamically as plain dicts.
                messages_any: Any = messages
                tools_any: Any = tools
                return client.chat.completions.create(
                    model=deployment,
                    messages=messages_any,
                    tools=tools_any,
                    tool_choice="auto",
                    temperature=0.2,
                    max_tokens=max_tokens,
                    timeout=http_timeout,
                )
            except RateLimitError as e:
                if attempt >= max_attempts:
                    raise
                ra = _retry_after_seconds(e)
                # Exponential backoff + jitter. If Azure provides retry-after, respect it.
                exp = base_sleep * (2 ** max(attempt - 1, 0))
                sleep_s = max((ra or 0) + 1, exp) + random.uniform(0, 3)
                print(
                    f"[llm] rate limited; retrying in {sleep_s:.0f}s (attempt {attempt}/{max_attempts})",
                    file=sys.stderr,
                )
                time.sleep(sleep_s)

    for _ in range(max_steps):
        try:
            resp = _chat_with_retries()
        except RateLimitError:
            # If we already have tool results, return deterministic summary.
            if last_tool_results:
                return _fallback_summary("LLM summarization skipped due to Azure OpenAI rate limiting; showing deterministic summary")
            # If the user asked for an explicit deterministic tool call, try that.
            if _explicit_tool_directive(prompt, "mer_balance_sheet_review"):
                end_date = _normalize_date_from_text(prompt)
                if end_date:
                    res = tool_mer_balance_sheet_review(end_date=end_date)
                    return _format_mer_review_bullets(res, bullets=_requested_bullet_count(prompt) or 5)
            raise

        msg = resp.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None)

        if tool_calls:
            messages.append({"role": "assistant", "content": msg.content or "", "tool_calls": [tc.model_dump() for tc in tool_calls]})
            for tc in tool_calls:
                name = tc.function.name
                args = json.loads(tc.function.arguments or "{}")

                # If end_date missing, try to recover from prompt (LLM sometimes forgets)
                if name in {"qbo_balance_sheet", "mer_balance_sheet_review"} and not args.get("end_date"):
                    recovered = _normalize_date_from_text(prompt)
                    if recovered:
                        args["end_date"] = recovered
                if name in {"mer_balance_sheet_entries"} and not args.get("end_date"):
                    recovered = _normalize_date_from_text(prompt)
                    if recovered:
                        args["end_date"] = recovered

                _log(f"running tool {name}...")
                result = _run_tool(name, args)
                last_tool_results.append({"tool": name, "args": args, "result": result})
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": name,
                        "content": json.dumps(result, separators=(",", ":"), ensure_ascii=False),
                    }
                )

            # Collapse architecture: let the LLM decide tool calls, but produce a deterministic final output
            # to avoid a second LLM call (which commonly triggers Azure TPM 429s).
            if os.environ.get("MER_AGENT_DETERMINISTIC_AFTER_TOOLS", "1") == "1":
                requested = _requested_bullet_count(prompt)
                bullets = requested or (5 if _wants_only_five_bullets(prompt) else 5)
                mer_res = next((r for r in last_tool_results if r.get("tool") == "mer_balance_sheet_review"), None)
                if mer_res and isinstance(mer_res.get("result"), dict):
                    return _format_mer_review_bullets(mer_res["result"], bullets=bullets)
                return _format_generic_tool_bullets(last_tool_results, bullets=bullets)

            continue

        # Final answer
        return msg.content or ""

    return "Agent did not finish within max_steps. Try a more specific prompt."


def main() -> int:
    ap = argparse.ArgumentParser(description="Local LLM-powered MER agent (terminal).")
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--max-steps", type=int, default=6)

    args = ap.parse_args()

    try:
        out = asyncio.run(run_agent(args.prompt, max_steps=args.max_steps))
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
