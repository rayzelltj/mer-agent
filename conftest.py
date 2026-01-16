"""Pytest configuration.

Ensures local packages can be imported consistently during test collection.
"""

import sys
from pathlib import Path

import pytest


def _prepend_sys_path(path: Path) -> None:
    resolved = str(path.resolve())
    if resolved not in sys.path:
        sys.path.insert(0, resolved)


REPO_ROOT = Path(__file__).resolve().parent

# Allow importing `common`, `v4`, etc. as top-level modules (used throughout backend code).
_prepend_sys_path(REPO_ROOT / "src" / "backend")

# Allow importing `src.*` package explicitly.
_prepend_sys_path(REPO_ROOT)

# Back-compat: some tests import agent modules directly.
_prepend_sys_path(REPO_ROOT / "src" / "backend" / "v4" / "magentic_agents")

@pytest.fixture
def agent_env_vars():
    """Common environment variables for agent testing."""
    return {
        "BING_CONNECTION_NAME": "test_bing_connection",
        "MCP_SERVER_ENDPOINT": "http://test-mcp-server",
        "MCP_SERVER_NAME": "test_mcp_server", 
        "MCP_SERVER_DESCRIPTION": "Test MCP server",
        "TENANT_ID": "test_tenant_id",
        "CLIENT_ID": "test_client_id",
        "AZURE_OPENAI_ENDPOINT": "https://test.openai.azure.com/",
        "AZURE_OPENAI_API_KEY": "test_key",
        "AZURE_OPENAI_DEPLOYMENT_NAME": "test_deployment"
    }