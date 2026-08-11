"""Connection to the MCP servers the chat assistant borrows tools from.

Two transports cover what we need. AWS hosts its Knowledge server over HTTP and asks for
no credentials, so documentation, Well-Architected guidance, and regional availability
come for free. The awslabs pricing server runs locally over stdio and reuses the same AWS
profile as a scan, which is what lets the assistant price an alternative architecture.

A server that will not start is a missing capability, never a failed conversation: the
hub records why and carries on with whatever else answered.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from types import TracebackType

from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, get_default_environment, stdio_client
from pydantic import BaseModel

from finops.agent.types import ToolSpec
from finops.config import McpServer, Settings

logger = logging.getLogger(__name__)

# Doc pages run long, and a single tool result should not crowd out the conversation.
MAX_RESULT_CHARS = 12_000
_SAFE_NAME = re.compile(r"[^a-zA-Z0-9_.:-]")


class McpServerStatus(BaseModel):
    """What happened when we tried to reach one server. Surfaced in the UI."""

    key: str
    description: str = ""
    connected: bool = False
    tool_count: int = 0
    error: str | None = None


class _Route(BaseModel):
    """Where a tool the model can see actually lives."""

    server_key: str
    tool_name: str

    model_config = {"arbitrary_types_allowed": True}


class McpHub:
    """Connects to the configured MCP servers for the duration of one conversation turn.

    Sessions are per-turn rather than long-lived. A held-open stdio child process and an
    HTTP session that expires between questions are both more trouble than reconnecting,
    and connecting costs a fraction of a second next to a model call.
    """

    def __init__(
        self,
        servers: list[McpServer],
        *,
        startup_timeout: float = 30.0,
        tool_timeout: float = 60.0,
        env: dict[str, str] | None = None,
        log_path: Path | None = None,
    ) -> None:
        self._servers = [server for server in servers if server.enabled]
        self._startup_timeout = startup_timeout
        self._tool_timeout = tool_timeout
        self._env = env or {}
        self._log_path = log_path
        self._stack = AsyncExitStack()
        self._clients: dict[str, Client] = {}
        self._routes: dict[str, _Route] = {}
        self._tools: list[ToolSpec] = []
        self.statuses: list[McpServerStatus] = []

    @classmethod
    def from_settings(cls, settings: Settings) -> McpHub:
        env = {}
        if settings.aws_profile:
            env["AWS_PROFILE"] = settings.aws_profile
        env["AWS_REGION"] = settings.billing_region
        return cls(
            settings.mcp_servers if settings.mcp_enabled else [],
            startup_timeout=settings.mcp_startup_timeout_seconds,
            tool_timeout=settings.mcp_tool_timeout_seconds,
            env=env,
            log_path=Path(settings.db_path).parent / "mcp.log",
        )

    async def __aenter__(self) -> McpHub:
        await self._stack.__aenter__()
        for server in self._servers:
            await self._connect(server)
        await self._load_tools()
        return self

    async def __aexit__(self, exc_type, exc: BaseException | None, tb: TracebackType | None):
        self._clients.clear()
        return await self._stack.__aexit__(exc_type, exc, tb)

    @property
    def tools(self) -> list[ToolSpec]:
        return list(self._tools)

    async def call_tool(self, name: str, arguments: dict) -> tuple[str, bool]:
        """Run a tool and return its text plus whether it failed."""
        route = self._routes.get(name)
        if route is None:
            return f"No such tool: {name}", True

        client = self._clients[route.server_key]
        try:
            result = await asyncio.wait_for(
                client.call_tool(route.tool_name, arguments), timeout=self._tool_timeout
            )
        except TimeoutError:
            return f"{name} did not respond within {self._tool_timeout:.0f}s", True
        except Exception as exc:  # noqa: BLE001 - any failure is the model's to work around
            logger.warning("MCP tool %s failed: %s", name, exc)
            return f"{name} failed: {exc}", True

        text = "\n".join(getattr(block, "text", "") for block in (result.content or [])).strip()
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + "\n\n[truncated]"
        return text or "(the tool returned nothing)", bool(result.is_error)

    async def _connect(self, server: McpServer) -> None:
        status = McpServerStatus(key=server.key, description=server.description)
        problem = server.validate_transport()
        if problem:
            status.error = problem
            self.statuses.append(status)
            return

        try:
            client = await asyncio.wait_for(
                self._stack.enter_async_context(
                    # `auto` opens with a discover call that only MCP 2.0 servers know;
                    # the awslabs servers answer it with a wall of validation errors.
                    Client(self._transport(server), mode="legacy")
                ),
                timeout=self._startup_timeout,
            )
        except TimeoutError:
            status.error = f"did not connect within {self._startup_timeout:.0f}s"
        except Exception as exc:  # noqa: BLE001 - unreachable server, missing uvx, bad url
            status.error = str(exc) or type(exc).__name__
        else:
            self._clients[server.key] = client
            status.connected = True

        if status.error:
            logger.warning("MCP server %s unavailable: %s", server.key, status.error)
        self.statuses.append(status)

    def _transport(self, server: McpServer):
        if server.transport == "http":
            return server.url
        return stdio_client(
            StdioServerParameters(
                command=server.command or "",
                args=server.args,
                env={**get_default_environment(), **self._env, **server.env},
            ),
            errlog=self._child_log(),
        )

    def _child_log(self):
        """Child processes are chatty on stderr; keep it out of the server's own log."""
        if self._log_path is None:
            return sys.stderr
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            return self._stack.enter_context(
                self._log_path.open("a", encoding="utf-8", errors="replace")
            )
        except OSError:
            return sys.stderr

    async def _load_tools(self) -> None:
        """Ask every connected server what it can do, and name the results for the model."""
        seen: dict[str, int] = {}
        listings: dict[str, list] = {}
        for key, client in self._clients.items():
            status = next(s for s in self.statuses if s.key == key)
            try:
                result = await asyncio.wait_for(client.list_tools(), timeout=self._startup_timeout)
            except Exception as exc:  # noqa: BLE001
                status.error = f"could not list tools: {exc}"
                status.connected = False
                logger.warning("MCP server %s: %s", key, status.error)
                continue
            listings[key] = list(result.tools)
            status.tool_count = len(listings[key])
            for tool in listings[key]:
                seen[tool.name] = seen.get(tool.name, 0) + 1

        for key, tools in listings.items():
            for tool in tools:
                exposed = tool.name if seen[tool.name] == 1 else f"{key}__{tool.name}"
                exposed = _SAFE_NAME.sub("_", exposed)[:128]
                self._routes[exposed] = _Route(server_key=key, tool_name=tool.name)
                self._tools.append(
                    ToolSpec(
                        name=exposed,
                        description=(tool.description or "").strip(),
                        input_schema=tool.input_schema or {"type": "object"},
                        source=key,
                    )
                )
