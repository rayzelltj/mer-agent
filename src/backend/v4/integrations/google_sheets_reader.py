"""Google Sheets reader (service-account based).

Goals
- Provide a small, testable integration wrapper around the Sheets API.
- Keep all network calls here; keep parsing/lookup deterministic and unit-testable.

This intentionally does not depend on FastAPI or the agent framework.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

from dotenv import load_dotenv

load_dotenv()

# Convenience: allow local runs with only `.env.example` filled.
if not os.environ.get("SPREADSHEET_ID"):
    load_dotenv(dotenv_path=os.path.abspath(".env.example"), override=False)


def _norm(s: str | None) -> str:
    return re.sub(r"[^\w]", "", s or "").lower()


def _col_to_a1(col_index_zero_based: int) -> str:
    """Convert 0-based column index to A1 column letters (0->A, 25->Z, 26->AA)."""

    if col_index_zero_based < 0:
        raise ValueError("col_index_zero_based must be >= 0")

    result = ""
    n = col_index_zero_based
    while True:
        n, rem = divmod(n, 26)
        result = chr(ord("A") + rem) + result
        if n == 0:
            break
        n -= 1

    return result


@dataclass(frozen=True, slots=True)
class SheetLookupResult:
    value: str | None
    header_row_index: int | None
    row_index: int | None
    col_index: int | None
    matched_row_key_cell: str | None
    matched_col_header: str | None

    @property
    def a1_cell(self) -> str | None:
        if self.row_index is None or self.col_index is None:
            return None
        return f"{_col_to_a1(self.col_index)}{self.row_index + 1}"


@dataclass(frozen=True, slots=True)
class SheetRowMatch:
    row_index: int
    col_index: int
    row_text: str
    value: str | None

    @property
    def a1_cell(self) -> str:
        return f"{_col_to_a1(self.col_index)}{self.row_index + 1}"


def find_values_for_rows_containing(
    *,
    rows: list[list[str]],
    row_substring: str,
    col_header: str,
    header_row_index: int | None = None,
    header_search_rows: int = 10,
) -> list[SheetRowMatch]:
    """Return all rows whose text contains `row_substring`, for the given column.

    This is used for checks like:
    - "any row containing 'clearing account'"
    - "the 'undeposited' row"

    Column selection uses the same fuzzy-header matching as `find_value_in_table`.
    """

    # Determine header row
    detected_header_row_index: int | None = header_row_index
    if detected_header_row_index is None:
        for i, r in enumerate(rows[: max(header_search_rows, 1)]):
            if any((c or "").strip() for c in r):
                detected_header_row_index = i
                break
    if detected_header_row_index is None:
        return []

    header = rows[detected_header_row_index]
    col_index: int | None = None
    needle = _norm(col_header)
    for j, h in enumerate(header):
        if needle and needle in _norm(h):
            col_index = j
            break
    if col_index is None:
        return []

    row_needle = _norm(row_substring)
    out: list[SheetRowMatch] = []
    for i, r in enumerate(rows):
        row_text = " ".join([c for c in r if c]).strip()
        if row_needle and row_needle in _norm(row_text):
            value = r[col_index] if col_index < len(r) else None
            out.append(
                SheetRowMatch(
                    row_index=i,
                    col_index=col_index,
                    row_text=row_text,
                    value=value,
                )
            )

    return out


def find_value_in_table(
    *,
    rows: list[list[str]],
    row_key: str,
    col_header: str,
    header_row_index: int | None = None,
    header_search_rows: int = 10,
) -> SheetLookupResult:
    """Find a value by fuzzy row-key match and fuzzy column-header match.

    Matching strategy (same as the proven local script):
    - Header row: first non-empty row within the first `header_search_rows`.
    - Column: first header cell whose normalized text contains normalized `col_header`.
    - Row: first cell anywhere in the table whose normalized text contains normalized `row_key`.
    """

    detected_header_row_index: int | None = header_row_index
    if detected_header_row_index is None:
        for i, r in enumerate(rows[: max(header_search_rows, 1)]):
            if any((c or "").strip() for c in r):
                detected_header_row_index = i
                break

    if detected_header_row_index is None:
        return SheetLookupResult(
            value=None,
            header_row_index=None,
            row_index=None,
            col_index=None,
            matched_row_key_cell=None,
            matched_col_header=None,
        )

    header = rows[detected_header_row_index]
    col_index: int | None = None
    needle = _norm(col_header)
    for j, h in enumerate(header):
        if needle and needle in _norm(h):
            col_index = j
            break

    row_index: int | None = None
    matched_row_key_cell: str | None = None
    row_needle = _norm(row_key)
    for i, r in enumerate(rows):
        # Match across the whole row too (e.g. label split across cells).
        if row_needle and row_needle in _norm(" ".join(r)):
            row_index = i
            matched_row_key_cell = " ".join([c for c in r if c]).strip() or None
            break
        for cell in r:
            if row_needle and row_needle in _norm(cell):
                row_index = i
                matched_row_key_cell = cell
                break
        if row_index is not None:
            break

    matched_col_header = header[col_index] if col_index is not None and col_index < len(header) else None

    if row_index is None or col_index is None:
        return SheetLookupResult(
            value=None,
            header_row_index=detected_header_row_index,
            row_index=row_index,
            col_index=col_index,
            matched_row_key_cell=matched_row_key_cell,
            matched_col_header=matched_col_header,
        )

    row = rows[row_index]
    value = row[col_index] if col_index < len(row) else None

    return SheetLookupResult(
        value=value,
        header_row_index=detected_header_row_index,
        row_index=row_index,
        col_index=col_index,
        matched_row_key_cell=matched_row_key_cell,
        matched_col_header=matched_col_header,
    )


class GoogleSheetsReader:
    def __init__(
        self,
        *,
        spreadsheet_id: str,
        service_account_path: str,
        timeout_seconds: int = 30,
    ) -> None:
        self._spreadsheet_id = spreadsheet_id
        self._service_account_path = os.path.expanduser(service_account_path)
        self._timeout_seconds = timeout_seconds

    @classmethod
    def from_env(cls) -> "GoogleSheetsReader":
        spreadsheet_id = os.environ.get("SPREADSHEET_ID")
        if not spreadsheet_id:
            raise ValueError("Missing SPREADSHEET_ID")

        service_account_path = os.environ.get("GOOGLE_SA_FILE") or os.path.expanduser(
            "~/Desktop/service-account.json"
        )
        timeout_seconds = int(os.environ.get("GOOGLE_HTTP_TIMEOUT_SECONDS", "30"))

        return cls(
            spreadsheet_id=spreadsheet_id,
            service_account_path=service_account_path,
            timeout_seconds=timeout_seconds,
        )

    def _build_sheets_service(self, *, readonly: bool = True) -> Any:
        # Lazy import so unit tests that only use the deterministic helpers
        # do not require Google client libs.
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        if not os.path.exists(self._service_account_path):
            raise FileNotFoundError(
                f"Service account file not found: {self._service_account_path}"
            )

        sa = json.load(open(self._service_account_path, "r", encoding="utf-8"))
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets.readonly"
            if readonly
            else "https://www.googleapis.com/auth/spreadsheets"
        ]
        creds = service_account.Credentials.from_service_account_info(
            sa,
            scopes=scopes,
        )

        return build(
            "sheets",
            "v4",
            credentials=creds,
            cache_discovery=False,
        )

    @property
    def spreadsheet_id(self) -> str:
        return self._spreadsheet_id

    def list_sheet_titles(self) -> list[str]:
        sheets = self._build_sheets_service(readonly=True)
        meta = (
            sheets.spreadsheets()
            .get(spreadsheetId=self._spreadsheet_id, fields="sheets(properties(title))")
            .execute(num_retries=2)
        )
        return [s["properties"]["title"] for s in meta.get("sheets", [])]

    def fetch_rows(self, *, a1_range: str) -> list[list[str]]:
        sheets = self._build_sheets_service(readonly=True)
        resp = (
            sheets.spreadsheets()
            .values()
            .get(spreadsheetId=self._spreadsheet_id, range=a1_range)
            .execute(num_retries=2)
        )
        rows = resp.get("values", [])
        return rows if isinstance(rows, list) else []

    def batch_update_values(self, *, updates: dict[str, str]) -> dict[str, Any]:
        """Update many individual cells with a single API call.

        `updates` is an A1-range -> value mapping, e.g.:
        {
          "'Balance Sheet'!AA12": "PASS",
          "'Balance Sheet'!AA13": "FAIL: ...",
        }

        Safety: writing is disabled unless GOOGLE_SHEETS_ALLOW_WRITE=1.
        """

        if os.environ.get("GOOGLE_SHEETS_ALLOW_WRITE", "").strip() != "1":
            raise PermissionError(
                "Google Sheets write disabled. Set GOOGLE_SHEETS_ALLOW_WRITE=1 to enable updates."
            )

        if not updates:
            return {"updated": 0}

        sheets = self._build_sheets_service(readonly=False)

        data = [
            {"range": a1_range, "values": [[value]]}
            for a1_range, value in updates.items()
            if a1_range and value is not None
        ]
        body: dict[str, Any] = {"valueInputOption": "USER_ENTERED", "data": data}

        resp = (
            sheets.spreadsheets()
            .values()
            .batchUpdate(spreadsheetId=self._spreadsheet_id, body=body)
            .execute(num_retries=2)
        )
        return resp
