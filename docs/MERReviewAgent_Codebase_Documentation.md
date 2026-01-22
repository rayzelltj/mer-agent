# MER Review Agent Codebase Documentation

This document is code-referenced and limited to what is observable in this repository; if something is unclear, it is explicitly labeled “Unknown from code alone.” (src/backend, src/frontend, src/mcp_server, data)

## 1. High-Level Architecture

**Text Diagram**

```
[Browser UI (React/Vite)] 
  -> REST: /api/v4/* (FastAPI)
  -> WS:  /api/v4/socket/{process_id}
        |
        v
[Backend Orchestration + MER APIs]
  -> Azure AI Foundry Agents (AzureAIAgentClient)
  -> MCP tool gateway (HostedMCPTool -> MCP server)
  -> MER deterministic checks (QBO + Google Sheets)
        |
        v
[MCP Server (FastMCP) tools]
  -> Backend MER review endpoint (HTTP)
  -> Local/placeholder tools
```
(src/frontend/src/api/apiClient.tsx, src/frontend/src/services/WebSocketService.tsx, src/backend/app.py, src/backend/v4/api/router.py, src/backend/v4/orchestration/orchestration_manager.py, src/backend/v4/magentic_agents/common/lifecycle.py, src/mcp_server/mcp_server.py, src/backend/v4/api/mer_router.py)

**Major Components + Communication Paths**

- Frontend is a React/Vite app that calls REST endpoints through `apiClient` and uses WebSockets for streaming updates. (src/frontend/src/api/apiClient.tsx, src/frontend/src/services/WebSocketService.tsx, src/frontend/package.json)
- Backend is a FastAPI app that mounts `/api/v4/*` routes and coordinates orchestration, MER checks, and WebSocket messaging. (src/backend/app.py, src/backend/v4/api/router.py, src/backend/v4/api/plan_router.py, src/backend/v4/api/mer_router.py, src/backend/v4/api/ws_router.py)
- MCP server is a FastMCP process registering multiple tool services and exposes them over HTTP/streamable HTTP. (src/mcp_server/mcp_server.py, src/mcp_server/core/factory.py)
- LLM/agent execution uses `agent_framework` and Azure AI Foundry clients; orchestration is handled by `HumanApprovalMagenticManager` with a Magentic workflow. (src/backend/v4/orchestration/orchestration_manager.py, src/backend/v4/orchestration/human_approval_manager.py)
- Azure services explicitly referenced in code: Azure AI Foundry (AIProjectClient + AzureAIAgentClient), Azure OpenAI (AzureOpenAIChatClient), Azure Cosmos DB, and Application Insights. (src/backend/common/config/app_config.py, src/backend/v4/orchestration/orchestration_manager.py, src/backend/v4/config/settings.py, src/backend/common/database/cosmosdb.py, src/backend/app.py)

**Backend vs Frontend vs MCP vs LLM vs Azure**

- Backend: FastAPI app, orchestration, MER checks, and integrations. (src/backend/app.py, src/backend/v4/api/mer_router.py, src/backend/v4/orchestration/orchestration_manager.py)
- Frontend: React/Vite UI with REST + WebSocket clients. (src/frontend/src/components/content/HomeInput.tsx, src/frontend/src/api/apiClient.tsx, src/frontend/src/services/WebSocketService.tsx)
- MCP: FastMCP server with tool registration across domains, including MER review tools. (src/mcp_server/mcp_server.py, src/mcp_server/services/mer_review_service.py)
- LLM: Azure AI Foundry agents via `AzureAIAgentClient`, `ChatAgent`, and Magentic workflow. (src/backend/v4/orchestration/orchestration_manager.py, src/backend/v4/magentic_agents/foundry_agent.py)
- Azure: AI Foundry project client and agents, Cosmos DB storage, Application Insights telemetry. (src/backend/common/config/app_config.py, src/backend/common/database/cosmosdb.py, src/backend/app.py)

## 2. Execution Flow (Critical)

**User Action → Backend API → Agent → Tools → Output**

1. User submits a task in the UI (home input), which calls `TaskService.createPlan` and then `APIService.createPlan` to `POST /api/v4/process_request`. (src/frontend/src/components/content/HomeInput.tsx, src/frontend/src/services/TaskService.tsx, src/frontend/src/api/apiService.tsx)
2. The backend `process_request` endpoint validates the user, loads the current team, runs RAI validation, creates a `Plan`, and schedules orchestration as a background task. (src/backend/v4/api/plan_router.py)
3. Orchestration is executed by `OrchestrationManager.run_orchestration`, which streams agent events to the WebSocket connection. (src/backend/v4/orchestration/orchestration_manager.py)
4. Agent instances are created from team configuration by `MagenticAgentFactory.get_agents`, which instantiates `FoundryAgentTemplate` or `ProxyAgent`. (src/backend/v4/magentic_agents/magentic_agent_factory.py, src/backend/v4/magentic_agents/foundry_agent.py, src/backend/v4/magentic_agents/proxy_agent.py)
5. The orchestrator (`HumanApprovalMagenticManager`) produces a plan, sends a plan approval request to the client, and waits for user approval before proceeding. (src/backend/v4/orchestration/human_approval_manager.py)
6. Agents stream responses; `response_handlers.streaming_agent_response_callback` and `agent_response_callback` dispatch WebSocket messages. (src/backend/v4/callbacks/response_handlers.py)
7. If MCP tools are enabled for an agent, `FoundryAgentTemplate` attaches a `HostedMCPTool`/`MCPStreamableHTTPTool`, enabling tool invocation against the MCP server. (src/backend/v4/magentic_agents/foundry_agent.py, src/backend/v4/magentic_agents/common/lifecycle.py)
8. MCP MER tool calls hit the backend MER review endpoint, which pulls QBO and Google Sheets data and evaluates the rulebook via `MERBalanceSheetRuleEngine`. (src/mcp_server/services/mer_review_service.py, src/mcp_server/services/mer_review_backend_client.py, src/backend/v4/api/mer_router.py, src/backend/v4/use_cases/mer_rule_engine.py, src/backend/v4/integrations/qbo_client.py, src/backend/v4/integrations/google_sheets_reader.py)
9. Final output is sent to the frontend via WebSocket `FINAL_RESULT_MESSAGE`. (src/backend/v4/orchestration/orchestration_manager.py, src/backend/v4/models/messages.py, src/frontend/src/services/WebSocketService.tsx)

**WebSocket Initialization**

- Frontend creates a WebSocket to `/api/v4/socket/{process_id}?user_id=...` to receive streaming updates. (src/frontend/src/services/WebSocketService.tsx)
- Backend accepts the WebSocket, registers it by `process_id` + `user_id`, and uses `ConnectionConfig.send_status_update_async` for streaming messages. (src/backend/v4/api/ws_router.py, src/backend/v4/config/settings.py)

## 3. Agent System

**Agents Are Defined via Team Configurations**

- Runtime agents are created from `TeamConfiguration.agents` in the DB or in JSON team configuration files; the sample MER team config is `data/agent_teams/mer_review.json`. (data/agent_teams/mer_review.json, src/backend/common/models/messages_af.py, src/backend/v4/magentic_agents/magentic_agent_factory.py)

### Agent: MERReviewOrchestrator

- **Purpose**: MER review orchestration and use of MCP tools, configured by team JSON. (data/agent_teams/mer_review.json)
- **System prompt**: `agents[].system_message` in `data/agent_teams/mer_review.json` (the prompt text is stored inline). (data/agent_teams/mer_review.json)
- **Allowed tools**: MCP tool access is enabled by `use_mcp: true` and `FoundryAgentTemplate` attaches MCP tools when configured. (data/agent_teams/mer_review.json, src/backend/v4/magentic_agents/foundry_agent.py, src/backend/v4/magentic_agents/common/lifecycle.py)
- **Termination conditions**: No explicit termination conditions in the agent class; depends on workflow completion. (src/backend/v4/magentic_agents/foundry_agent.py, src/backend/v4/orchestration/orchestration_manager.py)
- **Instantiation**: Created by `MagenticAgentFactory.create_agent_from_config` when iterating through `TeamConfiguration.agents`. (src/backend/v4/magentic_agents/magentic_agent_factory.py)

### Agent: ProxyAgent

- **Purpose**: Human clarification proxy that requests user input via WebSocket and returns the answer to the workflow. (src/backend/v4/magentic_agents/proxy_agent.py)
- **System prompt**: `agents[].system_message` in `data/agent_teams/mer_review.json` (this text is read but ProxyAgent logic is defined in code). (data/agent_teams/mer_review.json, src/backend/v4/magentic_agents/proxy_agent.py)
- **Allowed tools**: None; it only emits WebSocket messages and waits for responses. (src/backend/v4/magentic_agents/proxy_agent.py)
- **Termination conditions**: Returns when user response arrives or when clarification timeout occurs (default timeout from `orchestration_config`). (src/backend/v4/magentic_agents/proxy_agent.py, src/backend/v4/config/settings.py)
- **Instantiation**: Created in `MagenticAgentFactory.create_agent_from_config` when the agent name is `ProxyAgent`. (src/backend/v4/magentic_agents/magentic_agent_factory.py)

### Orchestrator: HumanApprovalMagenticManager

- **Purpose**: Generates plan steps, sends plan approval requests, and manages approval gating for workflow execution. (src/backend/v4/orchestration/human_approval_manager.py)
- **System prompt**: Custom plan and final-answer prompts are appended in `HumanApprovalMagenticManager.__init__` (plan append + final append). (src/backend/v4/orchestration/human_approval_manager.py)
- **Allowed tools**: Not directly tool-enabled; it manages workflow coordination. (src/backend/v4/orchestration/human_approval_manager.py)
- **Termination conditions**: Stops when approval is rejected or when `max_rounds` is exceeded; sends final result on max round termination. (src/backend/v4/orchestration/human_approval_manager.py, src/backend/v4/config/settings.py)
- **Instantiation**: Created in `OrchestrationManager.init_orchestration`. (src/backend/v4/orchestration/orchestration_manager.py)

## 4. Tooling (MCP)

**MCP Server Registration**

- MCP server registers HR, Tech Support, Marketing, Product, and MER Review services. (src/mcp_server/mcp_server.py)
- `GeneralService` and `DataToolService` exist but are not registered in `mcp_server.py`, so they are not exposed by default. (src/mcp_server/services/general_service.py, src/mcp_server/services/data_tool_service.py, src/mcp_server/mcp_server.py)

**Registered Tools (with input schema, external calls, and return behavior)**

- `mer_balance_sheet_review(end_date, mer_sheet?, mer_range?, mer_month_header?, mer_bank_row_key?, qbo_bank_label_substring?, rulebook_path?, backend_base_url?)`
  - **File**: `src/mcp_server/services/mer_review_service.py`
  - **External system**: Calls backend HTTP endpoint `/api/v4/mer/review/balance_sheet` via `httpx` (external system is backend; backend calls QBO + Google Sheets). (src/mcp_server/services/mer_review_backend_client.py, src/backend/v4/api/mer_router.py, src/backend/v4/integrations/qbo_client.py, src/backend/v4/integrations/google_sheets_reader.py)
  - **Return**: JSON payload from backend or error object with request context. (src/mcp_server/services/mer_review_backend_client.py)

- `update_google_sheet_cell(spreadsheet_id, sheet_name, cell_range, value, backend_base_url?)`
  - **File**: `src/mcp_server/services/mer_review_service.py`
  - **External system**: None (placeholder response only). (src/mcp_server/services/mer_review_service.py)
  - **Return**: Placeholder status message indicating not implemented. (src/mcp_server/services/mer_review_service.py)

- `update_google_sheet_range(spreadsheet_id, sheet_name, range_notation, values, backend_base_url?)`
  - **File**: `src/mcp_server/services/mer_review_service.py`
  - **External system**: None (placeholder response only). (src/mcp_server/services/mer_review_service.py)
  - **Return**: Placeholder status message indicating not implemented. (src/mcp_server/services/mer_review_service.py)

- `schedule_orientation_session(employee_name, date)`  
- `assign_mentor(employee_name, mentor_name?)`  
- `register_for_benefits(employee_name, benefits_package?)`  
- `provide_employee_handbook(employee_name)`  
- `initiate_background_check(employee_name, check_type?)`  
- `request_id_card(employee_name, department?)`  
- `set_up_payroll(employee_name, salary?)`  
  - **File**: `src/mcp_server/services/hr_service.py`
  - **External system**: None; returns formatted success/error strings. (src/mcp_server/services/hr_service.py, src/mcp_server/utils/formatters.py)

- `send_welcome_email(employee_name, email_address)`  
- `set_up_office_365_account(employee_name, email_address, department?)`  
- `configure_laptop(employee_name, laptop_model, operating_system?)`  
- `setup_vpn_access(employee_name, access_level?)`  
- `create_system_accounts(employee_name, systems?)`  
  - **File**: `src/mcp_server/services/tech_support_service.py`
  - **External system**: None; returns formatted success/error strings. (src/mcp_server/services/tech_support_service.py, src/mcp_server/utils/formatters.py)

- `generate_press_release(key_information_for_press_release)`  
- `handle_influencer_collaboration(influencer_name, campaign_name)`  
  - **File**: `src/mcp_server/services/marketing_service.py`
  - **External system**: None; returns constructed strings. (src/mcp_server/services/marketing_service.py)

- `get_product_info()`  
  - **File**: `src/mcp_server/services/product_service.py`
  - **External system**: None; returns static product info string. (src/mcp_server/services/product_service.py)

**Defined but Not Registered Tools**

- `greet_test(name)`, `get_server_status()` in `GeneralService` are not registered in `mcp_server.py`. (src/mcp_server/services/general_service.py, src/mcp_server/mcp_server.py)
- `data_provider(tablename)` and `show_tables()` in `DataToolService` are not registered in `mcp_server.py`. (src/mcp_server/services/data_tool_service.py, src/mcp_server/mcp_server.py)

## 5. State & Memory

- Persistent conversation artifacts (plans, steps, agent messages, team configs, and current team) are stored in Cosmos DB via `CosmosDBClient`. (src/backend/common/database/cosmosdb.py, src/backend/common/models/messages_af.py)
- The database factory uses a singleton `CosmosDBClient` configured via environment variables. (src/backend/common/database/database_factory.py, src/backend/common/config/app_config.py)
- In-memory orchestration state is stored in `OrchestrationConfig` (orchestrations, approvals, clarifications, and WebSocket mappings). (src/backend/v4/config/settings.py)
- Agent registry tracking is handled by `AgentRegistry` using a `WeakSet` for lifecycle management. (src/backend/v4/config/agent_registry.py)
- Agent IDs are persisted per team in the database via `CurrentTeamAgent` for reuse. (src/backend/v4/magentic_agents/common/lifecycle.py, src/backend/common/models/messages_af.py)
- Artifact storage beyond Cosmos DB (e.g., file artifacts) is **Unknown from code alone**. (src/backend/common/database/cosmosdb.py)

## 6. Configuration & Environments

**Backend Environment Variables**

- Required/optional Azure settings are loaded in `AppConfig` (e.g., `AZURE_OPENAI_ENDPOINT`, `AZURE_AI_PROJECT_ENDPOINT`, `COSMOSDB_*`, `APPLICATIONINSIGHTS_CONNECTION_STRING`). (src/backend/common/config/app_config.py)
- MCP settings for agents (`MCP_SERVER_ENDPOINT`, `MCP_SERVER_NAME`, `MCP_SERVER_DESCRIPTION`) are read from `AppConfig`. (src/backend/common/config/app_config.py, src/backend/v4/magentic_agents/models/agent_models.py)
- MER backend client uses `MER_REVIEW_BACKEND_BASE_URL` and `MER_REVIEW_HTTP_TIMEOUT_SECONDS`. (src/mcp_server/services/mer_review_backend_client.py)
- QBO integration requires `QBO_CLIENT_ID`, `QBO_CLIENT_SECRET`, `QBO_TOKENS_PATH`, `QBO_ENVIRONMENT`, `QBO_REDIRECT_URI`, and `QBO_HTTP_TIMEOUT_SECONDS`. (src/backend/v4/integrations/qbo_client.py)
- Google Sheets integration requires `SPREADSHEET_ID`, `GOOGLE_SA_FILE`, and `GOOGLE_HTTP_TIMEOUT_SECONDS`. (src/backend/v4/integrations/google_sheets_reader.py)

**Local vs azd up / Identity Differences**

- `AppConfig.get_azure_credential` uses `DefaultAzureCredential` when `APP_ENV == "dev"` and `ManagedIdentityCredential` otherwise. (src/backend/common/config/app_config.py)
- `AppConfig.get_ai_project_client` uses `AsyncDefaultAzureCredential` for dev and `AsyncManagedIdentityCredential` for non-dev. (src/backend/common/config/app_config.py)
- `azure.yaml` defines azd metadata and post-deploy scripts for team config/data setup. (azure.yaml)

**Azure Identity Assumptions**

- Managed Identity is assumed in non-dev environments when `APP_ENV != "dev"`. (src/backend/common/config/app_config.py)

## 7. How to Run

**Local Development**

- Backend: `src/backend/app.py` includes a `__main__` block running `uvicorn` on `127.0.0.1:8000`. (src/backend/app.py)
- MCP Server: `src/mcp_server/mcp_server.py` provides a CLI to run FastMCP with `--transport`, `--host`, and `--port`. (src/mcp_server/mcp_server.py)
- Frontend: `npm run dev` is configured in `src/frontend/package.json` using Vite. (src/frontend/package.json)

**azd up Deployment**

- `azure.yaml` defines azd metadata and postdeploy scripts; the actual `azd up` invocation and provisioning steps are not defined in code, so the full sequence is **Unknown from code alone**. (azure.yaml)

**Common Failure Points (from code paths)**

- Missing required env vars in `AppConfig` raise `ValueError` at startup. (src/backend/common/config/app_config.py)
- QBO token file missing causes `FileNotFoundError` during QBO reads. (src/backend/v4/integrations/qbo_client.py)
- Google Sheets `SPREADSHEET_ID` missing raises `ValueError`. (src/backend/v4/integrations/google_sheets_reader.py)
- MER rulebook path invalid returns `HTTP 400` from the MER API. (src/backend/v4/api/mer_router.py)

## 8. How to Extend the System

**Add a New Agent**

- Add a new agent entry to the team configuration JSON or database `TeamConfiguration.agents`; fields include `name`, `deployment_name`, `system_message`, `use_mcp`, `use_rag`, and `coding_tools`. (src/backend/common/models/messages_af.py, data/agent_teams/mer_review.json)
- Agents are instantiated through `MagenticAgentFactory.create_agent_from_config`, which chooses `FoundryAgentTemplate` or `ProxyAgent`. (src/backend/v4/magentic_agents/magentic_agent_factory.py)
- System prompts live in `TeamConfiguration.agents[].system_message`. (src/backend/common/models/messages_af.py)

**Add a New Tool**

- Implement a new tool function inside a service class in `src/mcp_server/services` and register it in `register_tools`. (src/mcp_server/services/mer_review_service.py, src/mcp_server/services/hr_service.py)
- Register the service in `src/mcp_server/mcp_server.py` via `factory.register_service(...)`. (src/mcp_server/mcp_server.py)
- Ensure agents have `use_mcp: true` to expose the MCP tool to `FoundryAgentTemplate`. (data/agent_teams/mer_review.json, src/backend/v4/magentic_agents/foundry_agent.py)

**How Agents Communicate**

- Agents participate in a Magentic workflow built by `MagenticBuilder().participants(...)`; communication is mediated by the orchestrator (`HumanApprovalMagenticManager`) and the workflow engine. (src/backend/v4/orchestration/orchestration_manager.py)
- Direct agent-to-agent communication details beyond the Magentic workflow are **Unknown from code alone**. (src/backend/v4/orchestration/orchestration_manager.py)

## 9. Testing & Debugging

**Test Agents Without Frontend**

- `scripts/mer_llm_agent_local.py` runs MER review logic locally and supports tool-only or LLM flows. (scripts/mer_llm_agent_local.py)
- `scripts/mer_llm_agent_terminal.py` and `scripts/mer_review_smoke_test.py` provide local CLI/smoke workflows. (scripts/mer_llm_agent_terminal.py, scripts/mer_review_smoke_test.py)

**Log Tool Calls**

- Tool calls are detected and emitted as `AGENT_TOOL_MESSAGE` by `_extract_tool_calls_from_contents` in response handlers. (src/backend/v4/callbacks/response_handlers.py, src/backend/v4/models/messages.py)

**Debug Failed Agent Runs**

- Orchestration logs detailed errors and sends error final results via WebSocket. (src/backend/v4/orchestration/orchestration_manager.py, src/backend/v4/config/settings.py)
- QBO debug logging can be enabled with `QBO_DEBUG` (prints request URLs). (src/backend/v4/integrations/qbo_client.py)

**Unit/Integration Tests**

- Backend tests live under `src/backend/tests` and use FastAPI/pytest conventions. (src/backend/tests/test_app.py, src/backend/tests/models/test_messages.py)

## 10. Known Gaps / Tech Debt

- Google Sheets update tools in MCP (`update_google_sheet_cell`, `update_google_sheet_range`) return placeholder responses and do not call any backend write API. (src/mcp_server/services/mer_review_service.py)
- `AgentsService.instantiate_agents` is a placeholder and explicitly not implemented. (src/backend/v4/common/services/agents_service.py)
- `GeneralService` and `DataToolService` exist but are not registered in the MCP server, so their tools are not reachable. (src/mcp_server/services/general_service.py, src/mcp_server/services/data_tool_service.py, src/mcp_server/mcp_server.py)
- MER review API uses a hardcoded fallback Client Maintenance spreadsheet ID when one is not provided. (src/backend/v4/api/mer_router.py)

