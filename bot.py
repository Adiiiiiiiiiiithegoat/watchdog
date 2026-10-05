"""Watchdog Discord bot: freeform messages go to an mcp-use agent on local Ollama.

Risky actions are NOT agent tools — mcp-use has no tool-call interception hook, so
`!push` is a bot command gated by ✅/❌ reactions. The model can't even attempt it.
"""
import asyncio
import subprocess
import sys
from pathlib import Path

# Windows consoles default to cp1252; mcp-use logs emoji, which crashes logging otherwise
for stream in (sys.stdout, sys.stderr):
    stream.reconfigure(encoding="utf-8", errors="replace")

import discord
from discord.ext import commands, tasks
from langchain_ollama import ChatOllama
from mcp_use import MCPAgent, MCPClient

from common import ROOT, block_summary, ccusage_active_block, load_config, log_event

CFG = load_config()
D = CFG["discord"]
OWNERS = set(D["allowed_user_ids"])
CHANNELS = set(D.get("allowed_channel_ids") or [])

SYSTEM_PROMPT = """You control the user's Windows PC through tools, chatting on Discord.
Call ONE tool at a time and wait for its result before deciding the next step.
If a tool fails, read the error and either fix the arguments or tell the user you
cannot proceed — never retry the same call unchanged. Only the tools listed exist;
never invent new tool names. If a follow-up question asks for something a previous
tool result didn't contain (e.g. a percentage that wasn't in the data), say plainly
that you don't have it — do not answer using data from an unrelated tool or topic.
Keep replies short."""

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

agent: MCPAgent | None = None
agent_lock = asyncio.Lock()
current_task: asyncio.Task | None = None
_alerted: set = set()


def authorized(msg: discord.Message) -> bool:
    if msg.author.bot or msg.author.id not in OWNERS:
        return False
    return isinstance(msg.channel, discord.DMChannel) or not CHANNELS or msg.channel.id in CHANNELS


async def send_chunked(channel, text: str):
    text = text or "(empty response)"
    for i in range(0, len(text), 1900):
        await channel.send(text[i:i + 1900])


async def confirm(ctx, text: str) -> bool:
    m = await ctx.send(f"{text}\nReact ✅ to run, ❌ to cancel (120s timeout).")
    for emoji in ("✅", "❌"):
        await m.add_reaction(emoji)

    def check(reaction, user):
        return user.id in OWNERS and reaction.message.id == m.id and str(reaction.emoji) in ("✅", "❌")

    try:
        reaction, _ = await bot.wait_for("reaction_add", timeout=120, check=check)
        return str(reaction.emoji) == "✅"
    except asyncio.TimeoutError:
        return False


@bot.event
async def on_ready():
    global agent
    if agent is None:
        llm = ChatOllama(
            model=CFG["ollama"]["model"],
            base_url=CFG["ollama"]["host"],
            temperature=0.1,  # higher temps make small models hallucinate tool names
            keep_alive=CFG["ollama"].get("keep_alive", "30m"),
        )
        client = MCPClient.from_config_file(str(ROOT / "mcp_config.json"))
        agent = MCPAgent(llm=llm, client=client, system_prompt=SYSTEM_PROMPT,
                         max_steps=CFG["ollama"].get("max_steps", 10), memory_enabled=True)
        usage_alerts.start()
    print(f"logged in as {bot.user}")


@bot.event
async def on_message(msg: discord.Message):
    if not authorized(msg):
        if not msg.author.bot:
            log_event("unauthorized", user=msg.author.id, content=msg.content[:200])
        return
    await bot.process_commands(msg)
    if msg.content.startswith("!"):
        return
    global current_task
    if agent_lock.locked():
        await msg.channel.send("⏳ Busy with the previous request — `!cancel` to abort it.")
        return
    log_event("message", user=msg.author.id, content=msg.content)
    async with agent_lock:
        async with msg.channel.typing():
            current_task = asyncio.ensure_future(agent.run(msg.content))
            try:
                result = str(await current_task)
            except asyncio.CancelledError:
                await msg.channel.send("🚫 Cancelled.")
                return
            except Exception as e:
                result = f"❌ Agent error: {e}"
    log_event("agent_result", content=result[:500])
    await send_chunked(msg.channel, result)


@bot.command()
async def cancel(ctx):
    """Abort the in-flight agent loop."""
    if current_task and not current_task.done():
        current_task.cancel()
        await ctx.send("Cancelling…")
    else:
        await ctx.send("Nothing running.")


@bot.command()
async def limits(ctx):
    """Claude usage in the current 5h window."""
    async with ctx.channel.typing():
        try:
            _, line = block_summary(await ccusage_active_block())
        except Exception as e:
            line = f"❌ ccusage failed: {e}"
    await ctx.send(f"📊 Claude Usage\n{line}")


@bot.command()
async def push(ctx, repo: str, branch: str, *, message: str = ""):
    """!push <repo_path> <branch> [commit message] — stage, commit, push. Needs ✅."""
    repo_path = Path(repo)
    if not (repo_path / ".git").exists():
        await ctx.send(f"❌ Not a git repo: `{repo}`")
        return
    if branch.startswith("-") or not branch.replace("/", "").replace(".", "").replace("_", "").replace("-", "").isalnum():
        await ctx.send("❌ Invalid branch name.")
        return
    allowlist = {Path(p).resolve() for p in (CFG.get("git") or {}).get("push_main_allowlist") or []}
    if branch.lower() in ("main", "master") and repo_path.resolve() not in allowlist:
        await ctx.send("❌ Refusing to push to main/master — repo not in `git.push_main_allowlist`.")
        return
    if not await confirm(ctx, f"⚠️ **git push**\nrepo: `{repo_path.resolve()}`\nbranch: `{branch}`\n"
                              f"commit: `{message or '(none — push existing commits only)'}`"):
        await ctx.send("🚫 Denied or timed out.")
        return

    def git(*args):
        return subprocess.run(["git", "-C", str(repo_path), *args], capture_output=True, text=True)

    output = []
    if message:
        await asyncio.to_thread(git, "add", "-A")
        commit = await asyncio.to_thread(git, "commit", "-m", message)
        output.append(commit.stdout + commit.stderr)
    pushed = await asyncio.to_thread(git, "push", "origin", branch)  # never --force
    output.append(pushed.stdout + pushed.stderr)
    log_event("git_push", repo=str(repo_path), branch=branch, ok=pushed.returncode == 0)
    status = "✅ pushed" if pushed.returncode == 0 else "❌ push failed"
    await send_chunked(ctx.channel, f"{status}\n```\n{''.join(output)[-1600:]}\n```")


@tasks.loop(minutes=15)
async def usage_alerts():
    try:
        block = await ccusage_active_block()
        pct, line = block_summary(block)
    except Exception:
        return
    if not block or pct is None:
        return
    for threshold in (CFG.get("alerts") or {}).get("usage_alert_thresholds") or [75, 90]:
        key = (block.get("id"), threshold)
        if pct >= threshold and key not in _alerted:
            _alerted.add(key)
            user = await bot.fetch_user(next(iter(OWNERS)))
            await user.send(f"🚨 Claude usage crossed {threshold}% of the 5h window.\n{line}")


if __name__ == "__main__":
    bot.run(D["token"])
