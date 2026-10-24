"""Use the real API/graph, isolated state, and no developer credentials."""

from unittest.mock import patch

import httpx
import pytest

# Importing main normally loads .env. Tests must never read developer secrets.
with patch("dotenv.load_dotenv", return_value=False):
    import main


@pytest.fixture(autouse=True)
def isolated_app(monkeypatch):
    for key in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "TAVILY_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    monkeypatch.setattr(main, "state", main.AgentState())

    def reject_external_model(*args, **kwargs):
        raise AssertionError("Tests must explicitly inject a local model; paid inference is disabled")

    monkeypatch.setattr(main, "ChatOpenAI", reject_external_model)


@pytest.fixture
async def api():
    # ASGITransport deliberately does not run lifespan: each test controls startup.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app, raise_app_exceptions=False),
        base_url="http://agentbridge.test",
    ) as client:
        yield client
