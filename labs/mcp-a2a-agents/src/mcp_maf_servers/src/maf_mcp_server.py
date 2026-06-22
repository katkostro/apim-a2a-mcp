import argparse
import os
from typing import Literal

from agent_framework import Agent, MCPStreamableHTTPTool
from agent_framework.openai import OpenAIChatCompletionClient
from mcp.server.fastmcp import FastMCP

TITLE                     = os.environ.get("TITLE", "Weather")
MCP_URL                   = os.environ.get("MCP_URL", "/weather")
apim_resource_gateway_url = os.environ.get("APIM_GATEWAY_URL", "")
apim_subscription_key     = os.environ.get("APIM_SUBSCRIPTION_KEY", "")  # secret!
inference_api_version     = os.environ.get("OPENAI_API_VERSION", "2025-03-01-preview")
openai_model_name         = os.environ.get("OPENAI_DEPLOYMENT_NAME", "gpt-4o-mini")


def _build_chat_client() -> OpenAIChatCompletionClient:
    """Microsoft Agent Framework chat client routed through APIM."""
    return OpenAIChatCompletionClient(
        model=openai_model_name,
        azure_endpoint=apim_resource_gateway_url,
        api_key=apim_subscription_key,
        api_version=inference_api_version,
    )


async def _ask_agent(question: str, mcp_path: str, title: str) -> str:
    """Run a MAF agent that can call the remote ``{title}`` MCP tools.

    A fresh tool/agent pair is opened per call so the server can run
    statelessly behind APIM / Container Apps.
    """
    remote_url = f"{apim_resource_gateway_url}/{mcp_path}".rstrip("/")

    async with MCPStreamableHTTPTool(
        name=title,
        url=remote_url,
        description=f"Remote {title} MCP tools via Streamable HTTP",
    ) as mcp_tool:
        async with Agent(
            client=_build_chat_client(),
            name=f"{title}Agent",
            instructions=(
                "You are a helpful assistant. "
                f"Use the '{title}' tools when the user asks about {title.lower()}. "
                "Cite the source if appropriate."
            ),
        ) as agent:
            response = await agent.run(question, tools=mcp_tool)
            return getattr(response, "text", None) or str(response)


def build_mcp_server(mcp_path: str, title: str) -> FastMCP:
    """Expose the MAF agent as a Streamable-HTTP MCP server via FastMCP."""
    mcp = FastMCP(
        name=f"{title}_maf_aca",
        stateless_http=True,
        json_response=True,
    )

    @mcp.tool(
        name=f"ask_{title.lower()}",
        description=f"Ask the {title} agent a question; it may call the remote {title} MCP tools.",
    )
    async def ask(question: str) -> str:
        return await _ask_agent(question, mcp_path, title)

    @mcp.prompt(
        name=f"{title}_report_prompt",
        description=f"Create a {title.lower()} report prompt for a given city.",
    )
    def report(city: str) -> str:
        return f"Report in {city}?"

    return mcp


def parse_arguments():
    parser = argparse.ArgumentParser(description="Run the Microsoft Agent Framework MCP server.")
    parser.add_argument(
        "--transport",
        type=str,
        choices=["sse", "stdio", "http"],
        default="http",
        help="Transport method to use (default: http).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=9090,
        help="Port to use for SSE/Streamable HTTP transport (required if transport is 'sse' or 'http').",
    )
    return parser.parse_args()


def main(transport: Literal["sse", "stdio", "http"] = "http", port: int = 9090) -> None:
    mcp = build_mcp_server(mcp_path=MCP_URL, title=TITLE)

    if transport in ("http", "sse"):
        mcp.settings.host = "0.0.0.0"
        mcp.settings.port = port

    transport_map = {"http": "streamable-http", "sse": "sse", "stdio": "stdio"}
    mcp.run(transport=transport_map[transport])


if __name__ == "__main__":
    args = parse_arguments()
    main(transport=args.transport, port=args.port)
