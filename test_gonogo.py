"""Phase 0 go/no-go: can the 8B model route a request to the right MCP tool?
Exact same stack as bot.py. Run: .venv\\Scripts\\python test_gonogo.py
"""
import asyncio

from langchain_ollama import ChatOllama
from mcp_use import MCPAgent, MCPClient

from bot import SYSTEM_PROMPT
from common import ROOT, load_config

CFG = load_config()


async def main():
    llm = ChatOllama(model=CFG["ollama"]["model"], base_url=CFG["ollama"]["host"],
                     temperature=0.1, keep_alive=CFG["ollama"].get("keep_alive", "30m"))
    client = MCPClient.from_config_file(str(ROOT / "mcp_config.json"))
    agent = MCPAgent(llm=llm, client=client, system_prompt=SYSTEM_PROMPT,
                     max_steps=CFG["ollama"].get("max_steps", 10))
    try:
        for ask in ("check the status of my machine", "how much claude usage do I have left?"):
            print(f"\n>>> {ask}")
            print(await agent.run(ask))
    finally:
        await client.close_all_sessions()


asyncio.run(main())
