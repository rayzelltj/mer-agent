"""Service abstractions for v4.

Exports:
- BaseAPIService: minimal async HTTP wrapper using endpoints from AppConfig
- MCPService: service targeting a local/remote MCP server
- FoundryService: helper around Azure AI Foundry (AIProjectClient)
"""

from src.backend.v4.common.services.agents_service import AgentsService
from src.backend.v4.common.services.base_api_service import BaseAPIService
from src.backend.v4.common.services.foundry_service import FoundryService
from src.backend.v4.common.services.mcp_service import MCPService

__all__ = [
    "BaseAPIService",
    "MCPService",
    "FoundryService",
    "AgentsService",
]
