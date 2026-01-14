"""QuickBooks Online (QBO) connector.

Purpose
- Provide a small, testable wrapper for *read-only* QBO API calls.
- Keep OAuth token handling (load/save/refresh) in one place.

This module is intentionally independent of FastAPI and the agent framework.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any

import requests
from dotenv import dotenv_values, load_dotenv
from intuitlib.client import AuthClient

load_dotenv(override=False)

# Convenience: allow local runs with only `.env.example` filled.
# Important: `.env.example` contains blank placeholders for secrets; do not
# allow those blanks to override real `.env` values (or to block later loads).
if not os.environ.get("QBO_CLIENT_ID"):
    example_path = os.path.abspath(".env.example")
    if os.path.exists(example_path):
        for k, v in (dotenv_values(example_path) or {}).items():
            if not k or v is None:
                continue
            if v == "":
                continue
            if not os.environ.get(k):
                os.environ[k] = v


@dataclass(slots=True)
class QBOAuthTokens:
    environment: str
    realm_id: str
    access_token: str
    refresh_token: str
    id_token: str | None = None
    saved_at_unix: int | None = None


class QBOClient:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        environment: str,
        tokens_path: str,
        timeout_seconds: int = 30,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._environment = environment
        self._tokens_path = tokens_path
        self._timeout_seconds = timeout_seconds

    @staticmethod
    def _base_url(environment: str) -> str:
        return (
            "https://quickbooks.api.intuit.com"
            if environment == "production"
            else "https://sandbox-quickbooks.api.intuit.com"
        )

    @classmethod
    def from_env(cls) -> "QBOClient":
        # Be defensive: ensure .env has been loaded even if this module
        # was imported in an unusual order.
        load_dotenv(override=False)
        client_id = os.environ.get("QBO_CLIENT_ID")
        client_secret = os.environ.get("QBO_CLIENT_SECRET")
        if not client_id or not client_secret:
            raise ValueError("Missing QBO_CLIENT_ID or QBO_CLIENT_SECRET")

        environment = os.environ.get("QBO_ENVIRONMENT", "sandbox")
        redirect_uri = os.environ.get("QBO_REDIRECT_URI", "http://localhost")
        tokens_path = os.environ.get(
            "QBO_TOKENS_PATH", os.path.abspath(".env_qbo_tokens.json")
        )
        timeout_seconds = int(os.environ.get("QBO_HTTP_TIMEOUT_SECONDS", "30"))

        return cls(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            environment=environment,
            tokens_path=tokens_path,
            timeout_seconds=timeout_seconds,
        )

    def load_tokens(self) -> QBOAuthTokens:
        if not os.path.exists(self._tokens_path):
            raise FileNotFoundError(
                f"Token file not found: {self._tokens_path}. Run scripts/qbo_auth_local.py first."
            )
        with open(self._tokens_path, "r", encoding="utf-8") as f:
            raw = json.load(f)

        return QBOAuthTokens(
            environment=raw.get("environment") or self._environment,
            realm_id=raw["realm_id"],
            access_token=raw["access_token"],
            refresh_token=raw["refresh_token"],
            id_token=raw.get("id_token"),
            saved_at_unix=raw.get("saved_at_unix"),
        )

    def save_tokens(self, tokens: QBOAuthTokens) -> None:
        payload: dict[str, Any] = {
            "environment": tokens.environment,
            "realm_id": tokens.realm_id,
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
            "id_token": tokens.id_token,
            "saved_at_unix": int(time.time()),
        }
        with open(self._tokens_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

    def refresh_tokens(self, tokens: QBOAuthTokens) -> QBOAuthTokens:
        auth = AuthClient(
            client_id=self._client_id,
            client_secret=self._client_secret,
            redirect_uri=self._redirect_uri,
            environment=tokens.environment,
            access_token=tokens.access_token,
            refresh_token=tokens.refresh_token,
            realm_id=tokens.realm_id,
            id_token=tokens.id_token,
        )
        auth.refresh(refresh_token=auth.refresh_token)

        if not auth.access_token or not auth.refresh_token:
            raise RuntimeError(
                "QBO token refresh failed (missing refreshed access_token/refresh_token)"
            )

        updated = QBOAuthTokens(
            environment=tokens.environment,
            realm_id=auth.realm_id or tokens.realm_id,
            access_token=auth.access_token,
            refresh_token=auth.refresh_token,
            id_token=auth.id_token,
            saved_at_unix=int(time.time()),
        )
        self.save_tokens(updated)
        return updated

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        bearer_token: str,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        resp = requests.request(
            method,
            url,
            headers={
                "Authorization": f"Bearer {bearer_token}",
                "Accept": "application/json",
            },
            params=params,
            timeout=self._timeout_seconds,
        )

        # Optional debug (safe): prints only URL/params, never tokens.
        if os.environ.get("QBO_DEBUG") in {"1", "true", "TRUE", "yes", "YES"}:
            print(f"[QBO_DEBUG] {method} {resp.url}")

        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text}")
        return resp.json()

    def get_company_info(self) -> dict[str, Any]:
        tokens = self.load_tokens()
        base = self._base_url(tokens.environment)
        url = f"{base}/v3/company/{tokens.realm_id}/companyinfo/{tokens.realm_id}"

        try:
            return self._request_json("GET", url, bearer_token=tokens.access_token)
        except RuntimeError as e:
            # Common case: expired access token
            if "401" in str(e) or "invalid_token" in str(e).lower():
                tokens = self.refresh_tokens(tokens)
                return self._request_json("GET", url, bearer_token=tokens.access_token)
            raise

    def get_balance_sheet(
        self,
        *,
        end_date: str,
        start_date: str | None = None,
        accounting_method: str | None = None,
        date_macro: str | None = None,
    ) -> dict[str, Any]:
        tokens = self.load_tokens()
        base = self._base_url(tokens.environment)
        url = f"{base}/v3/company/{tokens.realm_id}/reports/BalanceSheet"

        params: dict[str, str] = {"end_date": end_date}
        if start_date:
            params["start_date"] = start_date
        if accounting_method:
            params["accounting_method"] = accounting_method
        if date_macro:
            params["date_macro"] = date_macro

        try:
            return self._request_json(
                "GET",
                url,
                bearer_token=tokens.access_token,
                params=params,
            )
        except RuntimeError as e:
            if "401" in str(e) or "invalid_token" in str(e).lower():
                tokens = self.refresh_tokens(tokens)
                return self._request_json(
                    "GET",
                    url,
                    bearer_token=tokens.access_token,
                    params=params,
                )
            raise

    def _get_report(
        self,
        *,
        report_name: str,
        params: dict[str, str],
    ) -> dict[str, Any]:
        """Fetch a QBO report by name.

        Example report_name values:
        - BalanceSheet
        - AgedPayablesDetail
        - AgedReceivablesDetail
        """

        tokens = self.load_tokens()
        base = self._base_url(tokens.environment)
        url = f"{base}/v3/company/{tokens.realm_id}/reports/{report_name}"

        try:
            return self._request_json(
                "GET",
                url,
                bearer_token=tokens.access_token,
                params=params,
            )
        except RuntimeError as e:
            if "401" in str(e) or "invalid_token" in str(e).lower():
                tokens = self.refresh_tokens(tokens)
                return self._request_json(
                    "GET",
                    url,
                    bearer_token=tokens.access_token,
                    params=params,
                )
            raise

    def get_aged_payables_detail(self, *, end_date: str) -> dict[str, Any]:
        return self._get_report(
            report_name="AgedPayablesDetail",
            params={"end_date": end_date},
        )

    def get_aged_receivables_detail(self, *, end_date: str) -> dict[str, Any]:
        return self._get_report(
            report_name="AgedReceivablesDetail",
            params={"end_date": end_date},
        )

    def query(
        self,
        *,
        query: str,
        minorversion: str | None = None,
    ) -> dict[str, Any]:
        """Run a QBO Query API statement.

        This is used to retrieve Chart of Accounts (Account objects).
        Docs: QBO supports a SQL-like query language at /v3/company/<realmId>/query.
        """

        tokens = self.load_tokens()
        base = self._base_url(tokens.environment)
        url = f"{base}/v3/company/{tokens.realm_id}/query"

        params: dict[str, str] = {"query": query}
        if minorversion:
            params["minorversion"] = minorversion

        try:
            return self._request_json(
                "GET",
                url,
                bearer_token=tokens.access_token,
                params=params,
            )
        except RuntimeError as e:
            if "401" in str(e) or "invalid_token" in str(e).lower():
                tokens = self.refresh_tokens(tokens)
                return self._request_json(
                    "GET",
                    url,
                    bearer_token=tokens.access_token,
                    params=params,
                )
            raise

    def get_accounts(self, *, max_results: int = 1000) -> list[dict[str, Any]]:
        """Fetch Chart of Accounts (Account list) via Query API."""

        # MAXRESULTS is part of the query language; QBO typically limits this.
        q = f"select * from Account MAXRESULTS {int(max_results)}"
        resp = self.query(query=q, minorversion=os.environ.get("QBO_MINORVERSION"))
        qr = resp.get("QueryResponse") if isinstance(resp, dict) else None
        accounts = (qr or {}).get("Account") if isinstance(qr, dict) else None

        if accounts is None:
            return []
        if isinstance(accounts, list):
            return accounts
        if isinstance(accounts, dict):
            return [accounts]
        return []

    def get_trial_balance(
        self,
        *,
        end_date: str,
        start_date: str | None = None,
        accounting_method: str | None = None,
    ) -> dict[str, Any]:
        """Fetch TrialBalance report.

        This is often a better source than BalanceSheet for account-level lines.
        """

        tokens = self.load_tokens()
        base = self._base_url(tokens.environment)
        url = f"{base}/v3/company/{tokens.realm_id}/reports/TrialBalance"

        params: dict[str, str] = {"end_date": end_date}
        if start_date:
            params["start_date"] = start_date
        if accounting_method:
            params["accounting_method"] = accounting_method

        try:
            return self._request_json(
                "GET",
                url,
                bearer_token=tokens.access_token,
                params=params,
            )
        except RuntimeError as e:
            if "401" in str(e) or "invalid_token" in str(e).lower():
                tokens = self.refresh_tokens(tokens)
                return self._request_json(
                    "GET",
                    url,
                    bearer_token=tokens.access_token,
                    params=params,
                )
            raise
