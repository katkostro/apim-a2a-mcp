# ------------------------------------------------------------------------
# a2a_agents.py  – Microsoft Agent Framework (MAF) version
# ------------------------------------------------------------------------
# This module previously hosted a Semantic Kernel agent. It now uses the
# Microsoft Agent Framework (MAF) while keeping the same AbstractAgent
# interface, so the A2A executor / server scaffolding is unchanged.
# ------------------------------------------------------------------------
import abc
import asyncio
import logging
import random
from collections.abc import AsyncIterable
from typing import Any, Callable, Literal

from pydantic import BaseModel

from agent_framework import Agent, MCPStreamableHTTPTool
from agent_framework.openai import OpenAIChatCompletionClient

# ──────────────────────────────────────────────────────────────────────────
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# region Response Format ---------------------------------------------------
class AgentResponse(BaseModel):
    status: Literal["input_required", "completed", "error"] = "input_required"
    message: str
# endregion


# ──────────────────────────────────────────────────────────────────────────
class AbstractAgent(abc.ABC):
    SUPPORTED_CONTENT_TYPES = ["text", "text/plain"]

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    @abc.abstractmethod
    async def invoke(self, user_input: str, session_id: str) -> dict[str, Any]:
        ...


# ──────────────────────────────────────────────────────────────────────────
class MAFAgent(AbstractAgent):
    """Microsoft Agent Framework agent with MCP tools + automatic reconnect."""

    # ------------------------------------------------------------------ #
    # Construction / context-manager
    # ------------------------------------------------------------------ #
    def __init__(
        self,
        mcp_url: str,
        title: str,
        chat_client: OpenAIChatCompletionClient,
        *,
        max_attempts: int = 3,
        base_delay: float = 0.5,
        max_delay: float = 5.0,
    ):
        self._mcp_url = mcp_url.rstrip("/")
        self._title = title
        self._chat_client = chat_client

        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._max_delay = max_delay

        self.mcp_tool: MCPStreamableHTTPTool | None = None
        self.agent: Agent | None = None
        # MAF sessions keyed by A2A context/session id (preserves multi-turn).
        self._sessions: dict[str, Any] = {}

    async def __aenter__(self):
        await self._open_tool_and_agent()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self._close_everything(exc_type, exc, tb)
        return False

    # ------------------------------------------------------------------ #
    # Public API: invoke / stream
    # ------------------------------------------------------------------ #
    async def invoke(
        self, user_input: str, session_id: str
    ) -> dict[str, Any]:
        async def _call():
            session = self._ensure_session(session_id)
            response = await self.agent.run(  # type: ignore[union-attr]
                user_input,
                session=session,
                tools=self.mcp_tool,
                options={"response_format": AgentResponse},
            )
            return self._get_agent_response(response)

        return await self._retry_coro("invoke", _call)

    async def stream(
        self, user_input: str, session_id: str
    ) -> AsyncIterable[dict[str, Any]]:
        # Surface a lightweight progress notice, then run to a structured result.
        async def _stream_call():
            yield {
                "is_task_complete": False,
                "require_user_input": False,
                "content": "Processing the request…",
            }
            session = self._ensure_session(session_id)
            response = await self.agent.run(  # type: ignore[union-attr]
                user_input,
                session=session,
                tools=self.mcp_tool,
                options={"response_format": AgentResponse},
            )
            yield self._get_agent_response(response)

        async for item in self._retry_gen("stream", _stream_call):
            yield item

    # ------------------------------------------------------------------ #
    # Retry helpers (separate for coro vs generator)
    # ------------------------------------------------------------------ #
    async def _retry_coro(self, op_name: str, factory: Callable[[], Any]):
        for attempt in range(1, self._max_attempts + 1):
            try:
                return await factory()
            except (ConnectionError, RuntimeError, OSError) as ex:
                await self._backoff_or_raise(op_name, attempt, ex)

    async def _retry_gen(self, op_name: str, factory: Callable[[], Any]):
        for attempt in range(1, self._max_attempts + 1):
            try:
                async for item in factory():
                    yield item
                return
            except (ConnectionError, RuntimeError, OSError) as ex:
                await self._backoff_or_raise(op_name, attempt, ex)

    async def _backoff_or_raise(self, op_name: str, attempt: int, ex: Exception):
        logger.warning(
            "%s: connection dropped (attempt %d/%d): %s",
            op_name,
            attempt,
            self._max_attempts,
            ex,
        )
        if attempt == self._max_attempts:
            raise
        await self._reconnect_tool()
        delay = min(self._max_delay, self._base_delay * 2 ** (attempt - 1))
        delay *= random.uniform(0.8, 1.2)  # jitter
        await asyncio.sleep(delay)

    # ------------------------------------------------------------------ #
    # Tool / agent (re)initialisation
    # ------------------------------------------------------------------ #
    async def _reconnect_tool(self):
        logger.info("Reconnecting MCP tool for %s…", self._title)
        await self._close_everything(None, None, None)
        await self._open_tool_and_agent()

    async def _open_tool_and_agent(self):
        self.mcp_tool = MCPStreamableHTTPTool(
            name=self._title,
            url=self._mcp_url,
            description=f"{self._title} MCP tools",
        )
        await self.mcp_tool.__aenter__()

        self.agent = Agent(
            client=self._chat_client,
            name=f"{self._title}_agent",
            instructions=(
                f"You are a helpful assistant for {self._title}. "
                "Use the provided tools to answer, breaking the request down "
                "one question at a time."
            ),
        )
        await self.agent.__aenter__()
        logger.info("MCP tool connected (%s).", self._title)

    async def _close_everything(self, exc_type, exc, tb):
        self._sessions.clear()

        if self.agent:
            try:
                await self.agent.__aexit__(exc_type, exc, tb)
            except Exception as err:
                logger.debug("Agent close failed: %s", err)
            self.agent = None

        if self.mcp_tool:
            try:
                await self.mcp_tool.__aexit__(exc_type, exc, tb)
            except Exception as err:
                logger.debug("MCP tool close failed: %s", err)
            self.mcp_tool = None

    # ------------------------------------------------------------------ #
    # Utility helpers
    # ------------------------------------------------------------------ #
    def _ensure_session(self, session_id: str):
        session = self._sessions.get(session_id)
        if session is None:
            session = self.agent.create_session()  # type: ignore[union-attr]
            self._sessions[session_id] = session
        return session

    def _get_agent_response(self, response: Any) -> dict[str, Any]:
        # MAF returns a parsed pydantic instance via ``.value`` when a
        # ``response_format`` is supplied; fall back to raw text otherwise.
        structured = getattr(response, "value", None)
        if not isinstance(structured, AgentResponse):
            text = getattr(response, "text", None) or str(response)
            try:
                structured = AgentResponse.model_validate_json(text)
            except Exception:
                return {
                    "is_task_complete": False,
                    "require_user_input": True,
                    "content": "Unparseable response – please try again.",
                }

        mapping = {
            "input_required": {"is_task_complete": False, "require_user_input": True},
            "error": {"is_task_complete": False, "require_user_input": True},
            "completed": {"is_task_complete": True, "require_user_input": False},
        }
        meta = mapping.get(structured.status)
        return {**meta, "content": structured.message} if meta else {
            "is_task_complete": False,
            "require_user_input": True,
            "content": structured.message,
        }


# ------------------------------------------------------------------------
# End of file
# ------------------------------------------------------------------------
