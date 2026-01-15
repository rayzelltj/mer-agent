"""Probe QBO Reports API for aging report name accessibility.

Goal: determine which aging report names are accessible in the current tenant,
including "Summary"/"Detail" variants that may fail with 5020 (ReportName).

Safe output policy:
- Never print access/refresh tokens.
- Print report name attempted, params used, and a small non-sensitive header summary.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import sys
from typing import Any

from dotenv import load_dotenv


# Ensure `import src.*` works when running as `python scripts/...` from repo root.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.backend.v4.integrations.qbo_client import QBOClient


@dataclass(frozen=True)
class ProbeAttempt:
    report_name: str
    params: dict[str, str]


def _header_brief(report: dict[str, Any]) -> dict[str, Any]:
    h = report.get("Header")
    if not isinstance(h, dict):
        return {}
    return {
        "ReportName": h.get("ReportName"),
        "StartPeriod": h.get("StartPeriod"),
        "EndPeriod": h.get("EndPeriod"),
        "Time": h.get("Time"),
    }


def _column_titles(report: dict[str, Any]) -> list[str]:
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


def _looks_like_5020_reportname_permission_denied(err: Exception) -> bool:
    msg = str(err)
    return (
        "Permission Denied" in msg
        and "ReportName" in msg
        and "5020" in msg
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--end-date", required=True, help="YYYY-MM-DD")
    args = parser.parse_args()

    # Avoid python-dotenv find_dotenv() issues in inline execution contexts.
    load_dotenv(dotenv_path=".env", override=False)

    qbo = QBOClient.from_env()

    end_date = args.end_date

    # We try both param styles because Intuit docs vary:
    # - Many reports accept end_date
    # - Some doc pages show report_date
    candidates: list[ProbeAttempt] = [
        # Canonical aging reports (often work even when Summary/Detail is denied)
        ProbeAttempt("AgedPayables", {"report_date": end_date}),
        ProbeAttempt("AgedReceivables", {"report_date": end_date}),

        # Aged* variants
        ProbeAttempt("AgedPayablesSummary", {"report_date": end_date}),
        ProbeAttempt("AgedPayablesDetail", {"report_date": end_date}),
        ProbeAttempt("AgedReceivablesSummary", {"report_date": end_date}),
        ProbeAttempt("AgedReceivablesDetail", {"report_date": end_date}),

        # AP/AR Aging variants from docs
        ProbeAttempt("APAgingSummary", {"report_date": end_date}),
        ProbeAttempt("APAgingDetail", {"report_date": end_date}),
        ProbeAttempt("ARAgingSummary", {"report_date": end_date}),
        ProbeAttempt("ARAgingDetail", {"report_date": end_date}),

        # Same names but with end_date param, just in case this tenant expects it
        ProbeAttempt("APAgingSummary", {"end_date": end_date}),
        ProbeAttempt("APAgingDetail", {"end_date": end_date}),
        ProbeAttempt("ARAgingSummary", {"end_date": end_date}),
        ProbeAttempt("ARAgingDetail", {"end_date": end_date}),
    ]

    ok: list[tuple[str, dict[str, str]]] = []
    denied_5020: list[tuple[str, dict[str, str], str]] = []
    other_err: list[tuple[str, dict[str, str], str]] = []

    print(f"Probing aging report names for end_date={end_date}")
    print("Token source: .env_qbo_tokens.json (values not printed)")

    for attempt in candidates:
        print("\n---")
        print("Report:", attempt.report_name)
        print("Params:", attempt.params)

        try:
            report = qbo._get_report(report_name=attempt.report_name, params=attempt.params)
        except Exception as e:
            if _looks_like_5020_reportname_permission_denied(e):
                denied_5020.append((attempt.report_name, attempt.params, str(e)))
                print("RESULT: DENIED (5020 ReportName Permission Denied)")
            else:
                other_err.append((attempt.report_name, attempt.params, str(e)))
                print("RESULT: ERROR")
            print("Error:", str(e))
            continue

        ok.append((attempt.report_name, attempt.params))
        print("RESULT: OK")
        print("Header:", _header_brief(report))
        print("Columns:", _column_titles(report))

    print("\n==================== SUMMARY ====================")
    print("OK:")
    for name, params in ok:
        print(" -", name, params)

    print("\nDENIED 5020:")
    for name, params, _ in denied_5020:
        print(" -", name, params)

    print("\nOTHER ERRORS:")
    for name, params, _ in other_err:
        print(" -", name, params)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
