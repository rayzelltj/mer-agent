#!/usr/bin/env python3
"""
Demo script showing how to use MER reviews through the frontend.

This demonstrates the complete workflow:
1. Backend running with MER team loaded
2. Frontend displaying MER review options
3. User selecting a prompt
4. Agent executing MER review via MCP tools
5. Results displayed in chat interface
"""

import time
import requests
from typing import Dict, Any

def check_backend_health() -> bool:
    """Check if backend is running and healthy."""
    try:
        response = requests.get("http://localhost:8000/healthz", timeout=5)
        return response.status_code == 200
    except:
        return False

def check_frontend_config() -> bool:
    """Check if frontend is running."""
    try:
        response = requests.get("http://localhost:3001/config", timeout=5)
        return response.status_code == 200
    except:
        return False

def demonstrate_mer_workflow():
    """Demonstrate the complete MER review workflow."""

    print("🔍 MER Review Frontend Integration Demo")
    print("=" * 50)

    # Check backend
    print("1. Checking backend status...")
    if check_backend_health():
        print("   ✅ Backend is running on http://localhost:8000")
    else:
        print("   ❌ Backend not accessible. Start with: uvicorn src.backend.app:app --reload")
        return

    # Check frontend
    print("2. Checking frontend status...")
    if check_frontend_config():
        print("   ✅ Frontend is running on http://localhost:3001")
    else:
        print("   ❌ Frontend not accessible. Start with: cd src/frontend && npm start")
        return

    print("\n📋 Available MER Review Options:")
    print("   • 'MER Balance Sheet Review - Blackbird Fabrics' (pre-configured)")
    print("   • 'Custom MER Review' (asks for company/date)")
    print("   • Or type your own prompt in the input field")

    print("\n💡 How to Use:")
    print("   1. Open http://localhost:3000 in your browser")
    print("   2. Select the MER Review team from the team dropdown")
    print("   3. Click on a quick task or type your own MER review request")
    print("   4. The agent will:")
    print("      - Extract company name and date from your prompt")
    print("      - Call the mer_balance_sheet_review MCP tool")
    print("      - Analyze results against the YAML rulebook")
    print("      - Return structured pass/fail summary")

    print("\n🔧 Example Prompts:")
    print("   • 'Review Blackbird Fabrics balance sheet for November 20, 2025'")
    print("   • 'Run MER review for XYZ Corp as of 2025-11-20'")
    print("   • 'Check financials for ABC Company period ending 11/20/2025'")

    print("\n⚠️  Current Limitations:")
    print("   • Google Sheets editing tools are placeholders (need backend implementation)")
    print("   • QBO integration requires proper authentication setup")
    print("   • MCP server must be running for tool access")

    print("\n🚀 Ready to start MER reviews through the frontend!")

if __name__ == "__main__":
    demonstrate_mer_workflow()