"""MER review MCP tools.

This service exposes tools that call the backend FastAPI endpoints
responsible for executing deterministic MER balance sheet checks and
editing Google Sheets.

Design goals:
- Keep tools read-only for reviews, but allow editing for results
- Keep the agent surface simple: pass end_date + optional overrides
"""

from __future__ import annotations

from typing import Any, List

from ..core.factory import Domain, MCPToolBase
from .mer_review_backend_client import call_mer_balance_sheet_review_backend


class MERReviewService(MCPToolBase):
    """Finance/MER review tools."""

    def __init__(self):
        super().__init__(Domain.FINANCE)

    def register_tools(self, mcp) -> None:
        @mcp.tool(tags={self.domain.value})
        async def mer_balance_sheet_review(
            end_date: str,
            mer_sheet: str | None = None,
            mer_range: str | None = None,
            mer_month_header: str | None = None,
            mer_bank_row_key: str | None = None,
            qbo_bank_label_substring: str | None = None,
            rulebook_path: str | None = None,
            backend_base_url: str | None = None,
        ) -> dict[str, Any]:
            """Run MER Balance Sheet review (QBO + Google Sheets) via backend.

            Args:
              end_date: Period end date in YYYY-MM-DD.
              mer_sheet: Google Sheet tab name (defaults to "Balance Sheet" if present).
              mer_range: A1 range to fetch (defaults to full sheet range).
              mer_month_header: Explicit MER month header to use (optional).
              mer_bank_row_key: Required only for bank-match rules.
              qbo_bank_label_substring: Required only for bank-match rules.
              rulebook_path: Optional path override (relative to repo root or absolute).
              backend_base_url: Optional override; defaults to env MER_REVIEW_BACKEND_BASE_URL
                               or http://127.0.0.1:8000/api/v4

            Returns:
              JSON payload from the backend endpoint.
            """

            return await call_mer_balance_sheet_review_backend(
                end_date=end_date,
                mer_sheet=mer_sheet,
                mer_range=mer_range,
                mer_month_header=mer_month_header,
                mer_bank_row_key=mer_bank_row_key,
                qbo_bank_label_substring=qbo_bank_label_substring,
                rulebook_path=rulebook_path,
                backend_base_url=backend_base_url,
            )

        @mcp.tool(tags={self.domain.value})
        async def update_google_sheet_cell(
            spreadsheet_id: str,
            sheet_name: str,
            cell_range: str,
            value: str,
            backend_base_url: str | None = None,
        ) -> dict[str, Any]:
            """Update a single cell in Google Sheets.

            Args:
              spreadsheet_id: The Google Sheets spreadsheet ID.
              sheet_name: Name of the sheet tab.
              cell_range: A1 notation cell reference (e.g., 'A1', 'B5').
              value: The value to write to the cell.
              backend_base_url: Optional override for backend URL.

            Returns:
              Success confirmation with updated cell info.
            """
            # This would need a backend endpoint to be implemented
            # For now, return a placeholder response
            return {
                "status": "Google Sheets editing not yet implemented",
                "message": f"Would update {sheet_name}!{cell_range} with value: {value}",
                "spreadsheet_id": spreadsheet_id,
                "note": "Contact developer to implement Google Sheets write functionality"
            }

        @mcp.tool(tags={self.domain.value})
        async def update_google_sheet_range(
            spreadsheet_id: str,
            sheet_name: str,
            range_notation: str,
            values: List[List[str]],
            backend_base_url: str | None = None,
        ) -> dict[str, Any]:
            """Update a range of cells in Google Sheets.

            Args:
              spreadsheet_id: The Google Sheets spreadsheet ID.
              sheet_name: Name of the sheet tab.
              range_notation: A1 notation range (e.g., 'A1:B10').
              values: 2D array of values to write.
              backend_base_url: Optional override for backend URL.

            Returns:
              Success confirmation with updated range info.
            """
            # This would need a backend endpoint to be implemented
            return {
                "status": "Google Sheets range editing not yet implemented",
                "message": f"Would update {sheet_name}!{range_notation} with {len(values)} rows of data",
                "spreadsheet_id": spreadsheet_id,
                "note": "Contact developer to implement Google Sheets write functionality"
            }

    @property
    def tool_count(self) -> int:
        return 3
