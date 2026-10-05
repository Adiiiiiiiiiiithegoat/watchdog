"""Shared helpers: config, event log, ccusage (Claude usage limits)."""
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
LOGS = ROOT / "logs"
LOGS.mkdir(exist_ok=True)


def load_config() -> dict:
    return yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


def log_event(type_: str, **fields):
    rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "type": type_, **fields}
    with open(LOGS / "events.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, default=str) + "\n")


_cache = (0.0, None)  # ponytail: 5-min in-memory cache; ccusage reparses all transcripts each call


async def ccusage_active_block():
    """Active 5h billing block from `npx ccusage blocks --json`, cached 5 min. None if no active block."""
    global _cache
    ts, block = _cache
    if time.time() - ts < 300:
        return block
    proc = await asyncio.create_subprocess_exec(
        "cmd", "/c", "npx", "-y", "ccusage@latest", "blocks", "--json", "--active", "--token-limit", "max",
        stdin=asyncio.subprocess.DEVNULL,  # inheriting the MCP stdio pipe makes node hang
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(proc.communicate(), timeout=180)
    try:
        # ccusage exits nonzero when usage exceeds the limit — the JSON is still valid
        blocks = json.loads(out).get("blocks", [])
    except json.JSONDecodeError:
        raise RuntimeError(f"ccusage failed: {err.decode(errors='replace')[-500:]}") from None
    block = next((b for b in blocks if b.get("isActive")), None)
    _cache = (time.time(), block)
    return block


def block_summary(block) -> tuple[float | None, str]:
    """Return (percent_used or None, human-readable summary line)."""
    if not block:
        return None, "No active 5-hour block — Claude Code usage is currently at 0%."
    total = block.get("totalTokens") or sum(v for v in (block.get("tokenCounts") or {}).values() if isinstance(v, (int, float)))
    pct = None
    limit = (block.get("tokenLimitStatus") or {}).get("limit")
    if limit:
        pct = 100.0 * total / limit
    resets = ""
    try:
        end = datetime.fromisoformat(str(block["endTime"]).replace("Z", "+00:00"))
        mins = max(0, int((end - datetime.now(timezone.utc)).total_seconds() // 60))
        resets = f" · resets in {mins // 60}h {mins % 60:02d}m"
    except Exception:
        pass
    if pct is not None:
        filled = min(10, int(pct / 10))
        gauge = "▓" * filled + "░" * (10 - filled) + f" {pct:.0f}%  ({total:,} tokens)"
    else:
        gauge = f"{total:,} tokens (no limit estimate yet)"
    return pct, (
        f"5-hour window: {gauge}{resets}\n"
        "Source: local Claude Code logs via ccusage — excludes claude.ai web usage; "
        "limit estimated from your largest historical block."
    )
