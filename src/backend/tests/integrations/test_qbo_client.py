from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.backend.v4.integrations.qbo_client import QBOAuthTokens, QBOClient


class _FakeResp:
    def __init__(self, status_code: int, payload: dict, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text or json.dumps(payload)

    def json(self):
        return self._payload


def test_get_company_info_refreshes_on_401(monkeypatch, tmp_path) -> None:
    tokens_path = tmp_path / "tokens.json"
    tokens_path.write_text(
        json.dumps(
            {
                "environment": "sandbox",
                "realm_id": "123",
                "access_token": "expired",
                "refresh_token": "refresh",
                "id_token": None,
                "saved_at_unix": 0,
            }
        )
    )

    client = QBOClient(
        client_id="cid",
        client_secret="secret",
        redirect_uri="http://localhost",
        environment="sandbox",
        tokens_path=str(tokens_path),
    )

    calls = {"n": 0}

    def fake_request(method, url, headers=None, params=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeResp(401, {"Fault": "invalid"}, text="invalid_token")
        return _FakeResp(200, {"CompanyInfo": {"CompanyName": "X", "Id": "1"}})

    monkeypatch.setattr("requests.request", fake_request)

    def fake_refresh_tokens(tokens: QBOAuthTokens) -> QBOAuthTokens:
        return QBOAuthTokens(
            environment=tokens.environment,
            realm_id=tokens.realm_id,
            access_token="fresh",
            refresh_token=tokens.refresh_token,
            id_token=tokens.id_token,
        )

    monkeypatch.setattr(client, "refresh_tokens", fake_refresh_tokens)

    resp = client.get_company_info()
    assert resp["CompanyInfo"]["CompanyName"] == "X"
    assert calls["n"] == 2


def test_get_balance_sheet_passes_end_date(monkeypatch, tmp_path) -> None:
    tokens_path = tmp_path / "tokens.json"
    tokens_path.write_text(
        json.dumps(
            {
                "environment": "sandbox",
                "realm_id": "123",
                "access_token": "ok",
                "refresh_token": "refresh",
                "id_token": None,
            }
        )
    )

    client = QBOClient(
        client_id="cid",
        client_secret="secret",
        redirect_uri="http://localhost",
        environment="sandbox",
        tokens_path=str(tokens_path),
    )

    seen = SimpleNamespace(params=None)

    def fake_request(method, url, headers=None, params=None, timeout=None):
        seen.params = params
        return _FakeResp(200, {"Rows": {"Row": []}})

    monkeypatch.setattr("requests.request", fake_request)

    client.get_balance_sheet(end_date="2025-11-30")
    assert seen.params == {"end_date": "2025-11-30"}


def test_get_accounts_uses_query_api(monkeypatch, tmp_path) -> None:
    tokens_path = tmp_path / "tokens.json"
    tokens_path.write_text(
        json.dumps(
            {
                "environment": "sandbox",
                "realm_id": "123",
                "access_token": "ok",
                "refresh_token": "refresh",
                "id_token": None,
            }
        )
    )

    client = QBOClient(
        client_id="cid",
        client_secret="secret",
        redirect_uri="http://localhost",
        environment="sandbox",
        tokens_path=str(tokens_path),
    )

    seen = SimpleNamespace(url=None, params=None)

    def fake_request(method, url, headers=None, params=None, timeout=None):
        seen.url = url
        seen.params = params
        return _FakeResp(200, {"QueryResponse": {"Account": []}})

    monkeypatch.setattr("requests.request", fake_request)

    accounts = client.get_accounts(max_results=123)
    assert accounts == []
    assert seen.url is not None and seen.url.endswith("/v3/company/123/query")
    assert seen.params is not None
    assert "query" in seen.params
    assert "select * from Account" in seen.params["query"]
    assert "MAXRESULTS 123" in seen.params["query"]
