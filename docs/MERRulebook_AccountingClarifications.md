# MER Rulebook — Accounting Clarifications (meeting-ready)

Audience: accounting experts / ops owners.
Goal: replace generic “needs_human_review” outputs with precise, consistent, SOP-aligned requirements.
Scope: Balance Sheet rulebook [data/mer_rulebooks/balance_sheet_review_points.yaml](../data/mer_rulebooks/balance_sheet_review_points.yaml).

## 1) Reconciliation verification (bank / credit card / intercompany)

### 1.1 API accessibility for reconciliation status (blocking)
**Question**: Do we have an API-accessible source for reconciliation status and “reconciled through” / last reconciled date (or must we treat this as external-only evidence)?

**Why we need it**: The rule engine can’t deterministically verify “reconciled through period end” unless it can access reconciliation metadata.

**Impacted rules**:
- BS-BANK-RECONCILED-THROUGH-PERIOD-END
- BS-CC-RECONCILED-THROUGH-PERIOD-END
- BS-INTERCOMPANY-FORMAL-RECONCILE-IF-EOM-BALANCE

**Decision options**:
1) “API-accessible”: define exact source + fields (recommended for automation)
   - Source: (e.g., QBO endpoint / internal system / exported report)
   - Required fields: account identifier, reconciled_through_date, statement_end_date, reconciliation_status, evidence link (optional)
2) “External-only”: define required evidence artifact + schema (reconciliation spreadsheet)
   - Evidence artifact location: Google Sheet link policy, tab naming pattern, required columns

**Ask for concrete definition**:
- What is the minimum acceptable evidence to consider an account “reconciled through period end”?
  - Must it match the statement end date exactly?
  - Is “reconciled through ≥ period end” acceptable?
  - Are partial reconciliations acceptable?

### 1.2 Bank/CC reconciliation evidence schema (if external-only)
**Question**: If we rely on a reconciliation spreadsheet, what is the standard schema?

**Needed fields (proposed)**:
- account_name (or account_id)
- statement_end_date
- reconciled_through_date
- cleared_balance / difference (optional)
- evidence_link_to_statement
- reviewer_notes or explanation

**What you need to confirm**:
- Which fields are mandatory vs optional?
- Are there canonical tab names or a single tab?
- How to handle multiple statements in a month?


## 2) Manual process rules — operational action vocabulary

### 2.1 Canonical action labels for manual steps (blocking for “perfect” outputs)
**Question**: What is the canonical set of action labels we should emit for manual-process rules so downstream ops can route them (ticketing/slack/work queues)?

**Why we need it**: Today we can deterministically say “manual process required”, but the action items are generic unless we standardize the vocabulary.

**Impacted evaluation types**:
- manual_process_required
- needs_human_judgment
- requires_external_reconciliation_verification

**Deliverable requested from experts**:
- A short controlled vocabulary (10–30 items) with definitions.

**Examples of candidate actions**:
- raise_obp_ticket
- request_client_documentation
- post_to_billing_channel
- confirm_delegate_access
- attach_workpaper_link
- record_reviewer_rationale

### 2.2 Specific: “Bills from Enkel investigated” routing
Rule: BS-AP-ENKEL-BILLS

**Question**: When this triggers, what exactly should happen?
- Which channel/team owns it? (e.g., #billing)
- Required evidence of completion (link? screenshot? ticket id?)
- Is the “vendor name match” always “Enkel” or client-configurable?

**Decision**:
- Provide exact action mapping, e.g.
  - action: post_to_billing_channel
  - required fields: vendor_display_name, invoice/bill identifier(s), period_end_date


## 3) Lead-flagging definition (reconciliation workpapers)

### 3.1 How to detect “flagged to leads”
**Question**: How should the system detect that an uncleared item has been “flagged to leads” in reconciliation workpapers?

**Why we need it**: Without a schema definition, this check can’t be automated or consistently marked complete.

**Impacted rules**:
- BS-UNCLEARED-ITEMS-INVESTIGATED-AND-FLAGGED

**Decision options**:
- Dedicated column `flagged_to_leads` boolean
- Dedicated column `owner` / `assigned_to`
- @mention in a comment
- Status values (e.g., Open / In Progress / Sent to Lead / Resolved)

**Ask for concrete schema**:
- Which column(s)? what exact values? who is considered a “lead”?


## 4) Clearing account acceptable range (client-specific)

### 4.1 Threshold source + schema
**Question**: For non-Plooto clearing accounts, should acceptable balance/range always come from KYC/SOP (no defaults)? If yes, what is the schema/field?

**Why we need it**: Otherwise the rule can’t be deterministic (it needs a threshold/range).

**Impacted rules**:
- BS-CLEARING-ACCOUNTS-WITHIN-CLIENT-THRESHOLD

**Decision**:
- Where is the authoritative threshold stored? (KYC sheet tab + column)
- Is it a single number (abs(balance) <= x) or a range (min/max)?
- Does it vary by account?


## 5) Capitalization threshold default

### 5.1 Default threshold behavior
**Question**: If a client SOP/KYC does not specify capitalization threshold, should we default to 1000, or require explicit threshold per client?

**Impacted rules**:
- BS-FIXED-ASSET-CAPITALIZATION-THRESHOLD

**Decision**:
- Default threshold value (if any)
- Currency handling (CAD vs USD etc.)
- Exceptions (industries/clients)


## 6) Evidence standards for “explained” items

The rulebook policy currently treats **ANY link OR comment** as “explained”. If you want this to be “perfect”, confirm the minimum quality standard.

### 6.1 What counts as a valid explanation?
**Question**: Is “any comment/link” sufficient, or do we require specific content?

**Where it matters**:
- AP/AR > 60 day items: BS-AP-AR-ITEMS-OLDER-THAN-60-DAYS
- Uncleared items explanations: BS-UNCLEARED-ITEMS-* rules
- Any rule with `explanation_policy_ref`

**Decision options**:
- Minimal: any non-empty comment OR any link
- Moderate: comment length >= N or includes one of (reason/status/next step)
- Strong: structured fields (reason, expected clearance date, owner)


## 7) “Perfect” mappings for labels/row keys (MER vs QBO)

Even where checks are automatable, “perfect” matching requires stable mappings.

### 7.1 Bank book balance mapping (for the MVP alternative)
Rule (currently disabled by default): BS-BANK-BOOK-BALANCE-MATCH

**Question**: What are the canonical MER row label(s) and QBO balance sheet label(s) to match?
- MER row key candidates: “Bank book balance”, “Cash”, “Cash & cash equivalents”, etc.
- QBO label candidates: “Cash and cash equivalents”, specific bank account names, etc.

**Decision**:
- Should this be per-client mapping only?
- If not, provide the default match rules.


## 8) Status taxonomy for outputs (consumer expectations)

We added `needs_human_review` as a rule result status.

### 8.1 Confirm desired meaning of statuses
**Question**: Confirm how you want to interpret these statuses in reporting:
- passed: deterministically met
- failed: deterministically violated
- skipped: not applicable OR rule disabled OR missing configuration
- needs_human_review: cannot be verified automatically; human must confirm
- unimplemented: engine has no handler and no defined human-review fallback

If you want a different taxonomy (e.g., “warning” vs “needs_human_review”), specify it.


## Quick list (for agenda)
1) Reconciliation status access: API vs external-only; required evidence schema
2) Standard action vocabulary for manual steps + routing
3) Lead-flagging definition in reconciliation sheets
4) Clearing acceptable range: source + schema
5) Capitalization threshold default behavior
6) Explanation quality standard
7) Default mapping rules for MER/QBO label matching (esp bank)
8) Confirm result status taxonomy
