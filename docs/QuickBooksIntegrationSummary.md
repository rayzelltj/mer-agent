# QuickBooks Online (QBO) Integration - Current Implementation Summary

This repo integrates with QuickBooks Online for MER review flows (e.g., pulling a Balance Sheet and comparing it to MER entries).

This document intentionally describes the **current, actual code paths** (and does **not** describe older/placeholder endpoints like `/api/v4/quickbooks/*`).

## What’s Implemented

### 1) QBO client + report parsing

- QBO HTTP client and token refresh logic: `src/backend/v4/integrations/qbo_client.py`
- Report parsing helpers (Balance Sheet, etc.): `src/backend/v4/integrations/qbo_reports.py`

The backend MER route uses this client to fetch a Balance Sheet report and then extracts line items for deterministic checks.

### 2) Local OAuth helper (recommended for dev)

- Local OAuth helper script: `scripts/qbo_auth_local.py`

This script runs a local callback server, completes OAuth in your browser, and writes tokens to a local JSON file (defaults to `.env_qbo_tokens.json`).

### 3) MER review integration point

- Backend MER endpoint: `POST /api/v4/mer/review/balance_sheet` (implementation in `src/backend/v4/api/router.py`)

That endpoint:

1. Loads the MER rulebook YAML (default points at `data/mer_rulebooks/balance_sheet_review_points.yaml`)
2. Reads MER entries from Google Sheets
3. Pulls QBO Balance Sheet via `QBOClient`
4. Evaluates rules via the MER rule engine

## Environment Variables (QBO)

The code expects `QBO_*` variables. The key ones are:

- `QBO_CLIENT_ID`
- `QBO_CLIENT_SECRET`
- `QBO_ENVIRONMENT` (`sandbox` or `production`)
- `QBO_REDIRECT_URI` (for local dev, typically `http://localhost:8040/qbo/callback`)
- `QBO_TOKENS_PATH` (optional; defaults to `.env_qbo_tokens.json`)

## How to Run (Dev)

1. Create tokens (one-time per environment/account):

   - Run `python scripts/qbo_auth_local.py`
   - Complete the browser consent
   - Confirm `.env_qbo_tokens.json` was created/updated

2. Run MER checks:

   - Run the backend and call `POST /api/v4/mer/review/balance_sheet`, or
   - Use the MER local runner (`scripts/mer_llm_agent_local.py`) in tool-only mode to exercise the deterministic pipeline.

## Notes / Guardrails

- This repo currently relies on a **local token file** for QBO dev runs; treat it like a secret.
- If you’re looking for the authoritative setup steps, use `docs/QuickBooksSetupGuide.md` (this file is just a summary).

**Last Updated**: January 2026
