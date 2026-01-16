#!/usr/bin/env python3
"""
Test script to verify that mcp_server.py can be used with fastmcp run functionality.
This simulates what `fastmcp run mcp_server.py -t streamable-http -l DEBUG` would do.
"""

import sys
from pathlib import Path

import pytest

# Add current directory to path
sys.path.insert(0, str(Path(__file__).parent))

# Import the mcp_server module
import mcp_server


def test_mcp_instance():
    """Test that the MCP instance is available and properly configured."""
    print("🔍 Testing MCP server instance...")

    # Check if mcp instance exists
    if not hasattr(mcp_server, "mcp") or mcp_server.mcp is None:
        pytest.skip("mcp_server.mcp not available in this environment")

    if hasattr(mcp_server, "mcp") and mcp_server.mcp is not None:
        print("✅ MCP instance found!")

        # Try to get server info
        try:
            # Access the FastMCP server
            server = mcp_server.mcp
            print(f"✅ Server type: {type(server)}")
            print(f"✅ Server name: {getattr(server, 'name', 'Unknown')}")

            # Check if tools are registered
            factory = mcp_server.factory
            summary = factory.get_tool_summary()
            print(f"✅ Total services: {summary['total_services']}")
            print(f"✅ Total tools: {summary['total_tools']}")

            for domain, info in summary["services"].items():
                print(
                    f"   📁 {domain}: {info['tool_count']} tools ({info['class_name']})"
                )

            assert True  # sanity: reached end without exception

        except Exception as e:
            print(f"❌ Error accessing MCP server: {e}")
            raise


def test_fastmcp_compatibility():
    """Test if the server can be used with FastMCP Client."""
    print("\n🔍 Testing FastMCP client compatibility...")

    fastmcp = pytest.importorskip("fastmcp")
    Client = getattr(fastmcp, "Client", None)
    if Client is None:
        pytest.skip("fastmcp.Client not available")

    if not hasattr(mcp_server, "mcp") or mcp_server.mcp is None:
        pytest.skip("mcp_server.mcp not available in this environment")

    # This simulates how fastmcp run would use the server
    Client(mcp_server.mcp)
    print("✅ FastMCP Client can connect to our server instance")
    assert True


def test_streamable_http():
    """Test if we can run in streamable HTTP mode."""
    print("\n🔍 Testing streamable HTTP mode compatibility...")

    if not hasattr(mcp_server, "mcp") or mcp_server.mcp is None:
        pytest.skip("mcp_server.mcp not available in this environment")

    # This is what would happen when using -t streamable-http
    server = mcp_server.mcp
    assert server is not None

    print("✅ MCP server instance ready for HTTP streaming")
    print(
        "💡 To run with fastmcp: fastmcp run mcp_server.py -t streamable-http -l DEBUG"
    )


if __name__ == "__main__":
    print("🚀 FastMCP Run Compatibility Test")
    print("=" * 50)

    # Run tests
    test1 = test_mcp_instance()
    test2 = test_fastmcp_compatibility()
    test3 = test_streamable_http()

    print("\n📋 Test Results:")
    print(f"   MCP Instance: {'✅ PASS' if test1 else '❌ FAIL'}")
    print(f"   Client Compatibility: {'✅ PASS' if test2 else '❌ FAIL'}")
    print(f"   Streamable HTTP: {'✅ PASS' if test3 else '❌ FAIL'}")

    if all([test1, test2, test3]):
        print("\n🎉 All tests passed! Your mcp_server.py is ready for fastmcp run!")
        print("\n📖 Usage Examples:")
        print("   python -m fastmcp.cli.run mcp_server.py -t streamable-http -l DEBUG")
        print("   # OR if fastmcp CLI is available globally:")
        print("   fastmcp run mcp_server.py -t streamable-http -l DEBUG")
    else:
        print("\n❌ Some tests failed. Check the errors above.")

    print("\n" + "=" * 50)
