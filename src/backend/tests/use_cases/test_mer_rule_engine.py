from __future__ import annotations

from decimal import Decimal

from src.backend.v4.integrations.qbo_reports import ReportLineItem
from src.backend.v4.use_cases.mer_rule_engine import (
    MERBalanceSheetEvaluationContext,
    MERBalanceSheetRuleEngine,
    collect_action_items,
)


class _StubQBO:
    def __init__(
        self,
        *,
        aged_payables_total: dict | None = None,
        aged_payables_detail: dict | None = None,
        aged_receivables_detail: dict | None = None,
        accounts: list[dict] | None = None,
    ):
        self._aged_payables_total = aged_payables_total or {}
        self._aged_payables_detail = aged_payables_detail or {}
        self._aged_receivables_detail = aged_receivables_detail or {}
        self._accounts = accounts or []

    def get_aged_payables_total(self, *, end_date: str):
        return self._aged_payables_total

    def get_aged_payables_detail(self, *, end_date: str):
        return self._aged_payables_detail

    def get_aged_receivables_detail(self, *, end_date: str):
        return self._aged_receivables_detail

    def get_accounts(self, *, max_results: int = 1000):
        return self._accounts


def test_engine_inventory_accounts_must_exist_in_qbo_and_mer() -> None:
    engine = MERBalanceSheetRuleEngine()

    rulebook = {
        "rules": [
            {
                "rule_id": "BS-BANK-AND-CC-INVENTORY-COVERAGE",
                "title": "All bank/credit card accounts from maintenance sheet are included",
                "requires_external_sources": ["client_maintenance_kyc", "mer_google_sheet"],
                "evaluation": {"type": "inventory_accounts_must_exist_in_qbo_and_mer"},
            }
        ]
    }

    kyc_rows = [
        ["Account Name", "Type"],
        ["RBC Chequing", "Bank"],
        ["Amex", "Credit Card"],
    ]

    mer_rows = [
        ["Account", "Nov. 2025", "Comments"],
        ["RBC Chequing", "123.00", ""],
        ["Amex", "50.00", ""],
    ]

    ctx = MERBalanceSheetEvaluationContext(
        end_date="2025-11-30",
        mer_rows=mer_rows,
        mer_selected_month_header="Nov. 2025",
        mer_header_row_index=0,
        client_maintenance_rows=kyc_rows,
        qbo_balance_sheet_items=[],
        qbo_client=_StubQBO(
            accounts=[
                {"Name": "RBC Chequing"},
                {"Name": "Amex"},
            ]
        ),
        zero_tolerance=Decimal("0.00"),
        amount_match_tolerance=Decimal("0.00"),
    )

    res = engine.evaluate(rulebook=rulebook, ctx=ctx)
    assert res[0]["status"] == "passed"

    # Missing from MER should fail.
    mer_rows_missing = [
        ["Account", "Nov. 2025", "Comments"],
        ["RBC Chequing", "123.00", ""],
    ]
    ctx2 = MERBalanceSheetEvaluationContext(
        end_date="2025-11-30",
        mer_rows=mer_rows_missing,
        mer_selected_month_header="Nov. 2025",
        mer_header_row_index=0,
        client_maintenance_rows=kyc_rows,
        qbo_balance_sheet_items=[],
        qbo_client=_StubQBO(accounts=[{"Name": "RBC Chequing"}, {"Name": "Amex"}]),
        zero_tolerance=Decimal("0.00"),
        amount_match_tolerance=Decimal("0.00"),
    )
    res2 = engine.evaluate(rulebook=rulebook, ctx=ctx2)
    assert res2[0]["status"] == "failed"


def test_engine_inventory_accounts_prefers_qbo_xero_name_column() -> None:
    engine = MERBalanceSheetRuleEngine()

    rulebook = {
        "rules": [
            {
                "rule_id": "BS-BANK-AND-CC-INVENTORY-COVERAGE",
                "title": "All bank/credit card accounts from maintenance sheet are included",
                "requires_external_sources": ["client_maintenance_kyc", "mer_google_sheet"],
                "evaluation": {"type": "inventory_accounts_must_exist_in_qbo_and_mer"},
            }
        ]
    }

    # This mirrors the real template shape where statement names and QBO/Xero names are separate.
    kyc_rows = [
        [
            "Company",
            "Institution",
            "Type",
            "Account name on statement",
            "Account name in [QBO/Xero]",
        ],
        ["ClientCo", "RBC", "Chequing", "RBC CHQ (statement)", "RBC Chequing 6338"],
    ]

    mer_rows = [
        ["Account", "Nov. 2025", "Comments"],
        ["RBC Chequing 6338", "123.00", ""],
    ]

    ctx = MERBalanceSheetEvaluationContext(
        end_date="2025-11-30",
        mer_rows=mer_rows,
        mer_selected_month_header="Nov. 2025",
        mer_header_row_index=0,
        client_maintenance_rows=kyc_rows,
        qbo_balance_sheet_items=[],
        qbo_client=_StubQBO(accounts=[{"Name": "RBC Chequing 6338"}]),
        zero_tolerance=Decimal("0.00"),
        amount_match_tolerance=Decimal("0.00"),
    )

    res = engine.evaluate(rulebook=rulebook, ctx=ctx)
    assert res[0]["status"] == "passed"


def test_engine_qbo_aging_items_require_mer_comment_explanation_when_findings_exist() -> None:
    engine = MERBalanceSheetRuleEngine()

    rulebook = {
        "rules": [
            {
                "rule_id": "BS-AP-AR-ITEMS-OLDER-THAN-60-DAYS",
                "title": "AP/AR items older than 60 days flagged",
                "evaluation": {"type": "qbo_aging_items_older_than_threshold_require_explanation"},
                "parameters": {"max_age_days": 60},
            }
        ]
    }

    # Minimal shaped reports with one overdue item each.
    # The extractor looks for bucket columns whose start-day > max_age_days.
    ap_detail = {
        "Columns": {
            "Column": [
                {"ColTitle": "Name"},
                {"ColTitle": "Total"},
                {"ColTitle": "61 - 90"},
            ]
        },
        "Rows": {
            "Row": [
                {
                    "ColData": [
                        {"value": "Vendor A"},
                        {"value": "100.00"},
                        {"value": "10.00"},
                    ]
                }
            ]
        },
    }
    ar_detail = {
        "Columns": {
            "Column": [
                {"ColTitle": "Name"},
                {"ColTitle": "Total"},
                {"ColTitle": "61 - 90"},
            ]
        },
        "Rows": {
            "Row": [
                {
                    "ColData": [
                        {"value": "Customer A"},
                        {"value": "200.00"},
                        {"value": "20.00"},
                    ]
                }
            ]
        },
    }

    # No comments present on AP/AR lines -> should fail when findings exist.
    rows = [
        ["Account", "Nov. 2025", "Comments"],
        ["Accounts Payable", "100.00", ""],
        ["Accounts Receivable", "200.00", ""],
    ]

    ctx = MERBalanceSheetEvaluationContext(
        end_date="2025-11-30",
        mer_rows=rows,
        mer_selected_month_header="Nov. 2025",
        mer_header_row_index=0,
        qbo_balance_sheet_items=[],
        qbo_client=_StubQBO(aged_payables_detail=ap_detail, aged_receivables_detail=ar_detail),
        zero_tolerance=Decimal("0.00"),
        amount_match_tolerance=Decimal("0.00"),
    )

    res = engine.evaluate(rulebook=rulebook, ctx=ctx)
    assert res[0]["status"] == "failed"

    # Add explanations in MER comments -> should pass.
    rows_with_comments = [
        ["Account", "Nov. 2025", "Comments"],
        ["Accounts Payable", "100.00", "Explained - waiting credit note"],
        ["Accounts Receivable", "200.00", "Explained - dispute in progress"],
    ]
    ctx2 = MERBalanceSheetEvaluationContext(
        end_date="2025-11-30",
        mer_rows=rows_with_comments,
        mer_selected_month_header="Nov. 2025",
        mer_header_row_index=0,
        qbo_balance_sheet_items=[],
        qbo_client=_StubQBO(aged_payables_detail=ap_detail, aged_receivables_detail=ar_detail),
        zero_tolerance=Decimal("0.00"),
        amount_match_tolerance=Decimal("0.00"),
    )

    res2 = engine.evaluate(rulebook=rulebook, ctx=ctx2)
    assert res2[0]["status"] == "passed"


def test_engine_marks_unknown_eval_types_unimplemented() -> None:
    engine = MERBalanceSheetRuleEngine()

    rulebook = {
        "rules": [
            {
                "rule_id": "X-1",
                "evaluation": {"type": "does_not_exist"},
            }
        ]
    }

    ctx = MERBalanceSheetEvaluationContext(
        end_date="2025-11-30",
        mer_rows=[["Account", "Nov. 2025"], ["Petty Cash", "10.00"]],
        mer_selected_month_header="Nov. 2025",
        mer_header_row_index=0,
        qbo_balance_sheet_items=[ReportLineItem(label="Petty Cash", amount="10.00")],
        qbo_client=_StubQBO(),
        zero_tolerance=Decimal("0.00"),
        amount_match_tolerance=Decimal("0.00"),
    )

    res = engine.evaluate(rulebook=rulebook, ctx=ctx)
    assert res == [
        {
            "rule_id": "X-1",
            "status": "unimplemented",
            "evaluation_type": "does_not_exist",
        }
    ]


def test_engine_evaluates_balance_sheet_zero_rule() -> None:
    engine = MERBalanceSheetRuleEngine()

    rulebook = {
        "rules": [
            {
                "rule_id": "BS-UNDEPOSITED-FUNDS-ZERO",
                "title": "Undeposited Funds should be zero at period end",
                "applies_to": {
                    "qbo_balance_sheet_lines": {"label_contains_any": ["undeposited"]}
                },
                "evaluation": {"type": "balance_sheet_line_items_must_be_zero"},
            }
        ]
    }

    rows = [["Account", "Nov. 2025"], ["Undeposited Funds", "0.00"]]

    ctx = MERBalanceSheetEvaluationContext(
        end_date="2025-11-30",
        mer_rows=rows,
        mer_selected_month_header="Nov. 2025",
        mer_header_row_index=0,
        qbo_balance_sheet_items=[
            ReportLineItem(label="Undeposited Funds", amount="0.00")
        ],
        qbo_client=_StubQBO(),
        zero_tolerance=Decimal("0.00"),
        amount_match_tolerance=Decimal("0.00"),
    )

    res = engine.evaluate(rulebook=rulebook, ctx=ctx)
    assert res[0]["rule_id"] == "BS-UNDEPOSITED-FUNDS-ZERO"
    assert res[0]["evaluation_type"] == "balance_sheet_line_items_must_be_zero"
    assert res[0]["status"] == "passed"


def test_engine_evaluates_mer_line_amount_matches_qbo_line_amount() -> None:
    engine = MERBalanceSheetRuleEngine()

    rulebook = {
        "rules": [
            {
                "rule_id": "BS-PETTY-CASH-MATCH",
                "title": "Petty cash matches between MER and QBO",
                "applies_to": {
                    "qbo_balance_sheet_lines": {"label_contains_any": ["petty cash"]}
                },
                "evaluation": {"type": "mer_line_amount_matches_qbo_line_amount"},
            }
        ]
    }

    rows = [["Account", "Nov. 2025"], ["Petty Cash", "10.00"]]

    ctx = MERBalanceSheetEvaluationContext(
        end_date="2025-11-30",
        mer_rows=rows,
        mer_selected_month_header="Nov. 2025",
        mer_header_row_index=0,
        qbo_balance_sheet_items=[ReportLineItem(label="Petty Cash", amount="10.00")],
        qbo_client=_StubQBO(),
        zero_tolerance=Decimal("0.00"),
        amount_match_tolerance=Decimal("0.00"),
    )

    res = engine.evaluate(rulebook=rulebook, ctx=ctx)
    assert res[0]["status"] == "passed"
    assert res[0]["details"]["mer_a1_cell"] == "B2"
    assert res[0]["details"]["qbo_first_match_raw"] == "10.00"


def test_engine_evaluates_qbo_report_total_matches_balance_sheet_line_ap() -> None:
    engine = MERBalanceSheetRuleEngine()

    rulebook = {
        "rules": [
            {
                "rule_id": "BS-AP-SUBLEDGER-RECONCILES",
                "title": "AP aging total matches balance sheet",
                "evaluation": {
                    "type": "qbo_report_total_matches_balance_sheet_line",
                    "qbo_reports_required": ["aged_payables_detail"],
                },
            }
        ]
    }

    aged_payables_total = {
        "Columns": {"Column": [{"ColTitle": "Name"}, {"ColTitle": "Total"}]},
        "Rows": {"Row": [{"ColData": [{"value": "TOTAL"}, {"value": "100.00"}]}]},
    }

    ctx = MERBalanceSheetEvaluationContext(
        end_date="2025-11-30",
        mer_rows=[["Account", "Nov. 2025"]],
        mer_selected_month_header="Nov. 2025",
        mer_header_row_index=0,
        qbo_balance_sheet_items=[
            ReportLineItem(label="Accounts Payable", amount="100.00")
        ],
        qbo_client=_StubQBO(aged_payables_total=aged_payables_total),
        zero_tolerance=Decimal("0.00"),
        amount_match_tolerance=Decimal("0.00"),
    )

    res = engine.evaluate(rulebook=rulebook, ctx=ctx)
    assert res[0]["status"] == "passed"
    assert res[0]["details"]["aging_report_evidence"]["matched_row_label"] == "TOTAL"


def test_collect_action_items_includes_manual_sop_and_process_actions() -> None:
    rulebook = {
        "rules": [
            {
                "rule_id": "R-1",
                "title": "Example",
                "manual_attestation_required": True,
                "sop_expectation": {"required_step": "do the thing"},
                "process_actions": [{"action": "raise_obp_ticket"}],
            }
        ]
    }

    items = collect_action_items(rulebook)
    assert len(items) == 1
    assert items[0]["rule_id"] == "R-1"
    assert items[0]["actions"] == [
        "manual_attestation_required",
        "raise_obp_ticket",
        "required_manual_review_step",
    ]


def test_engine_returns_needs_human_review_for_requires_external_reconciliation_verification() -> None:
    engine = MERBalanceSheetRuleEngine()

    rulebook = {
        "rules": [
            {
                "rule_id": "BS-BANK-RECONCILED-THROUGH-PERIOD-END",
                "title": "Bank accounts reconciled through statement date",
                "requires_external_sources": ["reconciliation_spreadsheet"],
                "evaluation": {"type": "requires_external_reconciliation_verification"},
            }
        ]
    }

    ctx = MERBalanceSheetEvaluationContext(
        end_date="2025-11-30",
        mer_rows=[["Account", "Nov. 2025"]],
        mer_selected_month_header="Nov. 2025",
        mer_header_row_index=0,
        qbo_balance_sheet_items=[],
        qbo_client=_StubQBO(),
        zero_tolerance=Decimal("0.00"),
        amount_match_tolerance=Decimal("0.00"),
    )

    res = engine.evaluate(rulebook=rulebook, ctx=ctx)
    assert res[0]["status"] == "needs_human_review"
    assert res[0]["evaluation_type"] == "requires_external_reconciliation_verification"
    assert res[0]["details"]["reason"] == "requires_external_reconciliation_verification"
    assert res[0]["details"]["required_sources"] == ["reconciliation_spreadsheet"]


def test_engine_returns_needs_human_review_for_manual_process_required() -> None:
    engine = MERBalanceSheetRuleEngine()

    rulebook = {
        "rules": [
            {
                "rule_id": "BS-AP-ENKEL-BILLS",
                "title": "Bills from Enkel investigated",
                "evaluation": {"type": "manual_process_required"},
                "parameters": {"vendor_name_match": {"requires_client_mapping": True}},
                "process_actions": [{"action": "raise_obp_ticket"}],
            }
        ]
    }

    ctx = MERBalanceSheetEvaluationContext(
        end_date="2025-11-30",
        mer_rows=[["Account", "Nov. 2025"]],
        mer_selected_month_header="Nov. 2025",
        mer_header_row_index=0,
        qbo_balance_sheet_items=[],
        qbo_client=_StubQBO(),
        zero_tolerance=Decimal("0.00"),
        amount_match_tolerance=Decimal("0.00"),
    )

    res = engine.evaluate(rulebook=rulebook, ctx=ctx)
    assert res[0]["status"] == "needs_human_review"
    assert res[0]["evaluation_type"] == "manual_process_required"
    assert res[0]["details"]["reason"] == "manual_process_required"
    assert "raise_obp_ticket" in res[0]["details"]["action_items"]


def test_engine_mer_lines_require_link_to_support_checks_comments_column() -> None:
    engine = MERBalanceSheetRuleEngine()

    rulebook = {
        "rules": [
            {
                "rule_id": "BS-WORKING-PAPER-LINKS-PRESENT-IN-MER",
                "title": "Links to working papers included in Balance Sheet report",
                "requires_external_sources": ["mer_google_sheet"],
                "evaluation": {"type": "mer_lines_require_link_to_support"},
            }
        ]
    }

    rows = [
        ["Account", "Nov. 2025", "Comments"],
        ["Equipment", "100.00", ""],
    ]

    ctx = MERBalanceSheetEvaluationContext(
        end_date="2025-11-30",
        mer_rows=rows,
        mer_selected_month_header="Nov. 2025",
        mer_header_row_index=0,
        qbo_balance_sheet_items=[],
        qbo_client=_StubQBO(),
        zero_tolerance=Decimal("0.00"),
        amount_match_tolerance=Decimal("0.00"),
    )

    res = engine.evaluate(rulebook=rulebook, ctx=ctx)
    assert res[0]["status"] == "failed"
    assert res[0]["evaluation_type"] == "mer_lines_require_link_to_support"
    assert res[0]["details"]["missing_support_count"] == 1
    assert res[0]["details"]["missing_support"][0]["comments_a1_cell"] == "C2"


def test_engine_support_link_presence_check_loan_rows_only() -> None:
    engine = MERBalanceSheetRuleEngine()

    rulebook = {
        "rules": [
            {
                "rule_id": "BS-LOAN-SCHEDULE-LINKS-PRESENT",
                "title": "Interest/repayment schedules linked when available",
                "requires_external_sources": ["mer_google_sheet"],
                "evaluation": {"type": "support_link_presence_check"},
            }
        ]
    }

    rows = [
        ["Account", "Nov. 2025", "Comments"],
        ["Office Supplies", "25.00", ""],
        ["Loan Payable", "1000.00", ""],
    ]

    ctx = MERBalanceSheetEvaluationContext(
        end_date="2025-11-30",
        mer_rows=rows,
        mer_selected_month_header="Nov. 2025",
        mer_header_row_index=0,
        qbo_balance_sheet_items=[],
        qbo_client=_StubQBO(),
        zero_tolerance=Decimal("0.00"),
        amount_match_tolerance=Decimal("0.00"),
    )

    res = engine.evaluate(rulebook=rulebook, ctx=ctx)
    assert res[0]["status"] == "failed"
    assert res[0]["evaluation_type"] == "support_link_presence_check"
    assert res[0]["details"]["missing_support_count"] == 1
    assert res[0]["details"]["missing_support"][0]["mer_label"].lower().startswith("loan")
    assert res[0]["details"]["missing_support"][0]["comments_a1_cell"] == "C3"


def test_engine_support_link_presence_check_requires_external_sources_returns_needs_human_review() -> None:
    engine = MERBalanceSheetRuleEngine()

    rulebook = {
        "rules": [
            {
                "rule_id": "BS-PETTY-CASH-FORMAL-RECONCILE-PRESENT",
                "title": "Petty cash has a formal reconciliation artifact",
                "requires_external_sources": ["reconciliation_spreadsheet"],
                "evaluation": {"type": "support_link_presence_check"},
            }
        ]
    }

    rows = [["Account", "Nov. 2025", "Comments"], ["Petty Cash", "10.00", ""]]
    ctx = MERBalanceSheetEvaluationContext(
        end_date="2025-11-30",
        mer_rows=rows,
        mer_selected_month_header="Nov. 2025",
        mer_header_row_index=0,
        qbo_balance_sheet_items=[],
        qbo_client=_StubQBO(),
        zero_tolerance=Decimal("0.00"),
        amount_match_tolerance=Decimal("0.00"),
    )

    res = engine.evaluate(rulebook=rulebook, ctx=ctx)
    assert res[0]["status"] == "needs_human_review"
    assert res[0]["details"]["reason"] == "support_link_presence_check_requires_external_sources"
