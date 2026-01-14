# MER Review Agent — Initial Use Cases (MVP)

This document turns your current "what are we testing" table into explicit, testable use cases.

## Scope

- Focus: 4 checks only (MVP)
- Goal: deterministic checks + explainable outputs
- Non-goals: posting changes to QBO, journaling, or automating accounting decisions

## Shared Definitions

- **MER Period End Date**: the month-end date for the MER being reviewed (e.g. `2025-11-30`).
- **Balance Sheet timing**: checks are evaluated *as-of* the MER period end date. A start date is not required for Balance Sheet validation.
- **Realm ID / Company ID (QBO)**: the `realmId` value returned by Intuit OAuth. This identifies the QBO company file.
- **Source-of-truth**:
  - MER expected values come from the MER Google Sheet.
  - Actual balances come from QBO reports / accounts.

## Core Principle (Line-by-line)

All checks are **line-by-line** Balance Sheet checks:

- If a rule applies to multiple lines (e.g., multiple clearing accounts), each matching line is evaluated independently.
- A check fails if **any applicable line** violates the rule.
- Evidence should be reported per line item (label + amount), not as an aggregated sum.

---

## UC-01 — Clearing account is zero at period end

**Goal**
- Confirm clearing accounts have $0.00$ balance as-of the MER period end date.

**Inputs**
- MER period end date
- MER Google Sheet: which clearing account(s) should be checked (either explicit list or a location in the sheet)

**Data Sources**
- MER Google Sheet Balance Sheet (expected = 0)
- QBO Balance Sheet report (as-of end date)

**QBO Retrieval**
- Balance sheet: `GET /v3/company/{realmId}/reports/BalanceSheet?end_date=YYYY-MM-DD`

**Check Logic (deterministic)**
1. From MER Balance Sheet, identify any line(s) whose label contains `clearing` (case-insensitive).
2. From QBO Balance Sheet, identify any line(s) whose label contains `clearing` (case-insensitive).
3. For each matching line on either side, flag if the line amount is non-zero beyond tolerance.

**Outputs**
- Pass/fail per clearing account line + balance
- Short explanation: "Clearing account should be zero at month end; non-zero indicates uncleared payments/transfers."

**Edge Cases**
- Account names differ between MER and QBO
- Subaccounts
- Currency rounding

**Tests**
- Unit test: parsing report rows and extracting line items
- Integration test (manual): run report for known month and confirm match

---

## UC-02 — Bank accounts reconciled

**Goal**
- Confirm bank accounts are reconciled through the MER period end date.

**Inputs**
- MER period end date
- List of bank accounts to check (from MER sheet or configuration)

**Data Sources**
- MER Google Sheet (lists accounts and/or reconciliation expectations)
- QBO (reconciliation status)

**MVP Alternative (API-available)**
- Instead of fetching the Reconciliation Report (UI-only), compare the MER Balance Sheet bank account balance to the QBO Balance Sheet bank account balance (book value) as-of the same period end date.

**MVP Check Logic (practical)**
1. Identify which bank account line(s) in MER should be checked (explicit list or row keys).
2. Fetch QBO Balance Sheet as-of end date.
3. For each bank account, compare MER amount to QBO Balance Sheet amount within tolerance.

**Outputs**
- Pass/fail per bank account + last reconciled date found
- If not available via API, output a "Needs manual verification" with reason.

**Tests**
- Unit tests around parsing whatever QBO response we decide on.

---

## UC-03 — Undeposited / uncleared funds is zero at period end

**Goal**
- Confirm undeposited funds is $0.00$ as-of the MER period end.

**Inputs**
- MER period end date

**Data Sources**
- MER Google Sheet Balance Sheet (expected = 0)
- QBO Balance Sheet report (source-of-truth)

**Retrieval**
- Balance sheet: `GET /v3/company/{realmId}/reports/BalanceSheet?end_date=YYYY-MM-DD`

**Check Logic**
1. From MER Balance Sheet, identify any line(s) whose label contains `undeposited` (case-insensitive).
2. From QBO Balance Sheet, locate any undeposited funds line item(s) (label may vary).
3. For each matching line on either side, flag if the line amount is non-zero beyond tolerance.

**Outputs**
- Pass/fail + amount
- Explanation: "Undeposited funds should be cleared to deposits at month end."

---

## UC-04 — Petty cash amount matches MER

**Goal**
- Confirm petty cash in MER matches QBO balance sheet as-of end date.

**Inputs**
- MER period end date
- MER Google Sheet petty cash value (from Balance Sheet tab and/or Reconciliation tab)

**Data Sources**
- MER Google Sheet
- QBO Balance Sheet report

**Check Logic**
1. Read petty cash expected value from MER sheet.
2. Fetch QBO balance sheet for end date.
3. Locate petty cash line item in QBO.
4. Compare amounts within tolerance.

**Outputs**
- Pass/fail + (MER value, QBO value, delta)

---

## Next Implementation Steps (recommended order)

1. Lock down naming/config for account matching (clearing accounts list, petty cash label).
2. Build small reusable “connectors” for:
   - Reading values from the MER Google Sheet
   - Fetching QBO Balance Sheet report for a given end date
3. Implement UC-03 and UC-04 first (simplest, report-only).
4. Implement UC-01 (needs mapping from MER accounts to QBO accounts).
5. Investigate UC-02 feasibility via QBO API and define an MVP-compatible check.

---

## Future Direction: Balance Sheet line dispatcher

Once the Balance Sheet logic is fully specified, the intended shape is:

1. Iterate every Balance Sheet line item (MER and/or QBO, depending on the rule).
2. Determine which case (rule) applies to that line based on label/account mapping.
3. Evaluate the rule for that specific line and emit a pass/fail with evidence.

This keeps the system audit-friendly: reviewers can see exactly which line was checked, which rule was applied, and why it passed/failed.

---

# Balance Sheet — Remaining Use Cases (Exhaustive from Best Practices)

This section enumerates every Balance Sheet–related review point implied by the Best Practices text (beyond UC-01..UC-04).

## Conventions

For each use case below:

- **What are we testing**: machine-checkable statement (even if it ultimately becomes `Needs Human Review`).
- **Source**: system(s) providing the evidence.
- **Matching against**: the expected value / condition / comparator.
- **QBO-only?**:
  - **✅ QBO-only**: can be evaluated using QBO data + MER sheet only.
  - **⚠️ External**: needs Drive/Sheets/KYC/SOP/Plooto/Dext/Karbon/other companies.

Where the Best Practices doc implies a requirement but does not specify a deterministic test (e.g., “investigate”), the agent should:

- Emit `Warn` or `Needs Human Review`
- Include a *triage playbook* (what to look at next) rather than inventing an accounting conclusion.

## Bank & Credit Card Reconciliations

### BS-BANK-01 — Bank/CC account inventory matches “accounts to reconcile” list

- **What are we testing**: every bank/credit card account that the client expects to be reconciled is present in QBO and mapped to a Balance Sheet line in MER.
- **Source**: Client maintenance sheet + KYC (“List of bank accounts and credit cards” tab), MER sheet Balance Sheet, QBO Chart of Accounts + Balance Sheet report.
- **Matching against**: 1) existence in QBO, 2) correct type (Bank / Credit Card), 3) presence in MER package.
- **QBO-only?**: ⚠️ External (maintenance/KYC needed).
- **Edge cases**: account nicknames vs legal names; subaccounts; inactive accounts; merged accounts; multi-currency bank accounts.

### BS-BANK-02 — Reconciliation coverage for every available statement

- **What are we testing**: each bank/CC account is formally reconciled for every available statement up to the MER period end date.
- **Source**: QBO reconciliation metadata (if accessible), bank statements (Drive), reconciliation spreadsheet (Drive).
- **Matching against**: statement end dates ≤ MER end date.
- **QBO-only?**: ⚠️ External (statements), and reconciliation status is often not fully available via QBO API.
- **Note**: If reconciliation status cannot be fetched, emit `Needs Human Review: QBO reconciliation status not API-available`.

### BS-BANK-03 — Bank/CC reconciliations are to statement date

- **What are we testing**: reconciliation is performed up to the statement end date (not an arbitrary cut-off).
- **Source**: reconciliation spreadsheet (Drive) + statement PDFs/CSVs.
- **Matching against**: statement end date.
- **QBO-only?**: ⚠️ External.

### BS-BANK-04 — Uncleared items after reconciliation are explicitly explained

- **What are we testing**: any uncleared transaction remaining after reconciliation has an explanation/comment, is flagged to leads, and is present in the reconciliation spreadsheet.
- **Source**: reconciliation spreadsheet (Drive), Karbon note(s) if used for escalation.
- **Matching against**: presence of explanation per uncleared item.
- **QBO-only?**: ⚠️ External.
- **Edge cases**:
  - “Explained” might be: comment present, link present, or a structured reason code.
  - Some uncleared items are acceptable timing differences (next-statement clearing date).

### BS-BANK-05 — Uncleared item explanations follow an allowed reason set

- **What are we testing**: each uncleared item explanation falls into one of the allowed categories:
  - clears in next statement (date provided)
  - additional info required (explicitly stated)
  - steps taken / proposed to resolve or remove
- **Source**: reconciliation spreadsheet (Drive).
- **Matching against**: allowed reason categories.
- **QBO-only?**: ⚠️ External.

### BS-BANK-06 — Uncleared items cross-checked against AP/AR to detect duplicates

- **What are we testing**: reviewer has checked whether uncleared bank/CC transactions correspond to items sitting in AP/AR (potential duplicate cash-coded expenses).
- **Source**: QBO AR/AP detail reports + bank feed transactions; reconciliation spreadsheet commentary.
- **Matching against**: evidence of cross-check (comment + link) and/or detected duplicates.
- **QBO-only?**: ⚠️ External for “evidence performed”; **✅ QBO-only** for “potential duplicate detection” heuristics.
- **Edge cases**: vendor names missing on bank feed cash-coded entries; FX conversion differences.

### BS-BANK-07 — FX uncleared items flagged for exchange-rate/duplicate risk

- **What are we testing**: any uncleared items with foreign currency attributes are flagged for potential exchange issues or cash-coded duplicates.
- **Source**: QBO transactions (currency/exchange rate), bank/CC statements.
- **Matching against**: presence of FX-related uncleared items + explanation.
- **QBO-only?**: ⚠️ External for statement confirmation; **✅ QBO-only** to detect “this is FX”.

## Plooto (Clearing + Instant)

### BS-PLOOTO-01 — Plooto clearing account must be exactly zero

- **What are we testing**: Plooto Clearing account balance is 0.00 at MER period end.
- **Source**: QBO Balance Sheet.
- **Matching against**: expected 0.00 (with tolerance).
- **QBO-only?**: ✅ QBO-only.
- **Edge cases**: account label differs (“Plooto Clearing”, “Plooto - Clearing”, subaccounts).

### BS-PLOOTO-02 — If Plooto clearing ≠ 0, classify likely root-cause bucket

- **What are we testing**: non-zero Plooto clearing is categorized into one or more likely causes:
  - manual payments through Plooto not recorded
  - payment failed / not accepted / not recalled into Plooto Instant
  - payment sent/recorded but not yet approved
- **Source**: Plooto transaction report spreadsheets.
- **Matching against**: presence/absence of transactions supporting each bucket.
- **QBO-only?**: ⚠️ External (Plooto).

### BS-PLOOTO-03 — Plooto Instant live balance is disclosed when relevant

- **What are we testing**: if Plooto Instant has funds and client doesn’t keep a running balance there, the balance is identified to the client and suggested for transfer.
- **Source**: Plooto dashboard live balance + KYC client behavior.
- **Matching against**: disclosure note exists.
- **QBO-only?**: ⚠️ External.

## Wise / OFX / Other FX Accounts

### BS-FX-01 — Delegate access exists and statements can be downloaded

- **What are we testing**: the team has access to download statements to reconcile monthly.
- **Source**: KYC/access checklist + Wise/OFX.
- **Matching against**: access present.
- **QBO-only?**: ⚠️ External.

### BS-FX-02 — FX account balances reconcile to statements monthly

- **What are we testing**: FX account balance in QBO as-of MER end matches statement-derived balance.
- **Source**: QBO Balance Sheet + FX statements.
- **Matching against**: statement balance at period end.
- **QBO-only?**: ⚠️ External.

## Clearing Accounts (Non-Plooto)

### BS-CLEAR-01 — Clearing accounts expected to be 0 are actually 0

- **What are we testing**: for clearing accounts that must be 0 at period end, balance is 0.
- **Source**: MER sheet (which accounts are clearing) + QBO Balance Sheet.
- **Matching against**: 0.00.
- **QBO-only?**: ✅ QBO-only (assuming MER sheet provides list).

### BS-CLEAR-02 — Clearing accounts with acceptable ranges have range documented

- **What are we testing**: acceptable balance/range is documented in KYC; if not, raise OBP ticket.
- **Source**: KYC + OBP ticketing evidence (process).
- **Matching against**: range exists.
- **QBO-only?**: ⚠️ External.

### BS-CLEAR-03 — Clearing account balances are consistent month-to-month

- **What are we testing**: clearing accounts are reviewed for consistency across months.
- **Source**: MER sheet Balance Sheet (current + 3 prior) and/or QBO Balance Sheet across multiple dates.
- **Matching against**: expected pattern or threshold (client-specific).
- **QBO-only?**: ✅ QBO-only for variance detection; ⚠️ External if thresholds are in SOP/KYC.

## Petty Cash

### BS-CASH-01 — Petty cash formally reconciled when client provides a balance

- **What are we testing**: when a client provides petty cash balance (ideally monthly), a formal reconciliation exists.
- **Source**: petty cash reconciliation working paper (Drive) + MER sheet link.
- **Matching against**: reconciliation present and agrees to QBO.
- **QBO-only?**: ⚠️ External.

## Loans / Mortgages / Term Deposits / GICs / Investments

### BS-LOAN-01 — Statements/schedules exist for each loan/investment account

- **What are we testing**: each loan/investment has a statement or schedule available.
- **Source**: Drive schedules/statements + MER links.
- **Matching against**: document exists and is linked.
- **QBO-only?**: ⚠️ External.

### BS-LOAN-02 — Loan/investment balances reconcile monthly

- **What are we testing**: QBO Balance Sheet matches schedule/statement balance at MER end.
- **Source**: QBO Balance Sheet + statement/schedule.
- **Matching against**: statement/schedule balance.
- **QBO-only?**: ⚠️ External.

### BS-LOAN-03 — Loan/investment spreadsheet reconciliation includes uncleared items + comments

- **What are we testing**: reconciliation spreadsheet lists uncleared items and each has a comment (clears next statement / more info needed / steps taken).
- **Source**: reconciliation spreadsheet.
- **Matching against**: comment completeness.
- **QBO-only?**: ⚠️ External.

### BS-LOAN-04 — Interest/repayment schedule link included when enough info exists

- **What are we testing**: where possible, a schedule exists and is linked.
- **Source**: Drive schedule + MER links.
- **Matching against**: link present.
- **QBO-only?**: ⚠️ External.

### BS-LOAN-05 — Flag missing info / suspected new loans or investments

- **What are we testing**: if QBO shows activity suggesting a new loan/investment, but no schedule exists, flag to reviewer.
- **Source**: QBO account activity + Drive/KYC inventory.
- **Matching against**: presence of supporting docs.
- **QBO-only?**: ⚠️ External for “missing doc” confirmation; **✅ QBO-only** for “new liability/asset appears”.

## Accounts Payable / Accounts Receivable (Balance Sheet side)

### BS-APAR-01 — Aged AP total reconciles to Balance Sheet AP

- **What are we testing**: Aged Payables Detail total equals Balance Sheet Accounts Payable at MER end.
- **Source**: QBO Aged Payables Detail report + QBO Balance Sheet.
- **Matching against**: AP balance at end date.
- **QBO-only?**: ✅ QBO-only.
- **Edge cases**: some files use multiple AP accounts; sub-ledgers; class/location segments; foreign currency.

### BS-APAR-02 — Aged AR total reconciles to Balance Sheet AR

- **What are we testing**: Aged Receivables Detail total equals Balance Sheet Accounts Receivable at MER end.
- **Source**: QBO Aged Receivables Detail report + QBO Balance Sheet.
- **Matching against**: AR balance at end date.
- **QBO-only?**: ✅ QBO-only.

### BS-APAR-03 — Any open items older than 60 days are flagged and commented

- **What are we testing**: bills/invoices/payments > 60 days past due are identified and have a comment.
- **Source**: QBO Aged AP/AR detail + MER sheet annotations.
- **Matching against**: “today” or MER end date aging bucket > 60 days.
- **QBO-only?**: ✅ QBO-only for detection; ⚠️ External for verifying comments exist in MER sheet.

### BS-APAR-04 — Multi-currency QBO: currency revaluation posted

- **What are we testing**: currency revaluation has been posted for the period.
- **Source**: QBO Journal Entries / audit log / revaluation artifacts.
- **Matching against**: presence of revaluation entry in/near period end.
- **QBO-only?**: ✅ QBO-only (subject to API feasibility).
- **Edge cases**: revaluation timing; revaluation posted after period end; revaluation posted but reversed.

### BS-APAR-05 — Multi-currency QBO: AP/AR reconciles by currency using revalued open balance

- **What are we testing**: when filtering AP/AR detail by currency and using the “revalued open balance” column, totals reconcile to BS.
- **Source**: QBO AP/AR detail report by currency + QBO Balance Sheet.
- **Matching against**: BS per-date AR/AP.
- **QBO-only?**: ✅ QBO-only (if QBO report supports the required columns via API export).
- **Note**: if API cannot produce per-currency revalued totals, emit `Needs Human Review`.

### BS-APAR-06 — Negative items (credits/overpayments/prepayments) are justified

- **What are we testing**: any negative/open credit items have supporting evidence and expected disposition.
- **Source**: QBO open credits + Dext source docs + bank/CC evidence.
- **Matching against**: documentation exists; not duplicated in cash coding.
- **QBO-only?**: ⚠️ External.

### BS-APAR-07 — Paid-but-still-open items flagged (including RB9**** pattern)

- **What are we testing**: open AP items that appear paid (e.g., reference like RB9****) are flagged with payment details.
- **Source**: QBO bill fields + attachments/source docs (Dext) + bank/CC transactions.
- **Matching against**: presence of payment evidence and/or matching bank transaction.
- **QBO-only?**: ⚠️ External.

### BS-APAR-08 — Ensure paid items are truly removed from AP/AR (not hidden)

- **What are we testing**: if paid, payment is recorded in QBO and the transaction is removed from AP/AR properly.
- **Source**: QBO transaction linkage (bill → bill payment) + open status.
- **Matching against**: open balance = 0; not merely excluded from client-facing list.
- **QBO-only?**: ✅ QBO-only.

### BS-APAR-09 — Cash-coded duplicate risk due to missing vendor/customer names

- **What are we testing**: bank feed cash-coded transactions have vendor/customer names; missing names increase duplicate risk.
- **Source**: QBO bank feed/transactions.
- **Matching against**: presence of entity name.
- **QBO-only?**: ✅ QBO-only.

### BS-APAR-10 — Intercompany/shareholder-paid AP items identified

- **What are we testing**: bills paid by related company or shareholder loan are marked appropriately.
- **Source**: QBO (multiple companies) + related companies’ bank/CC transactions.
- **Matching against**: evidence of payment outside current entity.
- **QBO-only?**: ⚠️ External (requires multi-realm access).

### BS-APAR-11 — Foreign currency bill paid with different currency: FX discrepancy risk flagged

- **What are we testing**: open/overpaid anomalies caused by FX rate application are flagged.
- **Source**: QBO bills + payments + currency fields; credit card statement shows foreign + converted amounts.
- **Matching against**: consistent application of FX; open balance explained.
- **QBO-only?**: ⚠️ External for statement confirmation; **✅ QBO-only** to detect mismatch risk patterns.

### BS-APAR-12 — New bills already overdue are flagged to client

- **What are we testing**: bills entered since last AP cycle that were already overdue are flagged.
- **Source**: QBO bills (create date vs due date) + Karbon/client comms.
- **Matching against**: due date < create date; internal/client flag exists.
- **QBO-only?**: ⚠️ External for “flagged to client” evidence; **✅ QBO-only** for detection.

### BS-APAR-13 — Bills from Enkel appearing in AP trigger internal billing verification

- **What are we testing**: if vendor=Enkel bills appear in AP, create/record a request in internal billing channel.
- **Source**: QBO AP detail + internal process evidence.
- **Matching against**: presence of escalation.
- **QBO-only?**: ⚠️ External.

### BS-APAR-14 — Year-end batch AP/AR adjustments require transaction-level breakdown

- **What are we testing**: YE accountant batch adjustments are not booked to a generic vendor/customer without breakdown; uncleared batches are flagged.
- **Source**: QBO journal entries + supporting correspondence.
- **Matching against**: breakdown exists (list of invoices) or escalation note.
- **QBO-only?**: ⚠️ External for “breakdown provided”; **✅ QBO-only** to detect generic batch postings.

### BS-APAR-15 — Items paid after month-end are annotated with payment date/method

- **What are we testing**: for AP items open at month-end but paid after, MER includes payment date/method.
- **Source**: QBO payment transactions + MER sheet comments.
- **Matching against**: comment present for post-period payment.
- **QBO-only?**: ⚠️ External (MER comments); **✅ QBO-only** to detect “paid after end date”.

## Intercompany Loans (Balance Sheet)

### BS-IC-01 — Intercompany balances reconcile across all relevant entities

- **What are we testing**: if Enkel does bookkeeping for related company, intercompany loan balances match across entities.
- **Source**: QBO Balance Sheets for both/all companies OR intercompany financial statements.
- **Matching against**: equal and opposite balances (per agreed convention).
- **QBO-only?**: ⚠️ External (multi-company access required).

### BS-IC-02 — Intercompany account formally reconciled when EOM balance exists

- **What are we testing**: intercompany loan is formally reconciled in both/all QBO companies when an EOM balance exists.
- **Source**: reconciliation evidence (QBO/UI) + reconciliation spreadsheet.
- **Matching against**: reconciliation completed through MER end.
- **QBO-only?**: ⚠️ External (reconciliation evidence).

### BS-IC-03 — Intercompany reconciliation variances captured in reconciliation spreadsheet

- **What are we testing**: unresolved intercompany discrepancies are included as a variance in the reconciliation spreadsheet.
- **Source**: reconciliation spreadsheet.
- **Matching against**: variance line exists.
- **QBO-only?**: ⚠️ External.

## Prepaids / Deferred Revenue / Accruals

### BS-WP-01 — Working papers reconcile to Balance Sheet

- **What are we testing**: working paper total equals QBO Balance Sheet line balance.
- **Source**: working paper (Drive/Sheets) + QBO Balance Sheet + MER link.
- **Matching against**: equality within tolerance.
- **QBO-only?**: ⚠️ External.

### BS-WP-02 — KYC specifies required month-end journals and they were processed

- **What are we testing**: client-specific month-end journals in KYC were posted/processed.
- **Source**: KYC + QBO journal entries.
- **Matching against**: expected JE set exists for the month.
- **QBO-only?**: ⚠️ External (KYC required).

### BS-WP-03 — Working papers are reviewed, updated, and not recreated monthly

- **What are we testing**: a single canonical working paper is reused and updated; new items added each month.
- **Source**: Drive file history + working paper content.
- **Matching against**: stable working paper reference; new items present.
- **QBO-only?**: ⚠️ External.

### BS-WP-04 — Recurring JEs exist for consistent monthly recognition amounts

- **What are we testing**: recurring JEs are set up where amounts are consistent month-to-month.
- **Source**: QBO recurring transactions / JE patterns.
- **Matching against**: presence of recurring template or consistent cadence.
- **QBO-only?**: ✅ QBO-only (subject to API availability).

### BS-WP-05 — Working paper includes variance formula (QBO balance vs WP total)

- **What are we testing**: working paper has a check calculation tying to QBO/Xero and highlighting variance.
- **Source**: working paper.
- **Matching against**: presence of check cell/formula.
- **QBO-only?**: ⚠️ External.

## Tax Accounts (Balance Sheet)

### BS-TAX-01 — Sales tax filings completed through most recent period

- **What are we testing**: sales tax filings in QBO are completed through the latest required period.
- **Source**: QBO sales tax filing data + CRA/provincial schedule if applicable.
- **Matching against**: filing period end ≥ MER end (or as required by filing frequency).
- **QBO-only?**: ⚠️ External if CRA/prov portal is the true payable source.

### BS-TAX-02 — Tax payable and suspense accounts reconcile to latest return

- **What are we testing**: payable/suspense accounts tie to current/most recent period’s return.
- **Source**: QBO tax liability/return + QBO Balance Sheet.
- **Matching against**: return amount.
- **QBO-only?**: ✅ QBO-only if return data is accessible; otherwise ⚠️ External.

### BS-TAX-03 — GST/PST/HST/PSB payments/refunds offset the relevant suspense account

- **What are we testing**: tax payments/refunds are posted to the correct suspense account (not misc).
- **Source**: QBO transactions + Chart of Accounts.
- **Matching against**: expected suspense account mapping.
- **QBO-only?**: ✅ QBO-only.

### BS-TAX-04 — Tax payable reconciles to CRA/provincial portal balances owing

- **What are we testing**: QBO tax payable equals balances owing per CRA/prov portal.
- **Source**: QBO Balance Sheet + CRA/prov portal.
- **Matching against**: portal balance.
- **QBO-only?**: ⚠️ External.

### BS-TAX-05 — Interest/penalties recorded and payments matched correctly

- **What are we testing**: interest/penalties exist when present, and payments are matched to them correctly.
- **Source**: QBO transactions + notices (Drive).
- **Matching against**: notice amounts.
- **QBO-only?**: ⚠️ External.

### BS-TAX-06 — Taxes not misclassified (e.g., corporate installments booked to sales tax)

- **What are we testing**: tax-related transactions are posted to the correct tax accounts.
- **Source**: QBO transactions + Chart of Accounts.
- **Matching against**: account classification rules.
- **QBO-only?**: ✅ QBO-only.

## Fixed Assets

### BS-FA-01 — Capitalization threshold applied (per SOP/KYC)

- **What are we testing**: transactions above the capitalization threshold are capitalized; below are expensed.
- **Source**: SOP/KYC threshold + QBO transactions.
- **Matching against**: threshold (default guideline $1,000 unless specified).
- **QBO-only?**: ⚠️ External (threshold source).

### BS-FA-02 — Client-coded items that appear misclassified are flagged

- **What are we testing**: items coded by client that should be expensed (or capitalized) are flagged.
- **Source**: QBO transaction audit trail/user info (if available) + transaction details.
- **Matching against**: capitalization rules.
- **QBO-only?**: ✅ QBO-only for detection; ⚠️ External for final decision.

### BS-FA-03 — Fixed asset register / depreciation schedule reconciles to QBO

- **What are we testing**: FA register/depreciation schedule ties to QBO fixed assets and accumulated depreciation.
- **Source**: FA register (Drive) + QBO Balance Sheet.
- **Matching against**: NBV and accumulated depreciation balances.
- **QBO-only?**: ⚠️ External.

## MER Package Completeness Constraints (Balance Sheet relevant)

### BS-PACK-01 — MER package includes Balance Sheet for current month + at least 3 prior months

- **What are we testing**: review package contains BS for current month and ≥3 prior months.
- **Source**: MER Google Sheet tabs.
- **Matching against**: required month set.
- **QBO-only?**: ⚠️ External (MER sheet structure).

### BS-PACK-02 — Unusual/unexplained variances have annotations (to avoid rework)

- **What are we testing**: any unusual/unexplained variances have commentary next to the line item.
- **Source**: MER sheet annotations.
- **Matching against**: comment/link exists when variance exceeds threshold.
- **QBO-only?**: ⚠️ External.

### BS-PACK-03 — Final reviewer should not need to enter other systems

- **What are we testing**: any flagged item includes a link to supporting docs/threads (Drive/Karbon/etc).
- **Source**: MER sheet comments/links + Karbon note links.
- **Matching against**: link presence.
- **QBO-only?**: ⚠️ External.

## Year-end Special Case

### BS-YE-01 — If MER month is fiscal year-end, reporting scope rules are applied

- **What are we testing**: when MER month is also year-end, the Balance Sheet / Profit & Loss review package uses the correct “full fiscal year” scope as required.
- **Source**: client fiscal year-end (KYC) + MER package configuration.
- **Matching against**: required report periods.
- **QBO-only?**: ⚠️ External (fiscal year-end source).
- **Clarification needed**: for Balance Sheet specifically, “full fiscal year” may mean comparative columns or additional months; the Best Practices text is ambiguous.

