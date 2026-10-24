"""
CSCI 599 Assignment 1 -- Tool-Using Agent with MCP Integration.

A LangGraph ReAct agent, served over FastAPI, that reasons over user queries,
discovers and invokes tools exposed by real MCP servers (filesystem, Tavily
search, and a fetch server) via langchain-mcp-adapters, and keeps
conversational memory per session with a LangGraph checkpointer.

Run locally:    python main.py
Deploy:         see deploy.sh / README.md
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel

load_dotenv()

MCP_CONNECT_TIMEOUT_SECONDS = int(os.environ.get("MCP_CONNECT_TIMEOUT_SECONDS", "20"))
AGENT_INVOKE_TIMEOUT_SECONDS = int(os.environ.get("AGENT_INVOKE_TIMEOUT_SECONDS", "90"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("csci599-a1")
active_session_id: ContextVar[str] = ContextVar("active_session_id", default="startup")

SYSTEM_PROMPT = """You are a tool-using assistant with access to MCP-backed tools:

- Filesystem tools (read_file, write_file, list_directory, etc.) operate on a
  sandboxed workspace directory. Use them for requests about reading, writing,
  listing, or searching files in that workspace.
- tavily_search is for open-ended, query-based web search -- use it when the
  user asks about current events, facts you are not confident about, or
  anything that benefits from a fresh web lookup. tavily_extract, tavily_crawl,
  tavily_map, and tavily_research are available for deeper research once a
  search has identified relevant URLs or a broader topic.
- fetch retrieves the content of one specific, already-known URL. Use it when
  the user gives you a URL directly, or after a search has identified the
  single page you need to read in full.

For requests that need more than one step (e.g. "search for X, then save a
summary to notes.txt"), chain tool calls: call one tool, read its result, and
decide whether another tool call is needed before giving a final answer.

If a query does not require any tool -- general knowledge, reasoning, math,
conversation -- answer directly from what you know. Do not call a tool just
because one is available; call it because the query needs it.

Conversation memory consists of the messages supplied for the current session.
When asked to remember a conversational fact, keep it in this conversation;
do not write a file unless the user explicitly asks to save a file. When asked
about an earlier message, use only this session's conversation history. If
the fact is absent, say you do not know it. Do not search files or the web to
recover another session's conversation. The filesystem workspace is shared
between sessions; its files are not conversational memory.

If a tool call fails or returns an error, explain plainly what went wrong and,
if possible, continue helping with the rest of the request rather than giving
up on the whole turn."""


def _load_mcp_server_config() -> dict[str, dict[str, Any]]:
    """Read the MCP server manifest and substitute ${VAR} with environment values."""
    config_path = os.environ.get("MCP_SERVERS_CONFIG", "./mcp_config.json")
    with open(config_path, encoding="utf-8") as f:
        raw_config = json.load(f)

    def _sub(match: re.Match) -> str:
        var_name = match.group(1)
        return os.environ.get(var_name, "")

    def _substitute(value: Any) -> Any:
        # Substitute after parsing so quotes, backslashes, and newlines in an
        # environment value remain data rather than corrupting the JSON syntax.
        if isinstance(value, str):
            return re.sub(r"\$\{([A-Z_][A-Z0-9_]*)\}", _sub, value)
        if isinstance(value, list):
            return [_substitute(item) for item in value]
        if isinstance(value, dict):
            return {key: _substitute(item) for key, item in value.items()}
        return value

    full_config: dict[str, dict[str, Any]] = _substitute(raw_config)

    # Only pass through keys langchain-mcp-adapters' StdioConnection understands;
    # drop our own documentation-only "_comment" field.
    allowed_keys = {"transport", "command", "args", "env", "cwd"}
    connections: dict[str, dict[str, Any]] = {}
    for name, cfg in full_config.items():
        entry = {k: v for k, v in cfg.items() if k in allowed_keys}
        # The MCP SDK only forwards a minimal default environment (HOME, PATH,
        # SHELL, TERM) to the subprocess unless we hand it a full one. npx in
        # particular needs more than that in some environments (npm config,
        # proxy settings, etc.), so pass the full parent environment through
        # and layer the server-specific overrides (API keys, notifier flags)
        # on top, rather than a hand-picked minimal set.
        entry["env"] = {**os.environ, **entry.get("env", {})}
        connections[name] = entry
    return connections


async def trace_mcp_tool_call(request, handler):
    """Record real tool execution without logging file contents or credentials."""
    session_id = active_session_id.get()
    logger.info(
        "MCP tools/call session_id=%s server=%s tool=%s argument_keys=%s",
        session_id, request.server_name, request.name, sorted(request.args),
    )
    try:
        result = await handler(request)
    except Exception as exc:
        logger.warning(
            "MCP tool failure session_id=%s server=%s tool=%s exception=%s",
            session_id, request.server_name, request.name, type(exc).__name__,
        )
        raise
    is_error = bool(getattr(result, "isError", False)) or getattr(result, "status", None) == "error"
    logger.info(
        "MCP tool result session_id=%s server=%s tool=%s is_error=%s",
        session_id, request.server_name, request.name, is_error,
    )
    return result


class AgentState:
    """Holds the agent, its tool client, and per-server connectivity status."""

    def __init__(self) -> None:
        self.client: MultiServerMCPClient | None = None
        self.agent = None
        self.checkpointer = MemorySaver()
        self.server_status: dict[str, dict[str, Any]] = {}
        self.llm_configured: bool = False

    async def initialize(self) -> None:
        self.llm_configured = bool(os.environ.get("OPENAI_API_KEY"))
        if not self.llm_configured:
            logger.warning(
                "OPENAI_API_KEY is not set. The agent will fail LLM calls until it is "
                "configured, but the server will still start."
            )

        try:
            connections = _load_mcp_server_config()
        except Exception:
            logger.exception("Could not read MCP server config; starting with zero MCP tools.")
            connections = {}

        self.client = (
            MultiServerMCPClient(connections, tool_interceptors=[trace_mcp_tool_call])
            if connections else None
        )

        all_tools: list[Any] = []
        for name, cfg in connections.items():
            try:
                # Failure mode: connection/transport failure. A server that is
                # unreachable, misconfigured, or slow to start (e.g. a bad API
                # key that the server blocks on instead of rejecting outright)
                # must not take the rest of the agent down with it -- bound the
                # wait, catch per server, and continue with whatever did connect.
                tools = await asyncio.wait_for(
                    self.client.get_tools(server_name=name),
                    timeout=MCP_CONNECT_TIMEOUT_SECONDS,
                )
                all_tools.extend(tools)
                self.server_status[name] = {"connected": True, "tools": [t.name for t in tools]}
                logger.info("Connected to MCP server '%s' with tools: %s", name, [t.name for t in tools])
            except TimeoutError:
                self.server_status[name] = {
                    "connected": False,
                    "error": f"timed out after {MCP_CONNECT_TIMEOUT_SECONDS}s connecting/listing tools",
                }
                logger.warning("MCP server '%s' timed out during startup.", name)
            except Exception as exc:  # noqa: BLE001 - genuinely want to catch anything here
                self.server_status[name] = {"connected": False, "error": str(exc)[:500]}
                logger.warning("Failed to connect to MCP server '%s': %s", name, exc)

        model_name = os.environ.get("OPENAI_MODEL", "gpt-5")
        base_url = os.environ.get("OPENAI_BASE_URL") or None
        llm = ChatOpenAI(
            model=model_name,
            api_key=os.environ.get("OPENAI_API_KEY", "placeholder-until-configured"),
            base_url=base_url,
            timeout=90,
        )

        self.agent = create_react_agent(
            llm,
            all_tools,
            prompt=SYSTEM_PROMPT,
            checkpointer=self.checkpointer,
        )
        logger.info(
            "Agent ready. %d/%d MCP servers connected, %d tools total.",
            sum(1 for s in self.server_status.values() if s.get("connected")),
            len(connections),
            len(all_tools),
        )


state = AgentState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await state.initialize()
    yield


app = FastAPI(title="CSCI 599 A1 Tool-Using Agent", lifespan=lifespan)


class ChatRequest(BaseModel):
    query: str
    session_id: str


class ChatResponse(BaseModel):
    response: str


@app.get("/")
async def root() -> dict[str, str]:
    return {"status": "ok", "service": "csci599-a1-agent"}


@app.get("/test", response_class=FileResponse, include_in_schema=False)
async def browser_test() -> FileResponse:
    """A same-origin browser client for the existing chat and health APIs."""
    return FileResponse(Path(__file__).parent / "web_test.html", media_type="text/html")


@app.get("/health")
async def health() -> dict[str, Any]:
    """Not required by the assignment spec, but useful for verifying MCP
    connectivity and catching misconfiguration before/after deployment."""
    return {
        "llm_configured": state.llm_configured,
        "mcp_servers": state.server_status,
    }


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    if state.agent is None:
        return JSONResponse(
            status_code=503,
            content={"response": "Agent is not initialized yet. Try again shortly."},
        )
    if not req.query or not req.query.strip():
        return JSONResponse(status_code=400, content={"response": "query must not be empty."})
    if not req.session_id or not req.session_id.strip():
        return JSONResponse(status_code=400, content={"response": "session_id must not be empty."})

    config = {"configurable": {"thread_id": req.session_id}}
    session_token = active_session_id.set(req.session_id)
    logger.info("Agent request session_id=%s", req.session_id)

    try:
        result = await asyncio.wait_for(
            state.agent.ainvoke(
                {"messages": [{"role": "user", "content": req.query}]},
                config=config,
            ),
            timeout=AGENT_INVOKE_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        logger.warning("Agent invocation timed out for session_id=%s", req.session_id)
        return JSONResponse(
            status_code=504,
            content={"response": f"The agent took longer than {AGENT_INVOKE_TIMEOUT_SECONDS}s to respond. Please try again."},
        )
    except Exception:  # noqa: BLE001
        # Failure modes 2 & 3: a tool executed but raised, or the LLM/MCP layer
        # returned something the graph couldn't parse. Either way, the service
        # must respond, not crash or leak a raw stack trace to the caller.
        logger.exception("Agent invocation failed for session_id=%s", req.session_id)
        return JSONResponse(
            status_code=500,
            content={
                "response": (
                    "Something went wrong while processing that request. "
                    "The error has been logged; please try again."
                ),
            },
        )
    finally:
        active_session_id.reset(session_token)

    try:
        final_message = result["messages"][-1]
        content = final_message.content
        if isinstance(content, list):
            # Some providers return content as a list of blocks; flatten to text.
            content = "".join(
                block.get("text", "") if isinstance(block, dict) else str(block) for block in content
            )
    except (KeyError, IndexError, AttributeError, TypeError):
        logger.exception("Could not parse agent result for session_id=%s", req.session_id)
        return JSONResponse(
            status_code=500,
            content={"response": "The agent returned an unexpected result shape."},
        )

    logger.info("Agent completed session_id=%s", req.session_id)
    return {"response": content}


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last-resort safety net so no request can crash the process or return a
    bare, unhandled 500 with a stack trace."""
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"response": "Internal server error."})


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
