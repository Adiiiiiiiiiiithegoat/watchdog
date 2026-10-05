# Watchdog

Discord-controlled OS agent. Local Ollama does the reasoning (mcp-use), MCP servers do the work.
The only real product here is `watchdog_mcp/server.py` — a supervised Claude Code runner with
stuck detection, auto-restart, resource logging, heartbeats, and usage pre-flight.

## Setup

```powershell
python -m venv .venv; .venv\Scripts\pip install -r requirements.txt
copy config.example.yaml config.yaml   # fill in token + your Discord user ID + webhook URL
ollama pull llama3.1:8b
setx OLLAMA_KEEP_ALIVE 30m
```

Node 16+ needed (`npx ccusage` is called for limits). Create a channel webhook
(channel settings → Integrations → Webhooks) and paste it into `config.yaml` —
that's how run progress gets posted without the MCP server needing a bot token.

## Phase 0 — go/no-go (do this before anything else)

```powershell
pip install mcp-client-for-ollama
ollmcp --servers-json mcp_config.json --model llama3.1:8b
```

Ask it "check status". If the 8B model can't call `check_status` reliably here,
it won't from Discord either — tune temperature/tool descriptions before continuing.

## Run

```powershell
.venv\Scripts\python bot.py
```

- Freeform messages (from allowlisted user IDs only) → agent → tools.
- `!limits` — 5h-window usage. `!cancel` — abort the agent loop.
- `!push <repo> <branch> [commit msg]` — git push with ✅/❌ reaction confirmation.
  Deliberately a bot command, not an agent tool: mcp-use has no tool-interception
  hook, so keeping it out of the model's toolbox is the stronger guardrail.
- Usage alerts DM you at 75%/90% of the 5h window (checked every 15 min).

Agent tools (from watchdog-mcp): `run_claude_code`, `kill_run`, `check_status`, `check_limits`.

## Phase 4 — adding the npx servers later

Paste into `mcp_config.json` when ready; each adds 20–35 tools, which will sink an
8B model — pass `disallowed_tools=[...]` to `MCPAgent` in `bot.py` to trim the list first.

```json
"terminator": { "command": "npx", "args": ["-y", "terminator-mcp-agent@latest"] },
"playwright": { "command": "npx", "args": ["-y", "@playwright/mcp@latest",
                "--user-data-dir", "D:\\watchdog\\browser_profile"] },
"filesystem": { "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem",
                "D:\\projects", "D:\\watchdog\\temp"] }
```

## Known ceilings

- Claude runs are children of the MCP server: restarting the bot kills active runs.
- Agent memory is shared across channels (fine for a single-user bot).
- The 5h-limit % is ccusage's estimate from your largest historical block, Claude Code usage only.
