# TODO

Tracking follow-up tasks for the APIM MCP/A2A lab.

## Pin `agent-framework-core` after first successful build
`agent-framework-core` is currently **unpinned** (it's a pre-release/beta package, so
published versions can shift between builds). Once a build + run succeeds, capture the
exact working version and pin it for reproducible builds.

- Affected files:
  - `labs/mcp-a2a-agents/src/mcp_maf_servers/src/requirements.txt`
  - `labs/mcp-a2a-agents/src/a2a_servers/a2a_maf_agent/requirements.txt`
- How to find the working version (inside the built container or a venv):
  ```bash
  pip show agent-framework-core
  ```
- Then change `agent-framework-core` to `agent-framework-core==<version>`.

## Validate the MAF + FastMCP rewrite at runtime
The Semantic Kernel → Microsoft Agent Framework / FastMCP migration (commit `abf967b`)
passed only static checks (Python syntax + Bicep). It has not been run end-to-end.

- Build the `maf-agent-mcp-server` image (Lab 2 `az acr build` cell).
- Deploy and confirm the `ask_weather` MCP tool responds through APIM.
- Run the standalone notebooks' server + client cells locally.
