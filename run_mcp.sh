#!/usr/bin/env bash
# GridScope Dynamics MCP server (stdio). MCP clients (Claude Code, Claude Desktop, ...) launch this;
# it speaks the Model Context Protocol on stdin/stdout, so there is nothing to see when run by hand.
# See "MCP server" in README.md.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
if [[ ! -x .venv/bin/python ]]; then
    echo "run_mcp.sh: .venv not found - run ./setup_gridscope.sh first" >&2
    exit 1
fi
exec .venv/bin/python -m gridscope.mcp_server "$@"
