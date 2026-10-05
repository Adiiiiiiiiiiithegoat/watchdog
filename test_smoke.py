"""Smoke test: watchdog-mcp tools work in-process. Run: .venv\\Scripts\\python test_smoke.py"""
import asyncio

from fastmcp import Client

import watchdog_mcp.server as s


async def main():
    async with Client(s.mcp) as c:
        tools = {t.name for t in await c.list_tools()}
        assert tools == {"run_claude_code", "kill_run", "check_status", "check_limits"}, tools

        status = (await c.call_tool("check_status", {})).data
        assert status["ok"] and "cpu_pct" in status["result"], status

        bad = (await c.call_tool("run_claude_code",
                                 {"prompt": "x", "project_path": "Q:\\does\\not\\exist"})).data
        assert not bad["ok"] and "does not exist" in bad["error"], bad

        missing = (await c.call_tool("kill_run", {"run_id": "nope"})).data
        assert not missing["ok"], missing
    print("smoke OK:", sorted(tools))


asyncio.run(main())
