"""Service regressions: multi-user memory, deadline cancellation, and safe errors."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage

import main
from tests.helpers import ScenarioModel, install_graph


async def test_twelve_concurrent_conversations_keep_checkpoint_history_isolated(api):
    def recall(messages):
        user_messages = [m.content for m in messages if isinstance(m, HumanMessage)]
        if user_messages[-1] == "Recall my incident code":
            return AIMessage(content=user_messages[0].removeprefix("Remember "))
        return AIMessage(content="Recorded in this conversation")

    model = ScenarioModel(decide=recall)
    graph = install_graph(model)

    async def conversation(index):
        session = f"tenant-{index}"
        code = f"INC-{index:04d}-private"
        first = await api.post("/chat", json={"query": f"Remember {code}", "session_id": session})
        assert first.status_code == 200
        second = await api.post("/chat", json={"query": "Recall my incident code", "session_id": session})
        assert second.status_code == 200
        assert second.json() == {"response": code}
        snapshot = await graph.aget_state({"configurable": {"thread_id": session}})
        assert len(snapshot.values["messages"]) == 4
        assert [m.content for m in snapshot.values["messages"] if isinstance(m, HumanMessage)] == [
            f"Remember {code}", "Recall my incident code",
        ]

    await asyncio.gather(*(conversation(i) for i in range(12)))
    assert len(model.seen) == 24
    assert main.active_session_id.get() == "startup"


async def test_new_process_state_does_not_pretend_memory_is_durable(api, monkeypatch):
    def recall(messages):
        history = [m.content for m in messages if isinstance(m, HumanMessage)]
        return AIMessage(content=history[0] if len(history) > 1 else "No previous message")

    install_graph(ScenarioModel(decide=recall))
    await api.post("/chat", json={"query": "Incident INC-42", "session_id": "returning-user"})
    monkeypatch.setattr(main, "state", main.AgentState())
    install_graph(ScenarioModel(decide=recall))
    response = await api.post("/chat", json={"query": "Recall incident", "session_id": "returning-user"})
    assert response.json() == {"response": "No previous message"}


async def test_timeout_cancels_work_resets_trace_context_and_next_request_succeeds(api, monkeypatch):
    started = asyncio.Event()
    cancelled = asyncio.Event()
    never = asyncio.Event()

    async def stalled(*args, **kwargs):
        started.set()
        try:
            await never.wait()
        finally:
            cancelled.set()

    agent = SimpleNamespace(ainvoke=AsyncMock(side_effect=stalled))
    main.state.agent = agent
    monkeypatch.setattr(main, "AGENT_INVOKE_TIMEOUT_SECONDS", 0.03)
    response = await api.post("/chat", json={"query": "Slow research", "session_id": "deadline"})
    assert response.status_code == 504
    assert started.is_set() and cancelled.is_set()
    assert main.active_session_id.get() == "startup"
    agent.ainvoke.side_effect = None
    agent.ainvoke.return_value = {"messages": [AIMessage(content="Recovered")]}
    response = await api.post("/chat", json={"query": "Try again", "session_id": "next-user"})
    assert response.json() == {"response": "Recovered"}
    assert main.active_session_id.get() == "startup"


async def test_provider_failure_does_not_expose_credentials_and_service_recovers(api):
    private_detail = "provider rejected api_key=sk-test-DO-NOT-EXPOSE /srv/private/customer.json"
    main.state.agent = SimpleNamespace(ainvoke=AsyncMock(side_effect=RuntimeError(private_detail)))
    response = await api.post("/chat", json={"query": "Research", "session_id": "failure"})
    assert response.status_code == 500
    assert "sk-test-DO-NOT-EXPOSE" not in response.text
    assert "/srv/private" not in response.text
    assert response.json()["response"]
    assert (await api.get("/")).status_code == 200
    assert main.active_session_id.get() == "startup"
    install_graph(ScenarioModel(decide=lambda messages: AIMessage(content="Recovered")))
    recovery = await api.post("/chat", json={"query": "Continue", "session_id": "after-failure"})
    assert recovery.json() == {"response": "Recovered"}


@pytest.mark.parametrize("result", [{}, {"messages": []}, {"messages": [None]}, None])
async def test_malformed_agent_output_returns_safe_json_instead_of_breaking_http(api, result):
    main.state.agent = SimpleNamespace(ainvoke=AsyncMock(return_value=result))
    response = await api.post("/chat", json={"query": "Generate handoff", "session_id": "bad-output"})
    assert response.status_code == 500
    assert response.json()["response"]
    assert "error" not in response.json()


@pytest.mark.parametrize("query,session", [(" \n\t", "ops"), ("Investigate", " \t"), ("", "ops")])
async def test_invalid_blank_request_never_invokes_agent(api, query, session):
    main.state.agent = SimpleNamespace(ainvoke=AsyncMock())
    response = await api.post("/chat", json={"query": query, "session_id": session})
    assert response.status_code == 400
    main.state.agent.ainvoke.assert_not_awaited()


async def test_startup_readiness_and_schema_errors_are_actionable(api):
    response = await api.post("/chat", json={"query": "Investigate", "session_id": "ops"})
    assert response.status_code == 503
    response = await api.post("/chat", json={"query": "Investigate"})
    assert response.status_code == 422


def test_manifest_substitution_handles_quoted_secrets_paths_and_server_overrides(tmp_path, monkeypatch):
    path = tmp_path / "servers.json"
    path.write_text(json.dumps({"research": {
        "transport": "stdio", "command": "python", "args": ["${WORKSPACE_ROOT}"],
        "env": {"TOKEN": "${TEST_TOKEN}", "CHILD_MODE": "isolated"},
        "_comment": "Documentation only",
    }}))
    monkeypatch.setenv("MCP_SERVERS_CONFIG", str(path))
    monkeypatch.setenv("WORKSPACE_ROOT", '/tmp/incident "Q4"/résumés')
    monkeypatch.setenv("TEST_TOKEN", 'token-with-"quotes"\\and\nnewlines')
    monkeypatch.setenv("CHILD_MODE", "parent")
    monkeypatch.setenv("PARENT_PROXY", "http://internal-proxy.test")
    config = main._load_mcp_server_config()["research"]
    assert config["args"] == ['/tmp/incident "Q4"/résumés']
    assert config["env"]["TOKEN"] == 'token-with-"quotes"\\and\nnewlines'
    assert config["env"]["CHILD_MODE"] == "isolated"
    assert config["env"]["PARENT_PROXY"] == "http://internal-proxy.test"
    assert "_comment" not in config


async def test_tool_traces_correlate_concurrent_sessions_without_logging_payloads(caplog):
    barrier = asyncio.Event()
    count = 0

    async def handler(request):
        nonlocal count
        count += 1
        if count == 2:
            barrier.set()
        await asyncio.wait_for(barrier.wait(), timeout=1)
        return SimpleNamespace(isError=False)

    async def run(session):
        token = main.active_session_id.set(session)
        try:
            await main.trace_mcp_tool_call(SimpleNamespace(
                server_name="filesystem", name="write_file",
                args={"content": "CONFIDENTIAL-CUSTOMER-DATA", "path": "/private/customer.txt"},
            ), handler)
        finally:
            main.active_session_id.reset(token)

    with caplog.at_level("INFO", logger=main.logger.name):
        await asyncio.gather(run("incident-a"), run("incident-b"))
    for session in ("incident-a", "incident-b"):
        assert f"MCP tools/call session_id={session}" in caplog.text
        assert f"MCP tool result session_id={session}" in caplog.text
    assert "CONFIDENTIAL-CUSTOMER-DATA" not in caplog.text
    assert "/private/customer.txt" not in caplog.text


async def test_last_resort_exception_handler_does_not_expose_backend_details():
    from starlette.requests import Request

    request = Request({"type": "http", "method": "POST", "path": "/chat", "headers": []})
    response = await main.unhandled_exception_handler(request, RuntimeError("sk-test-INTERNAL-SECRET"))
    assert response.status_code == 500
    assert b"sk-test-INTERNAL-SECRET" not in response.body
