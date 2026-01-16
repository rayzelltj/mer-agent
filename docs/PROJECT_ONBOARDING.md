# Project Onboarding (How This Repo Works)

This document is meant to be the “missing onboarding” for this repo:

- A mental model of what an “AI agent” is in this codebase (vs a normal script).
- How the three services (backend / frontend / MCP server) connect.
- How your MER review functionality fits into the larger framework.
- How to run things locally and how to test.

> If you’re feeling like you’ve been “coding by Copilot” without understanding the system: that’s normal for agentic apps. The trick is to separate **(A) deterministic code paths** from **(B) LLM orchestration**.

---

## 1) Big picture: the three running services

This repo is a multi-service app (see [docs/LocalDevelopmentSetup.md](LocalDevelopmentSetup.md)):

1. **Backend** (FastAPI, port 8000)
   - Owns: API endpoints, “agent orchestration”, persistence (Cosmos-like store), authentication hooks.
   - Key entrypoint: `src/backend/app.py`
   - v4 API routes: `src/backend/v4/api/router.py`

2. **Frontend** (React/Vite, port 3000)
   - Owns: UI that submits prompts, shows streaming updates via WebSocket.
   - Folder: `src/frontend/`

3. **MCP Server** (FastMCP, usually port 9000)
   - Owns: “tools” that agents can call.
   - Folder: `src/mcp_server/`
   - Entrypoint: `src/mcp_server/mcp_server.py`

### How they connect

- Frontend calls backend REST endpoints (ex: `/api/v4/process_request`) and listens to backend websocket: `/api/v4/socket/{plan_id}`.
- Backend runs multi-agent orchestration (agent_framework Magentic workflow).
- During orchestration, an agent may call MCP tools.
- MCP tools are implemented in `src/mcp_server/services/*` and can call back into the backend (or external APIs).

**Important idea:** MCP is basically “function calls over HTTP/stdio”, with structured JSON inputs/outputs.

---

## 2) Mental model: what is an “agent” here?

In this repo there are two related but different concepts:

### A) Deterministic business logic (normal code)

Example: MER review checks.

- Parse data
- Compare numbers
- Emit structured evidence
- Return pass/fail

This lives in regular Python modules, with unit tests.

### B) Orchestration / planning (LLM + workflow)

This is where the “agent” part happens.

- You give a natural-language prompt.
- The orchestrator/agent(s) decide what steps to take.
- The agent(s) may call tools (MCP tools or internal tools) to fetch data.
- The agent(s) then produce a final response.

In this repo, orchestration is done in the backend via `agent_framework` (“Magentic”).

Key file: `src/backend/v4/orchestration/orchestration_manager.py`

The important separation:

- Deterministic checks should **not** depend on the LLM.
- The LLM should primarily decide **which deterministic checks/tools to run**, and how to summarize.

---

## 3) Repo map (what lives where)

### Top-level

- `infra/`: Bicep deployment for Azure.
- `data/`: sample datasets, team configs, MER rulebooks.
- `scripts/`: local scripts/smoke tests/CLI runners.

### Backend

- `src/backend/app.py`: FastAPI app, lifecycle hooks, compatibility endpoints.
- `src/backend/v4/api/router.py`: v4 routes (REST + websocket), plus MER review API endpoint.
- `src/backend/v4/orchestration/`: multi-agent orchestration.
- `src/backend/common/`: config + database + shared models.

### MCP server

- `src/mcp_server/mcp_server.py`: registers services/tools and starts FastMCP.
- `src/mcp_server/services/*`: tool implementations.

### MER-specific code

- Rulebook YAML: `data/mer_rulebooks/balance_sheet_review_points.yaml`
- Deterministic rule engine: `src/backend/v4/use_cases/mer_rule_engine.py`
- Deterministic check primitives: `src/backend/v4/use_cases/mer_review_checks.py`
- Integrations:
  - QBO client: `src/backend/v4/integrations/qbo_client.py`
  - Google Sheets reader: `src/backend/v4/integrations/google_sheets_reader.py`
  - QBO report parsing: `src/backend/v4/integrations/qbo_reports.py`
- Backend endpoint:
  - POST `/api/v4/mer/review/balance_sheet` in `src/backend/v4/api/router.py`
- MCP tool:
  - `mer_balance_sheet_review` in `src/mcp_server/services/mer_review_service.py`

---

## 4) The MER review flow (end-to-end)

There are **three ways** you run MER review in this repo:

### Path 1: Direct backend API (deterministic)

Call the FastAPI route:

- `POST http://127.0.0.1:8000/api/v4/mer/review/balance_sheet`

That endpoint:

1. Loads the YAML rulebook (defaults to `data/mer_rulebooks/balance_sheet_review_points.yaml`).
2. Reads MER Balance Sheet data from Google Sheets (read-only).
3. Reads QBO Balance Sheet data from QBO (read-only).
4. Runs the rule engine `MERBalanceSheetRuleEngine.evaluate(...)`.
5. Returns structured JSON with `results` and useful metadata (`policies`, `requires_clarification`, etc.).

This is the best path when you want repeatability and testability.

### Path 2: MCP tool (still deterministic)

The MCP server exposes the tool:

- `mer_balance_sheet_review(end_date, ...)`

Implementation:

- `src/mcp_server/services/mer_review_service.py` calls
- `src/mcp_server/services/mer_review_backend_client.py` which POSTs to the backend.

So MCP is essentially a transport wrapper around the backend route.

### Path 3: Local runner script (LLM + tools)

The script `scripts/mer_llm_agent_local.py` is a **standalone “agent runner”** that:

- Defines a few tools (QBO fetch, MER sheet fetch, MER review).
- Uses Azure OpenAI tool-calling to decide what to call.
- Formats the results into bullets / summaries.

This is great for iteration, demos, and when you want “agentic” behavior without spinning up the full backend/frontend.

Key behavior to know:

- If you set `MER_AGENT_TOOL_ONLY=1`, it bypasses Azure OpenAI and runs `mer_balance_sheet_review` deterministically.
- Otherwise it uses Azure OpenAI and will (usually) call tools, then write a narrative answer.

---

## 5) How the MER rulebook maps to code

The rulebook is YAML and intentionally “product-ish” (it reads like a checklist).

Example rule:

- `rule_id: BS-UNDEPOSITED-FUNDS-ZERO`
- `evaluation.type: balance_sheet_line_items_must_be_zero`

The engine does:

- Read `rules[*].evaluation.type`
- Dispatch to a handler in `MERBalanceSheetRuleEngine` (via `EvaluationRegistry`).

Important: not every rule in the YAML is implemented yet.

- Implemented rules return `status: passed|failed|skipped` and `details`.
- Unimplemented rule types return `status: unimplemented`.

This is a good pattern for incremental rollout: the YAML can contain the entire policy set even while code only implements the MVP subset.

---

## 6) Local dev: how to run (macOS)

There are two common dev loops:

### Loop A: Full app (frontend + backend + MCP)

Follow [docs/LocalDevelopmentSetup.md](LocalDevelopmentSetup.md) (3 terminals).

Typical (conceptual) commands:

- Backend (port 8000): run from `src/backend/`
- Frontend (port 3000): run from `src/frontend/`
- MCP server (port 9000): run from `src/mcp_server/`

### Loop B: MER-only local scripts

Run from repo root (so file paths like `data/...` resolve naturally).

Useful scripts:

- `scripts/mer_rulebook_smoke.py` (validates YAML structure; no network)
- `scripts/mer_review_smoke_test.py` (calls both Google Sheets + QBO)
- `scripts/mer_llm_agent_local.py` (LLM tool-calling runner)

---

## 7) Configuration & env vars (the ones that actually matter)

### Backend config

Backend loads env via `src/backend/common/config/app_config.py`.

Important Azure vars (non-exhaustive):

- `AZURE_OPENAI_ENDPOINT`
- `AZURE_OPENAI_DEPLOYMENT_NAME`
- `AZURE_OPENAI_API_VERSION`
- `AZURE_AI_*` (Foundry project/resource identifiers)
- `COSMOSDB_*` (if using Cosmos)

### MER (Google Sheets)

Used by `GoogleSheetsReader.from_env()`:

- `SPREADSHEET_ID`
- `GOOGLE_SA_FILE` (path to service account JSON)
- `GOOGLE_HTTP_TIMEOUT_SECONDS` (optional)

### MER (QuickBooks Online)

Used by `QBOClient.from_env()`:

- `QBO_CLIENT_ID`
- `QBO_CLIENT_SECRET`
- `QBO_ENVIRONMENT` (`sandbox` default)
- `QBO_REDIRECT_URI` (defaults to `http://localhost`)
- `QBO_TOKENS_PATH` (defaults to `./.env_qbo_tokens.json`)

Token bootstrap happens via `scripts/qbo_auth_local.py` (generates token json file).

### MER local LLM runner

`scripts/mer_llm_agent_local.py` uses:

- `MER_AGENT_AUTH` = `aad` (default) or `api_key`
- `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT_NAME`
- `AZURE_OPENAI_API_KEY` (only if `MER_AGENT_AUTH=api_key`)
- `MER_AGENT_TOOL_ONLY=1` (bypass LLM)
- `MER_AGENT_EMIT_RUN_LOG=1` (write run JSON to `.mer_agent_runs/`)

---

## 8) Testing: what to run, depending on what you changed

### Quick sanity checks (fast)

- Rulebook schema check (no network):
  - `python scripts/mer_rulebook_smoke.py`

- MER integration smoke test (network: Google + QBO):
  - `python scripts/mer_review_smoke_test.py`

### Unit tests

The repo uses `pytest`.

- Backend tests live under `src/backend/tests/`.
- MCP tests live under `src/tests/mcp_server/`.

If you only changed MER rule evaluation logic, focus on:

- `src/backend/tests/use_cases/test_mer_rule_engine.py`

### E2E tests

There is an `tests/e2e-test/` folder for end-to-end test flows.

---

## 9) “What did I actually build?” (your MER extension)

A practical way to view your work is to group it into 4 layers:

1. **Integrations** (data access)
   - QBO API client (`qbo_client.py`)
   - Sheets API reader (`google_sheets_reader.py`)

2. **Deterministic domain logic** (unit-testable)
   - Check primitives (`mer_review_checks.py`)
   - Rule engine (`mer_rule_engine.py`)

3. **API surface** (how it’s invoked)
   - Backend route: `/api/v4/mer/review/balance_sheet`

4. **Agent surfaces / UX** (how people experience it)
   - MCP tool: `mer_balance_sheet_review`
   - Local runner: `scripts/mer_llm_agent_local.py`
   - Terminal runner for deployed backend: `scripts/mer_llm_agent_terminal.py`

This layered approach is the main “agentic app trick”: make layer (2) rock-solid and deterministic, then layer (4) can be LLM-driven without turning your whole system into chaos.

---

## 10) If you want to learn agents (without getting lost)

A good learning path for this repo is:

1. Run `scripts/mer_rulebook_smoke.py` and read the YAML.
2. Read `MERBalanceSheetRuleEngine` and one handler end-to-end.
3. Run the backend endpoint directly and look at raw JSON.
4. Add one new rule type end-to-end:
   - YAML rule
   - Engine handler
   - Test
   - (Optional) expose it via MCP + update prompt runner

If you want, tell me which scenario you care about most next:

- “I want to extend MER rules”
- “I want to understand multi-agent orchestration in the backend”
- “I want to understand MCP tools and how agents call them”

---

## 11) Deep dive: multi-agent orchestration in the backend (Magentic)

This is the “real” orchestration loop in this repo. If you want to truly understand the system, anchor yourself on these files:

- `src/backend/v4/api/router.py` (HTTP + WebSocket entrypoints)
- `src/backend/v4/orchestration/orchestration_manager.py` (runs the workflow)
- `src/backend/v4/orchestration/human_approval_manager.py` (approval-gated planning)
- `src/backend/v4/callbacks/response_handlers.py` (streaming → websocket messages)
- `src/backend/v4/config/settings.py` (`orchestration_config` + `connection_config` state)

### 11.1 The 30-second mental model

- The backend builds (or reuses) a **workflow** per user: a Magentic “multi-agent conversation runner”.
- The workflow emits a stream of events.
- The backend converts those events into websocket messages so the frontend can render:
   - incremental text
   - tool calls
   - plan approval requests
   - final answer

### 11.2 The services and data structures you should know

**`connection_config` (WebSocket routing)**

- Stored in `src/backend/v4/config/settings.py` as a global `ConnectionConfig()`.
- Maps `user_id -> process_id -> WebSocket`.
- Any part of the backend can do:

```python
await connection_config.send_status_update_async(message, user_id, message_type=...)
```

…without knowing which socket it is.

**`orchestration_config` (workflow + approval/clarification state)**

- Also global in `src/backend/v4/config/settings.py` as `OrchestrationConfig()`.
- Stores:
   - `orchestrations[user_id]` → the workflow instance
   - `approvals[m_plan_id]` + an `asyncio.Event` per approval
   - `clarifications[request_id]` + an `asyncio.Event` per clarification

This “event map” is the key to understanding why the orchestrator can *pause* and later resume.

### 11.3 End-to-end backend call flow

#### Step A — Frontend opens the websocket

Backend route: `GET ws://.../api/v4/socket/{process_id}?user_id=...`

Key line:

```python
connection_config.add_connection(process_id=process_id, connection=websocket, user_id=user_id)
```

Code: `src/backend/v4/api/router.py` (`@app_v4.websocket("/socket/{process_id}")`)

#### Step B — Frontend starts a request

Backend route: `POST /api/v4/process_request`

This endpoint creates a `Plan` record and then schedules the orchestration in the background:

```python
async def run_orchestration_task():
      await OrchestrationManager().run_orchestration(user_id, input_task)

background_tasks.add_task(run_orchestration_task)
```

Code: `src/backend/v4/api/router.py` (`process_request`)

#### Step C — Orchestration runs and yields events

Inside `OrchestrationManager.run_orchestration(...)`:

- It loads the per-user workflow from `orchestration_config`.
- It calls `workflow.run_stream(task_text)`.
- For each event, it translates it into websocket messages.

Simplified excerpt:

```python
async for event in workflow.run_stream(task_text):
      if isinstance(event, MagenticAgentDeltaEvent):
            await streaming_agent_response_callback(event.agent_id, event, False, user_id)
      elif isinstance(event, MagenticAgentMessageEvent):
            agent_response_callback(event.agent_id, event.message, user_id)
```

Code: `src/backend/v4/orchestration/orchestration_manager.py`

#### Step D — Streaming callback emits *text* and *tool-call* websocket events

The callback `streaming_agent_response_callback(...)` does two important things:

1) Emits text chunks as `AGENT_MESSAGE_STREAMING`
2) Detects tool/function calls inside `update.contents` and emits them as `AGENT_TOOL_MESSAGE`

That second part is what makes tool calls visible in the UI.

Code: `src/backend/v4/callbacks/response_handlers.py`

### 11.4 Human-in-the-loop: plan approval

The orchestrator manager is `HumanApprovalMagenticManager`, which overrides Magentic’s `plan()` method.

The flow is:

1) orchestrator creates a plan (normal Magentic behavior)
2) backend sends `PLAN_APPROVAL_REQUEST` over websocket
3) backend blocks waiting for approval

Blocking happens here:

```python
approved = await orchestration_config.wait_for_approval(m_plan_id)
```

Approval arrives from the user via `POST /api/v4/plan_approval`, which calls:

```python
orchestration_config.set_approval_result(m_plan_id, approved)
```

Code:

- `src/backend/v4/orchestration/human_approval_manager.py`
- `src/backend/v4/api/router.py` (`plan_approval`)
- `src/backend/v4/config/settings.py` (`wait_for_approval`, `set_approval_result`)

### 11.5 Human-in-the-loop: clarification via ProxyAgent

When the team needs info from the user, it routes to `ProxyAgent`.

This is a real “agent” in the workflow, but it **does not call the LLM**. It:

- sends `USER_CLARIFICATION_REQUEST` over websocket
- waits on `orchestration_config.wait_for_clarification(request_id)`
- yields a synthetic assistant message containing the user’s answer

Code:

- `src/backend/v4/magentic_agents/proxy_agent.py`
- `src/backend/v4/api/router.py` (`user_clarification`)

---

## 12) Deep dive: MCP tools and how agents call them

This repo uses MCP as a clean separation layer:

- Agents run in the **backend** (agent_framework / Magentic)
- Tools run behind the **MCP server** (FastMCP)

From the agent’s POV: “I have a tool catalog; when I call a tool, I get structured JSON back.”

### 12.1 MCP server: where tools are defined

The MCP server entrypoint is `src/mcp_server/mcp_server.py`.

It uses a small registry pattern:

```python
factory = MCPToolFactory()
factory.register_service(MERReviewService())
mcp = factory.create_mcp_server(name=config.server_name, auth=auth)
```

The factory just loops all registered services and calls `service.register_tools(mcp)`.

Code: `src/mcp_server/core/factory.py`

Each service registers tools using `@mcp.tool(...)`.

Example tool (MER): `src/mcp_server/services/mer_review_service.py`

```python
@mcp.tool(tags={self.domain.value})
async def mer_balance_sheet_review(end_date: str, ...):
      return await call_mer_balance_sheet_review_backend(...)
```

### 12.2 Backend agent side: how MCP becomes a “tool” the agent can call

This is the critical glue.

**Step A — agent config chooses whether MCP is enabled**

In `MagenticAgentFactory.create_agent_from_config(...)`, MCP is only enabled if the agent configuration sets `use_mcp`:

```python
mcp_config = (MCPConfig.from_env() if getattr(agent_obj, "use_mcp", False) else None)
```

Code: `src/backend/v4/magentic_agents/magentic_agent_factory.py`

**Step B — base lifecycle turns MCPConfig into a hosted tool**

During agent `open()`, the base class calls `_prepare_mcp_tool()` which creates an `MCPStreamableHTTPTool`:

```python
mcp_tool = MCPStreamableHTTPTool(
      name=self.mcp_cfg.name,
      description=self.mcp_cfg.description,
      url=self.mcp_cfg.url,
)
self.mcp_tool = mcp_tool
```

Code: `src/backend/v4/magentic_agents/common/lifecycle.py`

**Step C — agent template attaches tools to ChatAgent**

In MCP mode, `FoundryAgentTemplate` collects tools (Code Interpreter optionally + MCP tool) and passes them into `ChatAgent(...)`:

```python
tools = await self._collect_tools()  # includes self.mcp_tool if present
self._agent = ChatAgent(..., tools=tools if tools else None, tool_choice="auto" if tools else "none")
```

Code: `src/backend/v4/magentic_agents/foundry_agent.py`

At this point, the LLM can choose to call an MCP tool exactly like any other function/tool call.

### 12.3 End-to-end example: MER tool call chain

When an agent calls the tool `mer_balance_sheet_review`, the real chain is:

1) Backend agent emits a tool-call (agent_framework)
2) `MCPStreamableHTTPTool` sends the request to the MCP server URL
3) MCP server routes to `mer_balance_sheet_review(...)`
4) Tool implementation calls back into backend REST
5) Tool returns JSON to the agent
6) Agent summarizes results for the user

Concrete code points for MER:

- Tool definition: `src/mcp_server/services/mer_review_service.py`
- Backend client helper: `src/mcp_server/services/mer_review_backend_client.py`
- Deterministic backend endpoint: `src/backend/v4/api/router.py` (`POST /api/v4/mer/review/balance_sheet`)

### 12.4 Why MER’s MCP tool calls the backend (and why that’s good)

You’ll notice the MER MCP tool is basically a wrapper over the backend endpoint.

That’s intentional:

- Keeps the tool surface stable and small
- Lets you keep deterministic logic + integrations in one place (backend)
- Lets the same MER review be invoked from:
   - backend API directly
   - MCP tool
   - local scripts/tests (via the same backend client helper)

### 12.5 How to add a new MCP tool (practical checklist)

1) Add the tool function to an existing service under `src/mcp_server/services/` (or create a new service class).
2) Decorate it with `@mcp.tool(...)` and keep inputs/outputs JSON-serializable.
3) Register the service in `src/mcp_server/mcp_server.py`.
4) Ensure agents that should call tools have `use_mcp: true` in their team configuration.
5) Prefer tools that call deterministic backend endpoints (so you can unit test the business logic without the LLM).

### 12.6 How to see tool calls in the UI

Tool calls become visible because the backend streaming callback emits a websocket message whenever it detects function-call-like items in the streaming update.

Code: `src/backend/v4/callbacks/response_handlers.py` (`_extract_tool_calls_from_contents`)
