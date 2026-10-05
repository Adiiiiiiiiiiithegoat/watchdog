"""A/B test: does the model use conversation context on an ambiguous follow-up,
instead of pattern-matching the wrong tool? Reproduces the exact failure seen live:
'how much claude usage do I have left' -> 'tell me in percentages'.
Run: .venv\\Scripts\\python test_ab_context.py llama3.1:8b
     .venv\\Scripts\\python test_ab_context.py qwen2.5-coder:7b
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
    agent = MCPAgent(llm=llm, client=client, system_prompt=SYSTEM_PROMPT, max_steps=10, memory_enabled=True)
    try:
        print(f"=== {model} ===")
        for ask in ("how much claude usage do I have left", "tell me in percentages"):
            print(f"\n>>> {ask}")
            print(await agent.run(ask))
    finally:
        await client.close_all_sessions()


asyncio.run(main())
