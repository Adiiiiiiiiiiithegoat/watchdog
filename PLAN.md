# Watchdog: plan

## Discord bot
- [x] discord.py bot that answers allowlisted users only
- [x] Local Ollama agent (llama3.1:8b) via mcp-use
- [x] !limits, !cancel and !push behind ✅/❌ reaction confirmation
- [x] Usage alerts at 75% / 90% of the 5h window, checked every 15 min
- [x] Autostart at login through the run_bot.bat restart loop

## watchdog-mcp server
- [x] run_claude_code with stuck detection, auto-restart, heartbeats and resource logging
- [x] kill_run, check_status and check_limits tools
- [x] Phase 0 go/no-go and smoke tests

## Next
- [ ] Raise the run_bot.bat restart delay from 5 s to 60 s (hundreds of restarts while offline)
- [ ] Fix the README Phase 4 snippet: it still points at D:\watchdog
- [ ] Phase 4: add terminator, playwright and filesystem MCP servers, trimmed with disallowed_tools
