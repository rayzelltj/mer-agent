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
from datetime import datetime, timezone
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


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redact_config(cfg: dict[str, Any]) -> dict[str, Any]:
    # Avoid leaking secrets in logs.
    redacted = dict(cfg)
    if redacted.get("azure_openai_api_key"):
        redacted["azure_openai_api_key"] = "***REDACTED***"
    return redacted


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


def _wants_detailed_failures(prompt: str) -> bool:
    p = prompt.lower()
    needles = [
        "list each failed",
        "list each failure",
        "list failed",
        "failed rule",
        "failed rules",
        "failed check",
        "failed checks",
        "key evidence",
        "exact amounts",
    ]
    return any(n in p for n in needles)


def _format_mer_review_detailed(result: dict) -> str:
    summary = result.get("summary") or {}
    failed = result.get("failed") or []
    action_items = result.get("action_items") or []
    req = result.get("requires_clarification") or []

    lines: list[str] = []
    lines.append(f"Period end: {result.get('period_end_date')}")
    lines.append(
        "Checks: "
        f"passed={summary.get('passed')} failed={summary.get('failed')} "
        f"unimplemented={summary.get('unimplemented')} skipped={summary.get('skipped')}"
    )

    lines.append("")
    lines.append("Failed checks:")
    if not failed:
        lines.append("- None")
    else:
        for f in failed:
            if not isinstance(f, dict):
                continue
            rid = f.get("rule_id")
            et = f.get("evaluation_type")
            ev = f.get("evidence") or {}
            if not isinstance(ev, dict):
                ev = {"evidence": ev}

            # Key evidence fields (best-effort across eval types)
            tol = ev.get("tolerance")
            delta = ev.get("delta")
            msg = ev.get("message") or ev.get("reason")
            matched_row_label = ((ev.get("aging_report_evidence") or {}).get("matched_row_label") if isinstance(ev.get("aging_report_evidence"), dict) else None)

            lines.append(f"- {rid} ({et})")
            if tol is not None:
                lines.append(f"  tolerance={tol}")
            if delta is not None:
                lines.append(f"  delta={delta}")
            if matched_row_label is not None:
                lines.append(f"  matched_row_label={matched_row_label}")

            # Common amount fields
            for k in [
                "mer_amount",
                "qbo_amount",
                "balance_sheet_amount",
                "aging_report_total",
                "total_over_threshold",
            ]:
                if k in ev and ev.get(k) is not None:
                    lines.append(f"  {k}={ev.get(k)}")

            # For zero checks: show the first MER/QBO match amounts if present.
            mer_matches = ev.get("mer_matches")
            qbo_matches = ev.get("qbo_matches")
            if isinstance(mer_matches, list) and mer_matches:
                mm0 = mer_matches[0] if isinstance(mer_matches[0], dict) else None
                if mm0 and mm0.get("amount") is not None:
                    lines.append(f"  mer_match_amount={mm0.get('amount')} (raw={mm0.get('amount_raw')})")
            if isinstance(qbo_matches, list) and qbo_matches:
                qm0 = qbo_matches[0] if isinstance(qbo_matches[0], dict) else None
                if qm0 and qm0.get("amount") is not None:
                    lines.append(f"  qbo_match_amount={qm0.get('amount')} (raw={qm0.get('amount_raw')})")

            if msg:
                msg_s = str(msg)
                msg_s = msg_s.replace("\n", " ")
                lines.append(f"  note={_clip(msg_s, 220)}")

    lines.append("")
    lines.append("Action items:")
    if not isinstance(action_items, list) or not action_items:
        lines.append("- None")
    else:
        for x in action_items:
            if not isinstance(x, dict):
                continue
            rid = x.get("rule_id")
            acts = x.get("actions")
            lines.append(f"- {rid}: {acts}")

    lines.append("")
    lines.append("Clarifications needed:")
    if not isinstance(req, list) or not req:
        lines.append("- None")
    else:
        for x in req:
            if isinstance(x, dict):
                lines.append(f"- {x.get('id')}: {_clip(str(x.get('question') or ''), 240)}")
            else:
                lines.append(f"- {x}")

    return "\n".join(lines)


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
        _col_to_a1,
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
    from src.backend.v4.use_cases.mer_rule_engine import MERBalanceSheetEvaluationContext
    from src.backend.v4.use_cases.mer_rule_handlers import HANDLER_REGISTRY as BACKEND_HANDLERS

    _date.fromisoformat(end_date)

    rulebook = _load_rulebook_yaml(Path(rulebook_path) if rulebook_path else DEFAULT_RULEBOOK)

    policies = (rulebook.get("rulebook") or {}).get("policies") or {}
    tolerances = policies.get("tolerances") or {}
    zero_amount = ((tolerances.get("zero_balance") or {}).get("amount"))
    zero_tolerance = _decimal_from_amount_str(zero_amount)

    # amount_match tolerance is driven by the rulebook policy.
    amount_default = ((tolerances.get("amount_match") or {}).get("default_amount"))
    amount_match_tolerance = _decimal_from_amount_str(amount_default)

    # Fetch MER sheet
    reader = GoogleSheetsReader.from_env()

    def _qbo_item_label(item: Any) -> str:
        # QBO balance sheet items are usually ReportLineItem(label, amount)
        if isinstance(item, dict):
            return str(item.get("label") or item.get("name") or "")
        return str(getattr(item, "label", None) or getattr(item, "name", "") or "")

    def _qbo_item_amount_raw(item: Any) -> Any:
        if isinstance(item, dict):
            return item.get("amount") if "amount" in item else item.get("value")
        return getattr(item, "amount", None) if hasattr(item, "amount") else getattr(item, "value", None)

    def _safe_mkdir(path: str) -> None:
        try:
            os.makedirs(path, exist_ok=True)
        except Exception:
            return

    def _write_json_artifact(*, out_dir: str, filename: str, payload: dict) -> str | None:
        try:
            _safe_mkdir(out_dir)
            out_path = os.path.join(out_dir, filename)
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            return out_path
        except Exception:
            return None

    def _overall_line_status(check_statuses: list[str]) -> str:
        if any(s == "failed" for s in check_statuses):
            return "fail"
        if any(s in {"needs_human_review", "skipped", "unimplemented"} for s in check_statuses):
            return "flag"
        if check_statuses and all(s == "passed" for s in check_statuses):
            return "pass"
        return "unreviewed"

    def _summarize_for_cell(*, overall: str, checks: list[dict]) -> str:
        if overall == "pass":
            return "PASS"
        if overall == "unreviewed":
            return ""
        # Prefer the first non-passing check to summarize.
        focus = next((c for c in checks if c.get("status") != "passed"), checks[0] if checks else None)
        if not focus:
            return ""
        rid = focus.get("rule_id") or ""
        note = (focus.get("note") or "").strip()
        prefix = "FAIL" if overall == "fail" else "FLAG"
        msg = f"{prefix}: {rid}"
        if note:
            msg += f" — {note}"
        # Keep cell text readable.
        max_len = int(os.environ.get("MER_AGENT_SHEET_CELL_MAX_CHARS", "450"))
        return msg if max_len < 0 or len(msg) <= max_len else (msg[:max_len] + "…")

    line_results_by_row: dict[int, dict] = {}

    def _ensure_line_from_match(m: Any) -> dict:
        # m is SheetRowMatch
        row_index = int(m.row_index)
        if row_index not in line_results_by_row:
            line_results_by_row[row_index] = {
                "mer": {
                    "row_index": row_index,
                    "a1_cell": m.a1_cell,
                    "row_text": str(m.row_text or ""),
                    "value_raw": m.value,
                    "amount": str(parse_money(m.value)) if parse_money(m.value) is not None else None,
                },
                "checks": [],
            }
        return line_results_by_row[row_index]

    def _add_line_check(*, m: Any, rule_id: str, evaluation_type: str, status: str, note: str, evidence: dict) -> None:
        entry = _ensure_line_from_match(m)
        entry["checks"].append(
            {
                "rule_id": rule_id,
                "evaluation_type": evaluation_type,
                "status": status,
                "note": note,
                "evidence": evidence,
            }
        )

    sheet = mer_sheet
    if not sheet:
        titles = reader.list_sheet_titles()
        title_map = {str(t or "").strip().lower(): t for t in titles}
        if "balance sheet" in title_map:
            sheet = title_map["balance sheet"]
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

    # Create evaluation context for backend handlers
    eval_ctx = MERBalanceSheetEvaluationContext(
        end_date=end_date,
        mer_rows=rows,
        mer_selected_month_header=selected_month,
        mer_header_row_index=header_row_index,
        qbo_balance_sheet_items=qbo_items,
        qbo_client=qbo,
        zero_tolerance=zero_tolerance,
        amount_match_tolerance=amount_match_tolerance,
    )

    def _call_backend_handler(rule: dict, eval_type: str) -> dict | None:
        """Call backend handler if available, returning result dict or None."""
        handler = BACKEND_HANDLERS.get(eval_type)
        if not handler:
            return None
        try:
            result = handler(rule, eval_ctx)
            return result
        except Exception as e:
            return {"status": "failed", "details": {"error": str(e)}}

    # Backend-implemented + agent-only handlers
    backend_handler_types = set(BACKEND_HANDLERS.keys())
    agent_only_types = {
        "mer_credit_debit_accounts_book_balance_match_qbo",
        "qbo_report_total_matches_balance_sheet_line",
        "qbo_aging_items_older_than_threshold_require_explanation",
    }
    implemented_types = backend_handler_types | agent_only_types

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

        if rule.get("enabled") is False:
            results.append(
                {
                    "rule_id": rule_id,
                    "status": "skipped",
                    "evaluation_type": eval_type,
                    "evidence": _truncate_evidence({"reason": "disabled_by_rulebook"}),
                }
            )
            continue

        if eval_type not in implemented_types:
            results.append({"rule_id": rule_id, "status": "unimplemented", "evaluation_type": eval_type})
            continue

        # For handlers without line-level annotations, delegate directly to backend
        backend_only_types = {
            "requires_external_reconciliation_verification",
            "needs_human_judgment",
            "manual_process_required",
            "needs_prior_cycle_context",
            "mer_lines_require_link_to_support",
            "support_link_presence_check",
        }
        if eval_type in backend_only_types:
            handler_result = _call_backend_handler(rule, eval_type)
            if handler_result:
                results.append(
                    {
                        "rule_id": rule_id,
                        "status": handler_result.get("status", "unimplemented"),
                        "evaluation_type": eval_type,
                        "evidence": _truncate_evidence(handler_result.get("details") or handler_result),
                    }
                )
                continue
            # Fall through if handler not found

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

            qbo_match_items = [x for x in (qbo_items or []) if substring.lower() in _qbo_item_label(x).lower()]
            qbo_amount_raw = find_first_amount(qbo_items, name_substring=substring)
            qbo_amount = parse_money(qbo_amount_raw)

            for m in mer_matches:
                mer_amount = parse_money(m.value)
                mer_ok = mer_amount is not None and abs(mer_amount) <= zero_tolerance
                qbo_ok = qbo_amount is not None and abs(qbo_amount) <= zero_tolerance
                per_row_status = "passed" if (mer_ok and qbo_ok) else "failed"
                _add_line_check(
                    m=m,
                    rule_id=str(rule_id),
                    evaluation_type=eval_type,
                    status=per_row_status,
                    note=f"Expected zero; MER={mer_amount} QBO={qbo_amount}",
                    evidence={
                        "label_substring": substring,
                        "mer_a1_cell": m.a1_cell,
                        "mer_value_raw": m.value,
                        "mer_amount": str(mer_amount) if mer_amount is not None else None,
                        "qbo_amount_raw": qbo_amount_raw,
                        "qbo_amount": str(qbo_amount) if qbo_amount is not None else None,
                        "qbo_matches": [
                            {
                                "label": _qbo_item_label(x),
                                "amount": _qbo_item_amount_raw(x),
                            }
                            for x in qbo_match_items[:10]
                        ],
                        "zero_tolerance": str(zero_tolerance),
                    },
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
                    "evidence": _truncate_evidence(
                        {
                            **check.details,
                            "mer_matches": [
                                {
                                    "a1_cell": m.a1_cell,
                                    "row_index": m.row_index,
                                    "row_text": m.row_text,
                                    "value_raw": m.value,
                                }
                                for m in mer_matches
                            ],
                        }
                    ),
                }
            )
            continue

        if eval_type == "mer_credit_debit_accounts_book_balance_match_qbo":
            params = rule.get("parameters") or {}
            include_tokens = params.get("qbo_include_label_contains_any")
            if not isinstance(include_tokens, list) or not include_tokens:
                # Heuristic: accounts with external statements (banks, payment processors/clearing, cards/LOC).
                include_tokens = [
                    # Banks / cash equivalents
                    "bank",
                    "chequing",
                    "checking",
                    "savings",
                    "rbc",
                    # Payment processors / clearing
                    "paypal",
                    "etsy",
                    "clearing",
                    # Credit cards / LOC
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
                # Heuristic: subledger/schedule-driven items (no external statement reconciliation).
                exclude_tokens = [
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

            include_lowered = [str(k).strip().lower() for k in include_tokens if str(k).strip()]
            exclude_lowered = [str(k).strip().lower() for k in exclude_tokens if str(k).strip()]

            def _is_reconcilable_label(label: str) -> bool:
                ll = (label or "").strip().lower()
                if not ll:
                    return False
                # Special-case: investigated, not bank-reconciled.
                if "undeposited" in ll:
                    return False
                if any(bad in ll for bad in exclude_lowered):
                    return False
                return any(tok in ll for tok in include_lowered)

            candidates = [it for it in (qbo_items or []) if _is_reconcilable_label(_qbo_item_label(it))]

            def _candidate_mer_match_keys(qbo_label: str) -> list[str]:
                # Generate a few increasingly fuzzy keys to find the corresponding MER row.
                s = (qbo_label or "").strip()
                if not s:
                    return []
                keys: list[str] = [s]
                # Remove common prefixes
                for prefix in ["rbc - ", "rbc "]:
                    if s.lower().startswith(prefix):
                        keys.append(s[len(prefix) :].strip())
                # Split on separators (often account numbers like 3514/3522)
                for sep in ["/", "-", ":"]:
                    if sep in s:
                        parts = [p.strip() for p in s.split(sep) if p.strip()]
                        keys.extend(parts[:3])
                # Extract digit groups (last4 etc.) to match rows that only show numbers.
                digit_groups = re.findall(r"\d{3,6}", s)
                keys.extend(digit_groups[-3:])
                # Deduplicate while preserving order
                out: list[str] = []
                seen: set[str] = set()
                for k in keys:
                    kk = k.strip()
                    if not kk:
                        continue
                    norm = kk.lower()
                    if norm in seen:
                        continue
                    seen.add(norm)
                    out.append(kk)
                return out[:10]

            missing_mer: list[str] = []
            mismatches: list[dict] = []

            for it in candidates:
                qbo_label = _qbo_item_label(it)
                qbo_amount_raw = _qbo_item_amount_raw(it)
                qbo_amount = parse_money(str(qbo_amount_raw) if qbo_amount_raw is not None else None)

                mer_matches: list[Any] = []
                for key in _candidate_mer_match_keys(qbo_label):
                    mer_matches = find_values_for_rows_containing(
                        rows=rows,
                        row_substring=key,
                        col_header=selected_month,
                        header_row_index=header_row_index,
                    )
                    if mer_matches:
                        break
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
                        and abs(delta or Decimal("0")) <= amount_match_tolerance
                    )
                    if not passed:
                        mismatches.append(
                            {
                                "qbo_label": qbo_label,
                                "mer_a1_cell": m.a1_cell,
                                "mer_value_raw": m.value,
                                "qbo_amount_raw": qbo_amount_raw,
                                "delta": str(delta) if delta is not None else None,
                            }
                        )

                    _add_line_check(
                        m=m,
                        rule_id=str(rule_id),
                        evaluation_type=eval_type,
                        status="passed" if passed else "failed",
                        note=f"{qbo_label}: MER={mer_amount} QBO={qbo_amount} Δ={delta}",
                        evidence={
                            "qbo_label": qbo_label,
                            "qbo_amount_raw": qbo_amount_raw,
                            "qbo_amount": str(qbo_amount) if qbo_amount is not None else None,
                            "mer_a1_cell": m.a1_cell,
                            "mer_value_raw": m.value,
                            "mer_amount": str(mer_amount) if mer_amount is not None else None,
                            "tolerance": str(amount_match_tolerance),
                            "delta": str(delta) if delta is not None else None,
                        },
                    )

            status = "passed"
            if mismatches or missing_mer:
                status = "failed"
            if not candidates:
                status = "skipped"

            results.append(
                {
                    "rule_id": rule_id,
                    "status": status,
                    "evaluation_type": eval_type,
                    "evidence": _truncate_evidence(
                        {
                            "include_tokens": include_lowered,
                            "exclude_tokens": exclude_lowered,
                            "qbo_candidates_count": len(candidates),
                            "missing_mer_count": len(missing_mer),
                            "missing_mer_labels": missing_mer[:10],
                            "mismatches_count": len(mismatches),
                            "mismatches": mismatches[:10],
                            "tolerance": str(amount_match_tolerance),
                            "note": "MVP book-balance match only; does not prove statement reconciliation. Candidate selection is heuristic (external-statement-like accounts).",
                        }
                    ),
                }
            )
            continue

        if eval_type == "mer_line_amount_matches_qbo_line_amount":
            mer_rule = (rule.get("applies_to") or {}).get("mer_line") or {}
            # Support both singular and plural forms for flexibility
            qbo_rule = (rule.get("applies_to") or {}).get("qbo_balance_sheet_line") or {}
            qbo_rules_plural = (rule.get("applies_to") or {}).get("qbo_balance_sheet_lines") or {}

            mer_row_key = mer_rule.get("row_key")
            # Check singular first, then plural (label_contains_any)
            qbo_label = qbo_rule.get("label_contains")
            if not qbo_label:
                qbo_labels_any = qbo_rules_plural.get("label_contains_any") or []
                qbo_label = str(qbo_labels_any[0]) if qbo_labels_any else None

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
            
            # Determine status and reason for summary annotation
            if mer_amount is None and qbo_amount is None:
                # Item not found in either system - mark as skipped/N/A
                result_status = "skipped"
                skip_reason = f"Item not found in MER or QBO (searched for '{qbo_label or mer_row_key}')"
            elif mer_amount is None:
                result_status = "skipped"
                skip_reason = f"MER row not found (searched for '{mer_row_key}')"
            elif qbo_amount is None:
                result_status = "skipped"
                skip_reason = f"QBO line not found (searched for '{qbo_label}')"
            else:
                result_status = "passed" if check.passed else "failed"
                skip_reason = None
            
            if mer_matches:
                m = mer_matches[0]
                delta = (mer_amount - qbo_amount) if mer_amount is not None and qbo_amount is not None else None
                _add_line_check(
                    m=m,
                    rule_id=str(rule_id),
                    evaluation_type=eval_type,
                    status=result_status,
                    note=f"MER={mer_amount} QBO={qbo_amount} Δ={delta}",
                    evidence={
                        "mer_a1_cell": m.a1_cell,
                        "mer_row_key": mer_row_key,
                        "qbo_label_contains": qbo_label,
                        "mer_value_raw": m.value,
                        "mer_amount": str(mer_amount) if mer_amount is not None else None,
                        "qbo_amount_raw": qbo_amount_raw,
                        "qbo_amount": str(qbo_amount) if qbo_amount is not None else None,
                        "tolerance": str(amount_match_tolerance),
                        "delta": str(delta) if delta is not None else None,
                    },
                )
            # Override check_id to the rule_id for reporting consistency
            evidence_out = {**check.details, "check_id": str(rule_id)}
            if skip_reason:
                evidence_out["reason"] = skip_reason
            results.append(
                {
                    "rule_id": rule_id,
                    "status": result_status,
                    "evaluation_type": eval_type,
                    "evidence": _truncate_evidence(evidence_out),
                }
            )
            continue

        if eval_type == "mer_bank_balance_matches_qbo_bank_balance":
            # First try applies_to, then fall back to parameters
            mer_bank_row_key = ((rule.get("applies_to") or {}).get("mer_line") or {}).get("row_key")
            if not mer_bank_row_key:
                mer_bank_row_key = (rule.get("parameters") or {}).get("mer_bank_row_key")
            
            qbo_bank_substring = (
                ((rule.get("applies_to") or {}).get("qbo_balance_sheet_lines") or {})
                .get("label_contains_any")
                or []
            )
            qbo_bank_sub = str(qbo_bank_substring[0]) if qbo_bank_substring else ""
            if not qbo_bank_sub:
                qbo_bank_sub = str((rule.get("parameters") or {}).get("qbo_bank_label_substring") or "")

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
            
            # Determine status and reason for summary annotation
            if mer_amount is None and qbo_amount is None:
                result_status = "skipped"
                skip_reason = f"Item not found in MER or QBO (searched for '{mer_bank_row_key}' / '{qbo_bank_sub}')"
            elif mer_amount is None:
                result_status = "skipped"
                skip_reason = f"MER row not found (searched for '{mer_bank_row_key}')"
            elif qbo_amount is None:
                result_status = "skipped"
                skip_reason = f"QBO line not found (searched for '{qbo_bank_sub}')"
            else:
                result_status = "passed" if check.passed else "failed"
                skip_reason = None

            if mer_matches:
                m = mer_matches[0]
                delta = (mer_amount - qbo_amount) if mer_amount is not None and qbo_amount is not None else None
                _add_line_check(
                    m=m,
                    rule_id=str(rule_id),
                    evaluation_type=eval_type,
                    status=result_status,
                    note=f"MER={mer_amount} QBO={qbo_amount} Δ={delta}",
                    evidence={
                        "mer_a1_cell": m.a1_cell,
                        "mer_row_key": mer_bank_row_key,
                        "qbo_label_contains_any": qbo_bank_substring,
                        "mer_value_raw": m.value,
                        "mer_amount": str(mer_amount) if mer_amount is not None else None,
                        "qbo_amount_raw": qbo_amount_raw,
                        "qbo_amount": str(qbo_amount) if qbo_amount is not None else None,
                        "tolerance": str(amount_match_tolerance),
                        "delta": str(delta) if delta is not None else None,
                    },
                )

            evidence_out = {**check.details}
            if skip_reason:
                evidence_out["reason"] = skip_reason
            results.append(
                {
                    "rule_id": rule_id,
                    "status": result_status,
                    "evaluation_type": eval_type,
                    "evidence": _truncate_evidence(evidence_out),
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
                        mer_matches = find_values_for_rows_containing(
                            rows=rows,
                            row_substring="accounts payable",
                            col_header=selected_month,
                            header_row_index=header_row_index,
                        )
                        for m in mer_matches:
                            _add_line_check(
                                m=m,
                                rule_id=str(rule_id),
                                evaluation_type=eval_type,
                                status="skipped",
                                note="Blocked by QBO report permission (AgedPayables*)",
                                evidence={"error": str(e)},
                            )
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
                        mer_matches = find_values_for_rows_containing(
                            rows=rows,
                            row_substring="accounts receivable",
                            col_header=selected_month,
                            header_row_index=header_row_index,
                        )
                        for m in mer_matches:
                            _add_line_check(
                                m=m,
                                rule_id=str(rule_id),
                                evaluation_type=eval_type,
                                status="skipped",
                                note="Blocked by QBO report permission (AgedReceivables*)",
                                evidence={"error": str(e)},
                            )
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

            mer_matches = []
            if bs_label_substring:
                mer_matches = find_values_for_rows_containing(
                    rows=rows,
                    row_substring=str(bs_label_substring),
                    col_header=selected_month,
                    header_row_index=header_row_index,
                )

            if total_amount is None or bs_amount is None:
                for m in mer_matches:
                    _add_line_check(
                        m=m,
                        rule_id=str(rule_id),
                        evaluation_type=eval_type,
                        status="failed",
                        note="Could not parse QBO totals",
                        evidence={
                            "balance_sheet_amount_raw": bs_raw,
                            "aging_report_total_raw": total_raw,
                        },
                    )
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

            for m in mer_matches:
                _add_line_check(
                    m=m,
                    rule_id=str(rule_id),
                    evaluation_type=eval_type,
                    status="passed" if passed else "failed",
                    note=f"Aging total={total_amount} BS={bs_amount} Δ={delta}",
                    evidence={
                        "balance_sheet_label_substring": bs_label_substring,
                        "balance_sheet_amount_raw": bs_raw,
                        "balance_sheet_amount": str(bs_amount),
                        "aging_report_total_raw": total_raw,
                        "aging_report_total": str(total_amount),
                        "tolerance": str(amount_match_tolerance),
                        "delta": str(delta),
                    },
                )
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

            # Attach this rule to the AP/AR lines (if present) so it shows up per-line.
            for label_sub in ["accounts payable", "accounts receivable"]:
                mer_matches = find_values_for_rows_containing(
                    rows=rows,
                    row_substring=label_sub,
                    col_header=selected_month,
                    header_row_index=header_row_index,
                )
                for m in mer_matches:
                    _add_line_check(
                        m=m,
                        rule_id=str(rule_id),
                        evaluation_type=eval_type,
                        status="failed" if has_findings else "passed",
                        note=f"{len(ap_items)} AP + {len(ar_items)} AR items > {max_age_days_int}d",
                        evidence={
                            "period_end_date": end_date,
                            "max_age_days": max_age_days_int,
                            "ap_count": len(ap_items),
                            "ar_count": len(ar_items),
                            "ap_total_over_threshold": ap.get("total_over_threshold"),
                            "ar_total_over_threshold": ar.get("total_over_threshold"),
                        },
                    )

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

    # Build per-line results (for JSON artifacts + optional sheet annotations).
    line_results: list[dict] = []
    for row_index, entry in sorted(line_results_by_row.items(), key=lambda kv: kv[0]):
        check_statuses = [str(c.get("status")) for c in (entry.get("checks") or [])]
        overall = _overall_line_status(check_statuses)
        entry["overall_status"] = overall
        entry["sheet_annotation"] = _summarize_for_cell(overall=overall, checks=entry.get("checks") or [])
        line_results.append(entry)

    artifacts_dir = os.environ.get("MER_AGENT_ARTIFACTS_DIR", ".mer_agent_runs")
    write_line_json = os.environ.get("MER_AGENT_WRITE_LINE_RESULTS_JSON", "").strip() == "1"
    line_json_path: str | None = None
    if write_line_json:
        from datetime import datetime, timezone

        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        line_json_path = _write_json_artifact(
            out_dir=artifacts_dir,
            filename=f"{ts}_mer_line_results.json",
            payload={
                "period_end_date": end_date,
                "mer": {"sheet": sheet, "range": mer_range, "selected_month_header": selected_month},
                "line_results": line_results,
            },
        )

    # Optional: write annotations to the configured column (default: H).
    wrote_sheet_annotations: dict | None = None
    if os.environ.get("MER_AGENT_WRITE_SHEET_ANNOTATIONS", "").strip() == "1":
        if header_row_index is None:
            raise ValueError("Cannot write sheet annotations without a detected header_row_index")

        def _a1_col_to_index(col_letters: str) -> int:
            col_letters = (col_letters or "").strip().upper()
            if not col_letters or not re.fullmatch(r"[A-Z]{1,3}", col_letters):
                raise ValueError(f"Invalid A1 column letters: {col_letters!r}")
            n = 0
            for ch in col_letters:
                n = n * 26 + (ord(ch) - ord("A") + 1)
            return n - 1

        header_row_num = header_row_index + 1
        annotation_col_letter = os.environ.get("MER_AGENT_SHEET_ANNOTATION_COLUMN", "H").strip().upper() or "H"
        annotation_col_index = _a1_col_to_index(annotation_col_letter)
        header_value = os.environ.get(
            "MER_AGENT_SHEET_ANNOTATION_HEADER",
            f"MER Review (auto) {end_date}",
        )

        updates: dict[str, str] = {f"'{sheet}'!{annotation_col_letter}{header_row_num}": header_value}
        
        # Track which rows have line-level annotations
        rows_with_annotations: set[int] = set()
        for lr in line_results:
            text = (lr.get("sheet_annotation") or "").strip()
            if not text:
                continue
            mer_row_index = int(((lr.get("mer") or {}).get("row_index")) or -1)
            if mer_row_index < 0:
                continue
            row_num = mer_row_index + 1
            updates[f"'{sheet}'!{annotation_col_letter}{row_num}"] = text
            rows_with_annotations.add(mer_row_index)

        # Find the last row of the balance sheet data
        last_data_row_index = len(rows) - 1
        # Walk backward to find the last non-empty row
        while last_data_row_index > header_row_index:
            row_data = rows[last_data_row_index] if last_data_row_index < len(rows) else []
            if any((c or "").strip() for c in row_data):
                break
            last_data_row_index -= 1

        # Collect rules that don't correspond to any specific line (summary-level checks)
        # These are rules whose results don't have row mappings in line_results_by_row
        rules_with_line_mappings: set[str] = set()
        for lr in line_results:
            for check in (lr.get("checks") or []):
                rules_with_line_mappings.add(str(check.get("rule_id") or ""))

        summary_annotations: list[str] = []
        for r in results:
            rule_id = str(r.get("rule_id") or "")
            status = str(r.get("status") or "")
            eval_type = str(r.get("evaluation_type") or "")
            
            # Skip unimplemented or rules that already have line-level annotations
            if status == "unimplemented" or rule_id in rules_with_line_mappings:
                continue
            
            # Build summary annotation for this rule
            if status == "passed":
                summary_annotations.append(f"PASS: {rule_id}")
            elif status == "failed":
                evidence = r.get("evidence") or {}
                note = ""
                if isinstance(evidence, dict):
                    note = str(evidence.get("reason") or evidence.get("note") or evidence.get("rule") or "").strip()
                if note:
                    summary_annotations.append(f"FAIL: {rule_id} — {note[:200]}")
                else:
                    summary_annotations.append(f"FAIL: {rule_id}")
            elif status == "skipped":
                evidence = r.get("evidence") or {}
                reason = ""
                if isinstance(evidence, dict):
                    reason = str(evidence.get("reason") or "").strip()
                if reason:
                    summary_annotations.append(f"SKIP: {rule_id} — {reason[:150]}")
                else:
                    summary_annotations.append(f"SKIP: {rule_id}")
            elif status == "needs_human_review":
                evidence = r.get("evidence") or {}
                action = ""
                if isinstance(evidence, dict):
                    action = str(evidence.get("action") or evidence.get("reason") or "").strip()
                if action:
                    summary_annotations.append(f"REVIEW: {rule_id} — {action[:150]}")
                else:
                    summary_annotations.append(f"REVIEW: {rule_id}")

        # Write summary annotations starting from the row after the last data row
        if summary_annotations:
            summary_start_row = last_data_row_index + 3  # Leave a blank row for spacing
            # Add a summary header
            updates[f"'{sheet}'!{annotation_col_letter}{summary_start_row}"] = f"--- MER Summary ({end_date}) ---"
            for i, annotation in enumerate(summary_annotations):
                row_num = summary_start_row + 1 + i
                updates[f"'{sheet}'!{annotation_col_letter}{row_num}"] = annotation

        # Guarded inside the integration by GOOGLE_SHEETS_ALLOW_WRITE=1.
        wrote_sheet_annotations = {
            "annotation_col_letter": annotation_col_letter,
            "annotation_col_index": annotation_col_index,
            "updated_cells": len(updates),
            "line_annotations": len(rows_with_annotations),
            "summary_annotations": len(summary_annotations),
            "response": reader.batch_update_values(updates=updates),
        }

    # Compact the payload: keep summary + failed evidence, omit huge unimplemented list.
    passed = sum(1 for r in results if r.get("status") == "passed")
    failed = sum(1 for r in results if r.get("status") == "failed")
    unimplemented = sum(1 for r in results if r.get("status") == "unimplemented")
    skipped = sum(1 for r in results if r.get("status") == "skipped")
    needs_review = sum(1 for r in results if r.get("status") == "needs_human_review")
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
            "needs_human_review": needs_review,
            "unimplemented": unimplemented,
            "skipped": skipped,
            "total_considered": len(results),
        },
        "failed": failed_results,
        "skipped": skipped_results,
        "implemented": implemented_results,
        "line_results": line_results,
        "artifacts": {
            "line_results_json": line_json_path,
            "sheet_write": wrote_sheet_annotations,
        },
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
        title_map = {str(t or "").strip().lower(): t for t in titles}
        if "balance sheet" in title_map:
            sheet = title_map["balance sheet"]
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
                "description": "Run deterministic MER Balance Sheet review checks (reads Google Sheet + QBO; optionally writes artifacts/annotations when explicitly enabled via env vars).",
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

    emit_run_log = os.environ.get("MER_AGENT_EMIT_RUN_LOG", "0") == "1"
    run_t0 = time.perf_counter()
    run_log: dict[str, Any] = {
        "config": {},
        "run started": _utc_now_iso(),
        "run ended": None,
        "time taken seconds": None,
        "attempts": [],
        "final rulebook checks": None,
        "final answer": None,
    }

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
        t0 = time.perf_counter()
        end_date = _normalize_date_from_text(prompt)
        if not end_date:
            return "- Missing end_date (YYYY-MM-DD)"
        result = tool_mer_balance_sheet_review(end_date=end_date)
        want_full_json = os.environ.get("MER_AGENT_PRINT_TOOL_JSON", "0") == "1"
        want_full_json = want_full_json or ("full tool result json" in prompt.lower())
        want_full_json = want_full_json or ("print the full tool result" in prompt.lower())

        bullets = _format_mer_review_bullets(result, bullets=bullet_count)
        duration_s = round(time.perf_counter() - t0, 3)

        if emit_run_log:
            run_log["config"] = _redact_config(
                {
                    "mode": "tool_only",
                    "prompt": prompt,
                    "max_steps": max_steps,
                    "tool_only": True,
                    "deterministic_after_tools": True,
                    "print_tool_json": bool(want_full_json),
                }
            )
            run_log["attempts"].append(
                {
                    "attempt number": 1,
                    "start_time": run_log["run started"],
                    "end_time": _utc_now_iso(),
                    "time taken seconds": duration_s,
                    "model used": None,
                    "input tokens": None,
                    "output tokens": None,
                    "cached tokens": None,
                    "warnings": [],
                    "tool_calls": [
                        {
                            "tool": "mer_balance_sheet_review",
                            "args": {"end_date": end_date},
                            "duration seconds": duration_s,
                            "error": None,
                            "result": result,
                        }
                    ],
                    "rulebook checks": {
                        "implemented": result.get("implemented"),
                        "failed": result.get("failed"),
                        "skipped": result.get("skipped"),
                        "summary": result.get("summary"),
                    },
                }
            )
            run_log["final rulebook checks"] = run_log["attempts"][0]["rulebook checks"]
            run_log["final answer"] = bullets
            run_log["run ended"] = _utc_now_iso()
            run_log["time taken seconds"] = round(time.perf_counter() - run_t0, 3)
            return json.dumps(run_log, indent=2, ensure_ascii=False)

        if want_full_json:
            payload = json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True)
            return payload + "\n\n" + bullets

        return bullets

    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
    deployment = os.environ.get("MER_AGENT_LLM_DEPLOYMENT") or os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME")
    api_version = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-11-20")
    auth_mode = os.environ.get("MER_AGENT_AUTH", "aad").lower().strip()
    api_key = os.environ.get("AZURE_OPENAI_API_KEY")

    run_log["config"] = _redact_config(
        {
            "mode": "llm_agent",
            "prompt": prompt,
            "max_steps": max_steps,
            "azure_openai_endpoint": endpoint,
            "azure_openai_deployment": deployment,
            "azure_openai_api_version": api_version,
            "mer_agent_auth": auth_mode,
            "azure_openai_api_key": api_key,
            "deterministic_after_tools": os.environ.get("MER_AGENT_DETERMINISTIC_AFTER_TOOLS", "1") == "1",
            "tool_only": tool_only,
            "limits": {
                "MER_AGENT_QBO_PREVIEW_LIMIT": os.environ.get("MER_AGENT_QBO_PREVIEW_LIMIT"),
                "MER_AGENT_EVIDENCE_LIST_LIMIT": os.environ.get("MER_AGENT_EVIDENCE_LIST_LIMIT"),
                "MER_AGENT_EVIDENCE_STRING_LIMIT": os.environ.get("MER_AGENT_EVIDENCE_STRING_LIMIT"),
                "MER_AGENT_FAILED_RULES_LIMIT": os.environ.get("MER_AGENT_FAILED_RULES_LIMIT"),
                "MER_AGENT_IMPLEMENTED_RULES_LIMIT": os.environ.get("MER_AGENT_IMPLEMENTED_RULES_LIMIT"),
                "MER_AGENT_ACTION_ITEMS_LIMIT": os.environ.get("MER_AGENT_ACTION_ITEMS_LIMIT"),
            },
        }
    )

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

    attempt_seq = 0
    step_seq = 0

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

    def _extract_usage(resp_obj: Any) -> dict[str, Any]:
        usage = getattr(resp_obj, "usage", None)
        if not usage:
            return {"input_tokens": None, "output_tokens": None, "cached_tokens": None}

        input_tokens = getattr(usage, "prompt_tokens", None)
        output_tokens = getattr(usage, "completion_tokens", None)

        cached_tokens = None
        details = getattr(usage, "prompt_tokens_details", None)
        if details is not None:
            cached_tokens = getattr(details, "cached_tokens", None)
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": cached_tokens,
        }

    def _chat_with_retries(*, step_index: int) -> Any:
        # Keep retries short and explicit; Azure often tells you a wait time.
        max_attempts = int(os.environ.get("MER_AGENT_LLM_MAX_RETRIES", "4"))
        base_sleep = float(os.environ.get("MER_AGENT_LLM_RETRY_SLEEP_SECONDS", "75"))
        max_tokens = int(os.environ.get("MER_AGENT_LLM_MAX_TOKENS", "400"))
        http_timeout = float(os.environ.get("MER_AGENT_HTTP_TIMEOUT_SECONDS", "30"))

        respect_retry_after = os.environ.get("MER_AGENT_RESPECT_RETRY_AFTER", "1") == "1"
        retry_after_cap = os.environ.get("MER_AGENT_RETRY_AFTER_CAP_SECONDS")
        retry_after_cap_s: float | None = None
        if retry_after_cap is not None and str(retry_after_cap).strip() != "":
            try:
                retry_after_cap_s = float(retry_after_cap)
            except Exception:
                retry_after_cap_s = None

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

        nonlocal attempt_seq
        for retry_index in range(1, max_attempts + 1):
            attempt_seq += 1
            attempt_log: dict[str, Any] = {
                "attempt number": attempt_seq,
                "step_index": step_index,
                "retry_index": retry_index,
                "start_time": _utc_now_iso(),
                "end_time": None,
                "time taken seconds": None,
                "model used": deployment,
                "input tokens": None,
                "output tokens": None,
                "cached tokens": None,
                "warnings": [],
                "tool_calls": [],
                "rulebook checks": None,
            }
            t0 = time.perf_counter()
            try:
                _log("calling Azure OpenAI...")
                print(f"[llm] request (attempt {retry_index}/{max_attempts})", file=sys.stderr)
                # The OpenAI SDK provides precise type hints for messages/tools; in this script
                # we build them dynamically as plain dicts.
                messages_any: Any = messages
                tools_any: Any = tools
                resp_obj = client.chat.completions.create(
                    model=deployment,
                    messages=messages_any,
                    tools=tools_any,
                    tool_choice="auto",
                    temperature=0.2,
                    max_tokens=max_tokens,
                    timeout=http_timeout,
                )
                usage_out = _extract_usage(resp_obj)
                attempt_log.update(
                    {
                        "input tokens": usage_out.get("input_tokens"),
                        "output tokens": usage_out.get("output_tokens"),
                        "cached tokens": usage_out.get("cached_tokens"),
                    }
                )
                return resp_obj
            except RateLimitError as e:
                attempt_log["warnings"].append(
                    {
                        "type": "rate_limit",
                        "message": str(e),
                    }
                )
                if retry_index >= max_attempts:
                    raise
                ra = _retry_after_seconds(e)
                if not respect_retry_after:
                    ra = None
                elif retry_after_cap_s is not None and ra is not None:
                    ra = min(ra, retry_after_cap_s)
                # Exponential backoff + jitter. If Azure provides retry-after, respect it.
                exp = base_sleep * (2 ** max(retry_index - 1, 0))
                sleep_s = max((ra or 0) + 1, exp) + random.uniform(0, 3)
                print(
                    f"[llm] rate limited; retrying in {sleep_s:.0f}s (attempt {retry_index}/{max_attempts})",
                    file=sys.stderr,
                )
                attempt_log["warnings"].append(
                    {
                        "type": "retry_sleep",
                        "sleep_seconds": sleep_s,
                        "retry_after_seconds": ra,
                    }
                )
                time.sleep(sleep_s)
            finally:
                attempt_log["end_time"] = _utc_now_iso()
                attempt_log["time taken seconds"] = round(time.perf_counter() - t0, 3)
                run_log["attempts"].append(attempt_log)

    for _ in range(max_steps):
        step_seq += 1
        try:
            resp = _chat_with_retries(step_index=step_seq)
        except RateLimitError:
            # If we already have tool results, return deterministic summary.
            if last_tool_results:
                final_text = _fallback_summary(
                    "LLM summarization skipped due to Azure OpenAI rate limiting; showing deterministic summary"
                )
                run_log["final answer"] = final_text

                mer_res = next((r for r in last_tool_results if r.get("tool") == "mer_balance_sheet_review"), None)
                if mer_res and isinstance(mer_res.get("result"), dict):
                    run_log["final rulebook checks"] = {
                        "implemented": mer_res["result"].get("implemented"),
                        "failed": mer_res["result"].get("failed"),
                        "skipped": mer_res["result"].get("skipped"),
                        "summary": mer_res["result"].get("summary"),
                    }

                run_log["run ended"] = _utc_now_iso()
                run_log["time taken seconds"] = round(time.perf_counter() - run_t0, 3)
                if emit_run_log:
                    return json.dumps(run_log, indent=2, ensure_ascii=False)
                return final_text
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
                tool_t0 = time.perf_counter()
                try:
                    result = _run_tool(name, args)
                    tool_err = None
                except Exception as tool_exc:
                    result = None
                    tool_err = str(tool_exc)
                tool_dt = round(time.perf_counter() - tool_t0, 3)

                last_tool_results.append({"tool": name, "args": args, "result": result, "error": tool_err, "duration_seconds": tool_dt})

                # Attach tool call info to the latest attempt record.
                if run_log["attempts"]:
                    run_log["attempts"][-1].setdefault("tool_calls", []).append(
                        {
                            "tool": name,
                            "args": args,
                            "duration seconds": tool_dt,
                            "error": tool_err,
                            "result": result,
                        }
                    )

                    # If this tool produced rulebook check output, attach it to the current attempt.
                    if name == "mer_balance_sheet_review" and isinstance(result, dict):
                        run_log["attempts"][-1]["rulebook checks"] = {
                            "implemented": result.get("implemented"),
                            "failed": result.get("failed"),
                            "skipped": result.get("skipped"),
                            "summary": result.get("summary"),
                        }

                if tool_err is not None:
                    # Bubble up tool failure to the LLM loop to avoid silently continuing.
                    raise RuntimeError(f"Tool '{name}' failed: {tool_err}")
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
                    if _wants_detailed_failures(prompt):
                        final_text = _format_mer_review_detailed(mer_res["result"])
                    else:
                        final_text = _format_mer_review_bullets(mer_res["result"], bullets=bullets)
                    run_log["final rulebook checks"] = {
                        "implemented": mer_res["result"].get("implemented"),
                        "failed": mer_res["result"].get("failed"),
                        "skipped": mer_res["result"].get("skipped"),
                        "summary": mer_res["result"].get("summary"),
                    }
                else:
                    final_text = _format_generic_tool_bullets(last_tool_results, bullets=bullets)

                run_log["final answer"] = final_text
                run_log["run ended"] = _utc_now_iso()
                run_log["time taken seconds"] = round(time.perf_counter() - run_t0, 3)
                if emit_run_log:
                    return json.dumps(run_log, indent=2, ensure_ascii=False)
                return final_text

            continue

        # Final answer
        final = msg.content or ""
        run_log["final answer"] = final
        run_log["run ended"] = _utc_now_iso()
        run_log["time taken seconds"] = round(time.perf_counter() - run_t0, 3)
        if emit_run_log:
            # Best-effort: if a deterministic review was run, capture rulebook checks for debugging.
            mer_res = next((r for r in last_tool_results if r.get("tool") == "mer_balance_sheet_review"), None)
            if mer_res and isinstance(mer_res.get("result"), dict):
                run_log["final rulebook checks"] = {
                    "implemented": mer_res["result"].get("implemented"),
                    "failed": mer_res["result"].get("failed"),
                    "skipped": mer_res["result"].get("skipped"),
                    "summary": mer_res["result"].get("summary"),
                }
            return json.dumps(run_log, indent=2, ensure_ascii=False)
        return final

    final = "Agent did not finish within max_steps. Try a more specific prompt."
    run_log["final answer"] = final
    run_log["run ended"] = _utc_now_iso()
    run_log["time taken seconds"] = round(time.perf_counter() - run_t0, 3)
    if emit_run_log:
        return json.dumps(run_log, indent=2, ensure_ascii=False)
    return final


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
