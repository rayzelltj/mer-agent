# MER Review Agent — Initial Use Cases (MVP)

This document turns your current "what are we testing" table into explicit, testable use cases.

## Scope

- Focus: 4 checks only (MVP)
- Goal: deterministic checks + explainable outputs
- Non-goals: posting changes to QBO, journaling, or automating accounting decisions

## Shared Definitions

- **MER Period End Date**: the month-end date for the MER being reviewed (e.g. `2025-11-30`).
- **Realm ID / Company ID (QBO)**: the `realmId` value returned by Intuit OAuth. This identifies the QBO company file.
- **Source-of-truth**:
  - MER expected values come from the MER Google Sheet.
  - Actual balances come from QBO reports / accounts.

---

## UC-01 — Clearing account is zero at period end

**Goal**
- Confirm clearing accounts have $0.00$ balance as-of the MER period end date.

**Inputs**
- MER period end date
- MER Google Sheet: which clearing account(s) should be checked (either explicit list or a location in the sheet)

**Data Sources**
- MER Google Sheet (expected = 0, and optionally which account names)
- QBO Balance Sheet report (as-of end date) and/or QBO Chart of Accounts

**Proposed QBO Retrieval**
- Balance sheet: `GET /v3/company/{realmId}/reports/BalanceSheet?end_date=YYYY-MM-DD`
- If Balance Sheet line labels don’t map cleanly: use accounts (Query endpoint) to locate clearing accounts by name/type.

**Check Logic (deterministic)**
1. Determine the list of clearing accounts to evaluate (config, or parse from MER sheet).
2. Fetch QBO balances as-of end date.
3. For each clearing account, compute absolute value and compare to tolerance (e.g. `<= 0.01`).

**Outputs**
- Pass/fail per clearing account + balance
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

**Important Note (API uncertainty)**
- QBO’s UI shows reconciliation history, but the public API support for reconciliation “history report” can be limited.
- MVP strategy: define exactly what we can reliably fetch via API (e.g., last reconciled date per account) and align the check to that.

**MVP Check Logic (practical)**
1. Get list of bank accounts.
2. For each bank account, obtain the latest reconciliation cutoff date (if available via API/report).
3. Pass if cutoff date >= period end date.

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
- QBO Balance Sheet report (source-of-truth)

**Retrieval**
- Balance sheet: `GET /v3/company/{realmId}/reports/BalanceSheet?end_date=YYYY-MM-DD`

**Check Logic**
1. Fetch QBO balance sheet for end date.
2. Locate the undeposited funds line item (label may vary).
3. Pass if value is within tolerance.

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
