"""Deterministic model decisions; tool execution and checkpointing stay real."""

import asyncio
from collections.abc import Callable
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

import main


class ScenarioModel(BaseChatModel):
    decide: Callable[[list[BaseMessage]], AIMessage]
    seen: list[list[BaseMessage]] = Field(default_factory=list)
    bound_tool_names: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self):
        return "local-scenario-model"

    def bind_tools(self, tools, **kwargs):
        self.bound_tool_names = [tool.name for tool in tools]
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(list(messages))
        return ChatResult(generations=[ChatGeneration(message=self.decide(messages))])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        await asyncio.sleep(0)  # Let competing requests overlap at the model boundary.
        return self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def tool_call(name: str, args: dict[str, Any], call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def install_graph(model, tools=()):
    main.state.agent = main.create_react_agent(
        model, list(tools), prompt=main.SYSTEM_PROMPT, checkpointer=main.state.checkpointer,
    )
    return main.state.agent
