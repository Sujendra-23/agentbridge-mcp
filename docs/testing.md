# Scenario validation and technical walkthrough

AgentBridge coordinates external tools and keeps conversation history in process memory. Its tests focus on whether the service preserves those boundaries and remains useful when individual components fail.

## Recorded validation

Local run on **October 23, 2026**:

```text
Python 3.13.9 / Node.js v26.8.1 / macOS
python -m pytest -q --junitxml=test-results/scenarios.xml
28 passed in 19.06s
```

This is a recorded local run, not a latency or throughput benchmark. GitHub Actions validates Linux with Python 3.13 and Node.js 24; consult the [workflow runs](https://github.com/Sujendra-23/agentbridge-mcp/actions/workflows/scenario-tests.yml) for CI status and downloadable JUnit results.

| Layer | Collected cases | Source |
| --- | ---: | --- |
| Service behavior and configuration | 15 | [test_service_scenarios.py](../tests/test_service_scenarios.py) |
| MCP integration and state boundaries | 13 | [test_mcp_scenarios.py](../tests/test_mcp_scenarios.py) |

Parameterized cases count separately: the six filesystem escape checks cover three path forms for both read and write, for example.

## What runs for real

- In-process HTTP requests pass through the actual FastAPI application and Pydantic validation.
- The actual LangGraph ReAct graph executes tools and persists checkpoints using `MemorySaver`.
- The official filesystem MCP server runs through `npx`, pinned in tests to version `2.0.0`.
- The reference fetch MCP server runs as a Python subprocess and retrieves a runbook from an ephemeral local HTTP server.
- A small search fixture runs as its own MCP stdio subprocess. Its separate crash and hang modes simulate failed discovery without mocking the transport.
- Read/write denial checks inspect both tool error status and the outside file's unchanged contents.

The [scenario model](../tests/helpers.py) supplies deterministic model messages and tool selections. Its decisions inspect actual tool results; the incident workflow passes the discovered URL to fetch, then saves the fetched text. It does not replace MCP tools, graph execution, or checkpoint storage. Some HTTP failure tests inject a stalled or failing agent invocation to target cancellation and error behavior directly.

The fixture search server is **not Tavily**. No tests make paid model/search requests or evaluate model reasoning. Tests block implicit OpenAI model construction, disable LangSmith tracing, and avoid loading the developer's `.env`. The initial npm download requires registry access; the scenario data itself comes from local fixtures. Missing `npx` fails integration checks explicitly rather than silently skipping them.

## Scenarios to inspect

### An on-call engineer prepares an incident handoff

`test_incident_research_fetch_save_and_readback_across_real_mcp_servers` starts three MCP integrations and drives four tool calls through `POST /chat`:

1. Search the controlled incident corpus for a queue runbook.
2. Fetch the URL returned by the search result.
3. Save a handoff containing that source URL and the fetched runbook.
4. Read it back through MCP and compare it with the expected artifact and the file on disk.

Assertions include exact UTF-8 content, tool order, the HTTP fixture receiving the runbook request, and the session ID appearing in filesystem tool traces without logging the runbook text. Only this test's fetch subprocess enables loopback access. A separate check uses the production manifest's fetch arguments and verifies that default private-address protection rejects the same kind of URL without contacting the fixture.

### Multiple customers discuss separate incidents

`test_twelve_concurrent_conversations_keep_checkpoint_history_isolated` overlaps 12 conversations with `asyncio.gather`. Each makes a remember request followed by a recall request. The test checks both the returned incident code and the stored user messages, rather than trusting a successful HTTP status alone.

This validates routing and checkpoint isolation for **different session IDs** under a deterministic model. It does not establish user authentication or correctness for overlapping turns using the same ID. A separate real-tool scenario demonstrates that different sessions can read the same workspace file, while a state-replacement test demonstrates that conversation memory is not durable.

### A dependency fails during startup or a tool returns an error

The crash/hang scenarios invoke `AgentState.initialize` against a real failing subprocess and a healthy filesystem peer. The discovery timeout is reduced to three seconds for the test. After initialization, the health response must mark the failed peer disconnected, and a chat must still create a file through the healthy peer.

The missing-file scenario gets a real filesystem error, checks that it arrives in the graph as a `ToolMessage` with error status, and then completes a recovery write. The deadline scenario separately verifies cancellation of a stalled invocation, restoration of the request's context variable, and success of a subsequent chat.

### A tool attempts to leave its workspace

Read and write attempts target a synthetic secret outside the allowed directory through an absolute path, parent traversal, and an in-workspace symlink. Each must return an MCP tool error. The response must omit the secret contents, and the outside file must remain unchanged.

These are regression checks against the configured server version, not a complete sandbox audit. The tests check common escape paths; they do not test filesystem races or provide per-user file isolation.

## Bugs found and fixed

| Trigger | Previous behavior | Fix |
| --- | --- | --- |
| Backend exception includes an API key or private path | `/chat` and the fallback handler echoed raw exception text in an `error` field. | Return a generic JSON error body; keep exception diagnostics in server logs. |
| Environment value contains quotes, backslashes, or newlines | Substitution happened in raw JSON and could invalidate the server manifest. | Parse JSON first, then recursively substitute string values while preserving parent environment and per-server overrides. |
| Agent returns `None` instead of a message result | Result parsing raised a `TypeError` outside the route's result-shape handler. | Handle `TypeError` alongside missing keys, empty message lists, and missing message attributes. |

Exception logs and MCP discovery errors in `/health` can still contain diagnostic details; the HTTP error-body regression is scoped to chat/fallback errors. The tool trace middleware records argument names and outcomes, rather than argument values.

## Reproduce

From the repository root, with Python 3.13 and Node.js 24+ / npm available:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -q --junitxml=test-results/scenarios.xml
```

Run one scenario while walking through its assertions:

```bash
python -m pytest -v tests/test_mcp_scenarios.py::test_incident_research_fetch_save_and_readback_across_real_mcp_servers
python -m pytest -v tests/test_mcp_scenarios.py -k rejects_escape
python -m pytest -v tests/test_service_scenarios.py -k concurrent
```

Tests create temporary workspaces rather than modifying `mcp_workspace`. The JUnit output and pytest cache are ignored by Git. CI runs all scenarios on every push and pull request and uploads the report even if a test fails.

## Design tradeoffs for a technical walkthrough

| Decision | Current benefit | Constraint / next step |
| --- | --- | --- |
| Per-server discovery timeout and partial startup | One unavailable integration does not prevent healthy tools from serving requests. | Discovery is sequential; startup can take the sum of the per-server waits. |
| Session-keyed in-memory checkpointing | Small deployment with inspectable conversation state. | Restarts lose history; durable multi-instance operation needs persistent checkpoints. |
| Shared filesystem root | Sessions can collaborate on saved artifacts. | Session IDs do not authorize access; private customer workspaces require authentication and separate roots. |
| Invocation deadline | Bounds the application's wait and cancels cooperative asynchronous work. | Cancellation does not undo completed writes or guarantee cancellation of remote side effects. |
| Deterministic model in CI | Repeatable tool-path and failure assertions without inference cost. | Live model selection, prompt injection resistance, and Tavily quality require separate evaluations. |
| Pinned test filesystem server | Reproducible integration contract. | Production npm dependencies remain unpinned and may drift from that contract. |

Useful next evaluations would measure task completion and tool-selection accuracy with a live model, overlapping turns within a single session, and restart behavior with persistent storage. The current suite makes no claims about those outcomes.
