"""Test configuration for MCP server tests."""

import pytest


@pytest.fixture
def mcp_factory():
    """Factory fixture for tests."""
    from src.mcp_server.core.factory import MCPToolFactory

    return MCPToolFactory()


@pytest.fixture
def hr_service():
    """HR service fixture."""
    from src.mcp_server.services.hr_service import HRService

    return HRService()


@pytest.fixture
def tech_support_service():
    """Tech support service fixture."""
    from src.mcp_server.services.tech_support_service import TechSupportService

    return TechSupportService()


@pytest.fixture
def general_service():
    """General service fixture."""
    from src.mcp_server.services.general_service import GeneralService

    return GeneralService()


@pytest.fixture
def mock_mcp_server():
    """Mock MCP server for testing."""

    class MockMCP:
        def __init__(self):
            self.tools = []

        def tool(self, tags=None):
            def decorator(func):
                self.tools.append({"func": func, "tags": tags or []})
                return func

            return decorator

    return MockMCP()
