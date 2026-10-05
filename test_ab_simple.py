"""Does the model reliably invoke a tool at all (not just print JSON as text)?
Runs the same simple query 3x to check consistency.
Run: .venv\\Scripts\\python test_ab_simple.py <model>
"""
import asyncio
import sys

from langchain_ollama import ChatOllama
from mcp_use import MCPAgent, MCPClient

from bot import SYSTEM_PROMPT
from common import ROOT, load_config

CFG = load_config()
model = sys.argv[1] if len(sys.argv) > 1 else CFG["ollama"]["model"]


async def main():
    llm = ChatOllama(model=model, base_url=CFG["ollama"]["host"], temperature=0.1, keep_alive="10m")
    client = MCPClient.from_config_file(str(ROOT / "mcp_config.json"))
    for i in range(3):
        agent = MCPAgent(llm=llm, client=client, system_prompt=SYSTEM_PROMPT, max_steps=10, memory_enabled=False)
        result = await agent.run("how much claude usage do I have left")
        print(f"[{model} #{i+1}] {result[:200]}")
    await client.close_all_sessions()


asyncio.run(main())
