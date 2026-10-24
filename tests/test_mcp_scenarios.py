"""Actual stdio MCP servers, real graph/tool execution, no paid APIs."""

import asyncio
import json
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_mcp_adapters.client import MultiServerMCPClient

import main
from tests.helpers import ScenarioModel, install_graph, tool_call

pytestmark = pytest.mark.integration
FIXTURE_SERVER = str(Path(__file__).parent / "fixtures" / "research_server.py")
FILESYSTEM_PACKAGE = "@modelcontextprotocol/server-filesystem@2.0.0"
RUNBOOK = "# Queue incident Q-317\n\nMitigation: pause ingestion, drain the queue, then resume.\nOwner: Platform — São Paulo.\n"


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "shared workspace"
    root.mkdir()
    return root


def filesystem_config(root):
    if not shutil.which("npx"):
        pytest.fail("Integration scenarios require Node.js/npm (npx); see README")
    return {
        "transport": "stdio", "command": "npx",
        "args": ["-y", "--quiet", FILESYSTEM_PACKAGE, str(root)],
        "env": {"NO_UPDATE_NOTIFIER": "1", "npm_config_update_notifier": "false"},
    }


@pytest.fixture
async def filesystem_tools(workspace):
    client = MultiServerMCPClient(
        {"filesystem": filesystem_config(workspace)},
        tool_interceptors=[main.trace_mcp_tool_call],
    )
    tools = await asyncio.wait_for(client.get_tools(), timeout=60)
    return {tool.name: tool for tool in tools}


@pytest.fixture
def runbook_server():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            if self.path == "/robots.txt":
                body, code = "User-agent: *\nAllow: /\n", 200
            elif self.path == "/runbooks/queue":
                body, code = RUNBOOK, 200
            else:
                body, code = "Not found", 404
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/runbooks/queue", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


async def test_incident_research_fetch_save_and_readback_across_real_mcp_servers(
    api, workspace, runbook_server, monkeypatch, caplog,
):
    url, requests = runbook_server
    output = workspace / "incident-handoff.md"
    expected = f"# Incident handoff\nSource: {url}\n\n{RUNBOOK}"
    client = MultiServerMCPClient({
        "research_fixture": {
            "transport": "stdio", "command": sys.executable, "args": [FIXTURE_SERVER],
            "env": {"FIXTURE_RUNBOOK_URL": url},
        },
        # Only this isolated fixture allows loopback; production config is untouched.
        "fetch": {
            "transport": "stdio", "command": sys.executable,
            "args": ["-m", "mcp_server_fetch", "--allow-private-ips"],
        },
        "filesystem": filesystem_config(workspace),
    }, tool_interceptors=[main.trace_mcp_tool_call])
    tools = await asyncio.wait_for(client.get_tools(), timeout=60)
    transcript = []

    def decide(messages):
        results = [m for m in messages if isinstance(m, ToolMessage)]
        if not results:
            return tool_call("search_incidents", {"query": "queue incident mitigation"}, "search")
        last = results[-1]
        transcript.append(last.name)
        assert last.status != "error", last.content
        if last.name == "search_incidents":
            discovered_url = last.text.strip()
            assert discovered_url == url
            return tool_call("fetch", {"url": discovered_url}, "fetch")
        if last.name == "fetch":
            fetched_runbook = last.text.split(f"Contents of {url}:\n", 1)[1]
            assert fetched_runbook == RUNBOOK
            handoff = f"# Incident handoff\nSource: {url}\n\n{fetched_runbook}"
            return tool_call("write_file", {"path": str(output), "content": handoff}, "save")
        if last.name == "write_file":
            return tool_call("read_file", {"path": str(output)}, "verify")
        assert expected in last.text
        return AIMessage(content="Saved and verified incident-handoff.md")

    install_graph(ScenarioModel(decide=decide), tools)
    monkeypatch.setattr(main, "AGENT_INVOKE_TIMEOUT_SECONDS", 60)
    with caplog.at_level("INFO", logger=main.logger.name):
        response = await api.post("/chat", json={
            "query": "Find queue mitigation, fetch the runbook and save a verified handoff",
            "session_id": "oncall-q317",
        })
    assert response.status_code == 200, response.text
    assert response.json() == {"response": "Saved and verified incident-handoff.md"}
    assert transcript == ["search_incidents", "fetch", "write_file", "read_file"]
    assert output.read_text() == expected
    assert "/runbooks/queue" in requests
    assert "session_id=oncall-q317 server=filesystem tool=write_file" in caplog.text
    assert "pause ingestion" not in caplog.text


@pytest.mark.parametrize("escape", ["absolute", "traversal", "symlink"])
@pytest.mark.parametrize("operation", ["read_file", "write_file"])
async def test_filesystem_rejects_escape_without_reading_or_overwriting_outside_files(
    filesystem_tools, workspace, escape, operation,
):
    outside = workspace.parent / "customer-secrets.txt"
    secret = "TEST-ONLY-CUSTOMER-SECRET"
    outside.write_text(secret)
    if escape == "symlink":
        path = workspace / "linked-customer.txt"
        path.symlink_to(outside)
    elif escape == "traversal":
        path = str(workspace) + "/../customer-secrets.txt"
    else:
        path = outside
    args = {"path": str(path)}
    if operation == "write_file":
        args["content"] = "Unauthorized overwrite"
    result = await filesystem_tools[operation].ainvoke({
        "type": "tool_call", "name": operation, "args": args, "id": "escape-attempt",
    })
    assert result.status == "error"
    assert "denied" in result.text.lower() or "outside" in result.text.lower()
    assert secret not in result.text
    assert outside.read_text() == secret


async def test_missing_file_tool_error_reaches_graph_then_agent_recovers_with_real_write(
    api, filesystem_tools, workspace,
):
    path = workspace / "recovery-notes.txt"

    def decide(messages):
        results = [m for m in messages if isinstance(m, ToolMessage)]
        if not results:
            return tool_call("read_file", {"path": str(path)}, "read-missing")
        if len(results) == 1:
            assert results[-1].status == "error"
            assert "ENOENT" in str(results[-1].content)
            return tool_call("write_file", {"path": str(path), "content": "Handoff recovered"}, "recover")
        assert results[-1].status == "success"
        return AIMessage(content="Missing handoff created successfully")

    install_graph(ScenarioModel(decide=decide), filesystem_tools.values())
    response = await api.post("/chat", json={"query": "Recover missing handoff", "session_id": "ops-recovery"})
    assert response.status_code == 200, response.text
    assert response.json() == {"response": "Missing handoff created successfully"}
    assert path.read_text() == "Handoff recovered"


@pytest.mark.parametrize("failure_mode", ["crash", "hang"])
async def test_failed_mcp_startup_keeps_healthy_filesystem_available(
    api, workspace, tmp_path, monkeypatch, failure_mode,
):
    path = workspace / "degraded-mode.txt"
    connections = {
        "unavailable_research": {
            "transport": "stdio", "command": sys.executable, "args": [FIXTURE_SERVER, failure_mode],
        },
        "filesystem": filesystem_config(workspace),
    }
    manifest = tmp_path / "servers.json"
    manifest.write_text(json.dumps(connections))
    monkeypatch.setenv("MCP_SERVERS_CONFIG", str(manifest))
    # Pre-cache/discover the real filesystem before using a small discovery budget.
    healthy = MultiServerMCPClient({"filesystem": connections["filesystem"]})
    await asyncio.wait_for(healthy.get_tools(), timeout=60)
    monkeypatch.setattr(main, "MCP_CONNECT_TIMEOUT_SECONDS", 3)

    def decide(messages):
        if isinstance(messages[-1], ToolMessage):
            assert messages[-1].status == "success"
            return AIMessage(content="Filesystem remains available")
        return tool_call("write_file", {"path": str(path), "content": "Still operational"}, "degraded-write")

    model = ScenarioModel(decide=decide)
    monkeypatch.setattr(main, "ChatOpenAI", lambda **kwargs: model)
    await asyncio.wait_for(main.state.initialize(), timeout=60)
    health = (await api.get("/health")).json()
    assert health["mcp_servers"]["unavailable_research"]["connected"] is False
    error = health["mcp_servers"]["unavailable_research"]["error"]
    assert error
    if failure_mode == "hang":
        assert "timed out after 3s" in error
    assert health["mcp_servers"]["filesystem"]["connected"] is True
    assert "write_file" in model.bound_tool_names
    response = await api.post("/chat", json={"query": "Save handoff", "session_id": "degraded"})
    assert response.status_code == 200, response.text
    assert path.read_text() == "Still operational"


async def test_corrupt_manifest_starts_a_direct_answer_agent_and_reports_no_tools(api, tmp_path, monkeypatch):
    manifest = tmp_path / "corrupt.json"
    manifest.write_text("{this is not valid JSON}")
    monkeypatch.setenv("MCP_SERVERS_CONFIG", str(manifest))
    model = ScenarioModel(decide=lambda messages: AIMessage(content="156"))
    monkeypatch.setattr(main, "ChatOpenAI", lambda **kwargs: model)
    await main.state.initialize()
    assert main.state.client is None
    assert (await api.get("/health")).json() == {"llm_configured": False, "mcp_servers": {}}
    response = await api.post("/chat", json={"query": "12 times 13", "session_id": "direct"})
    assert response.json() == {"response": "156"}


async def test_sessions_share_workspace_even_though_their_conversation_memory_is_separate(
    api, filesystem_tools, workspace,
):
    path = workspace / "team-handoff.txt"

    def decide(messages):
        if isinstance(messages[-1], ToolMessage):
            assert messages[-1].status == "success"
            return AIMessage(content=messages[-1].text)
        if messages[-1].content == "Publish team handoff":
            return tool_call("write_file", {"path": str(path), "content": "Team handoff Q-317"}, "publish")
        return tool_call("read_file", {"path": str(path)}, "shared-read")

    graph = install_graph(ScenarioModel(decide=decide), filesystem_tools.values())
    published = await api.post("/chat", json={"query": "Publish team handoff", "session_id": "author"})
    assert published.status_code == 200
    read = await api.post("/chat", json={"query": "Read team handoff", "session_id": "reader"})
    assert read.json() == {"response": "Team handoff Q-317"}
    reader = await graph.aget_state({"configurable": {"thread_id": "reader"}})
    assert all(m.content != "Publish team handoff" for m in reader.values["messages"])


async def test_production_fetch_defaults_refuse_loopback_before_contacting_private_server(runbook_server):
    url, requests = runbook_server
    manifest = json.loads((Path(main.__file__).parent / "mcp_config.json").read_text())
    fetch_config = manifest["fetch"]
    assert "--allow-private-ips" not in fetch_config["args"]
    # Use the test interpreter so the subprocess resolves the installed fetch package.
    client = MultiServerMCPClient({"fetch": {
        "transport": fetch_config["transport"], "command": sys.executable,
        "args": fetch_config["args"],
    }})
    tools = await asyncio.wait_for(client.get_tools(), timeout=30)
    result = await tools[0].ainvoke({
        "type": "tool_call", "name": "fetch", "args": {"url": url}, "id": "private-fetch",
    })
    assert result.status == "error"
    assert "private" in result.text.lower() or "loopback" in result.text.lower()
    assert requests == []
