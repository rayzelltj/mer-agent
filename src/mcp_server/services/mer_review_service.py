"""MER review MCP tools.

This service exposes a single tool that calls the backend FastAPI endpoint
responsible for executing deterministic MER balance sheet checks driven by the
YAML rulebook.

Design goals:
- Keep tools read-only and deterministic (no sheet edits).
- Keep the agent surface simple: pass end_date + optional overrides.
"""

from __future__ import annotations

from typing import Any

from core.factory import Domain, MCPToolBase
from services.mer_review_backend_client import call_mer_balance_sheet_review_backend


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

    @property
    def tool_count(self) -> int:
        return 1
