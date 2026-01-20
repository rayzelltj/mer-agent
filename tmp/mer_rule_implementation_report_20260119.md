# MER rule implementation report (local runner)

- Rulebook: data/mer_rulebooks/balance_sheet_review_points.yaml
- Runner: scripts/mer_llm_agent_local.py
- Total rules: 48
- Implemented rules (by runner): 8
- Unimplemented rules (by runner): 40

## Implemented (runner will execute)
- BS-CLEARING-ACCOUNTS-ZERO — balance_sheet_line_items_must_be_zero
- BS-UNDEPOSITED-FUNDS-ZERO — balance_sheet_line_items_must_be_zero
- BS-PETTY-CASH-MATCH — mer_line_amount_matches_qbo_line_amount
- BS-BANK-BOOK-BALANCE-MATCH — mer_bank_balance_matches_qbo_bank_balance
- BS-CC-DEBIT-BOOK-BALANCE-MATCH-ALL — mer_credit_debit_accounts_book_balance_match_qbo
- BS-AP-SUBLEDGER-RECONCILES — qbo_report_total_matches_balance_sheet_line
- BS-AR-SUBLEDGER-RECONCILES — qbo_report_total_matches_balance_sheet_line
- BS-AP-AR-ITEMS-OLDER-THAN-60-DAYS — qbo_aging_items_older_than_threshold_require_explanation

## Unimplemented (runner returns status=unimplemented)
- BS-PETTY-CASH-FORMAL-RECONCILE-PRESENT — support_link_presence_check
- BS-BANK-AND-CC-INVENTORY-COVERAGE — inventory_accounts_must_exist_in_qbo_and_mer
- BS-BANK-RECONCILED-THROUGH-PERIOD-END — requires_external_reconciliation_verification
- BS-CC-RECONCILED-THROUGH-PERIOD-END — requires_external_reconciliation_verification
- BS-UNCLEARED-ITEMS-INVESTIGATED-AND-FLAGGED — reconciliation_uncleared_items_require_explanation_and_flag
- BS-UNCLEARED-ITEMS-COMMENT-WHY-NOT-CONCERN — reconciliation_uncleared_items_require_explanation
- BS-UNCLEARED-ITEMS-SUGGEST-DUPLICATE-SCAN — heuristic_duplicate_detection
- BS-PLOOTO-CLEARING-ZERO — balance_sheet_line_items_must_be_zero_with_external_diagnosis
- BS-PLOOTO-INSTANT-BALANCE-DISCLOSURE — external_live_balance_check
- BS-FX-ACCOUNTS-RECONCILED-MONTHLY — requires_external_statement_reconciliation
- BS-FX-DELEGATE-ACCESS-ATTESTED — needs_human_judgment
- BS-AP-AR-NEGATIVE-OPEN-ITEMS — detect_negative_open_items_and_require_external_justification
- BS-AP-AR-PAID-BUT-STILL-OPEN — heuristic_paid_but_open_detection
- BS-AP-AR-INTERCOMPANY-OR-SHAREHOLDER-PAID — multi_entity_payment_trace
- BS-AP-AR-FOREIGN-CURRENCY-DISCREPANCIES — fx_ap_ar_exception_review
- BS-AP-AR-NEW-OVERDUE-ITEMS — needs_prior_cycle_context
- BS-AP-ENKEL-BILLS — manual_process_required
- BS-AP-AR-YEAR_END_BATCH_ADJUSTMENTS — detect_ap_ar_batch_adjustments_and_require_breakdown
- BS-AP-AR-PAID-AFTER-MONTH-END-NOTED — payments_after_period_end_require_mer_annotation
- BS-INTERCOMPANY-BALANCES-RECONCILE — multi_company_balance_reconciliation
- BS-INTERCOMPANY-FORMAL-RECONCILE-IF-EOM-BALANCE — requires_external_reconciliation_verification
- BS-INTERCOMPANY-VARIANCE-DISCLOSED — variance_must_be_documented
- BS-WORKING-PAPER-RECONCILES — working_paper_balance_matches_qbo_balance_sheet
- BS-WORKING-PAPER-LINKS-PRESENT-IN-MER — mer_lines_require_link_to_support
- BS-MONTH-END-JOURNALS-FROM-KYC-PROCESSED — required_journals_present
- BS-WORKING-PAPER-HAS-CHECK-FORMULA — working_paper_integrity_check
- BS-WORKING-PAPER-SINGLE-COPY-REUSED — working_paper_link_consistency
- BS-TAX-FILINGS-UP-TO-DATE — requires_external_tax_filing_verification
- BS-TAX-PAYABLE-AND-SUSPENSE-RECONCILE-TO-RETURN — tax_accounts_reconcile
- BS-TAX-PAYABLE-MATCHES-PORTAL — external_authority_balance_match
- BS-TAX-INTEREST-PENALTIES-RECORDED — requires_external_notice_verification
- BS-TAX-MISCLASSIFICATION-CHECK — detect_tax_misclassification
- BS-LOAN-AND-INVESTMENT-BALANCES-RECONCILE — external_statement_balance_match
- BS-LOAN-SCHEDULE-LINKS-PRESENT — support_link_presence_check
- BS-LOAN-MISSING-INFO-FLAGGED — needs_human_judgment
- BS-CLEARING-ACCOUNTS-WITHIN-CLIENT-THRESHOLD — balance_within_configured_range
- BS-CLEARING-ACCOUNTS-CONSISTENCY-ACROSS-MONTHS — multi_period_variance_review
- BS-FIXED-ASSET-CAPITALIZATION-THRESHOLD — fixed_asset_capitalization_review
- BS-FIXED-ASSET-REGISTER-RECONCILES — external_register_balance_match
- BS-FIXED-ASSET-CLIENT-CODING-REVIEW — needs_human_judgment
