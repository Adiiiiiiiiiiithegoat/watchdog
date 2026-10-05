"""watchdog-mcp: supervised Claude Code runner + status/limits tools.

FastMCP server over stdio. Test standalone with ollmcp or MCP Inspector before
wiring into the Discord bot. Discord progress posts go through a channel webhook
(config: discord.webhook_url) so this server needs no bot token.

ponytail: runs die if this server process dies (they are child processes).
Upgrade path: detached spawn + pidfile reattach, only if bot restarts mid-run hurt.
"""
import asyncio
import csv
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
import psutil
from fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import LOGS, block_summary, ccusage_active_block, load_config, log_event

CFG = load_config()
CC = CFG.get("claude_code") or {}
RUN_LOGS = LOGS / "claude_runs"
RUN_LOGS.mkdir(exist_ok=True)
RESOURCE_CSV = LOGS / "resource_log.csv"

mcp = FastMCP("watchdog")
RUNS: dict[str, dict] = {}
BOOT = time.time()


def _err(msg: str) -> dict:
    return {"ok": False, "error": msg}


async def notify(text: str):
    url = (CFG.get("discord", {}).get("webhook_url") or "").strip()
    if not url:
        print(f"[notify] {text}", file=sys.stderr)
        return
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            await c.post(url, json={"content": text[:1990]})
    except Exception as e:
        print(f"[notify failed] {e}", file=sys.stderr)


def alarm():
    try:
        import winsound
        f = (CFG.get("alerts") or {}).get("sound_file") or ""
        if f and Path(f).exists():
            winsound.PlaySound(f, winsound.SND_FILENAME | winsound.SND_ASYNC)
        else:
            for _ in range(3):
                winsound.Beep(1000, 300)
    except Exception:
        pass


def kill_tree(pid: int):
    """Kill claude + descendants; killing only the `cmd /c` wrapper would orphan them."""
    try:
        parent = psutil.Process(pid)
        for child in parent.children(recursive=True):
            try:
                child.kill()
            except psutil.Error:
                pass
        parent.kill()
    except psutil.Error:
        pass


def tree_sample(pid: int) -> tuple[float, int]:
    try:
        procs = [psutil.Process(pid)]
        procs += procs[0].children(recursive=True)
        cpu = sum(p.cpu_percent(None) for p in procs)
        ram = sum(p.memory_info().rss for p in procs) // 2**20
        return round(cpu, 1), ram
    except psutil.Error:
        return 0.0, 0


NVIDIA_SMI = shutil.which("nvidia-smi") or r"C:\Windows\System32\nvidia-smi.exe"
# MCP clients spawn servers with a filtered env; NVML refuses to init without PROGRAMFILES
os.environ.setdefault("PROGRAMFILES", r"C:\Program Files")


def gpu_sample() -> tuple[float | None, float | None]:
    try:
        out = subprocess.run(
            [NVIDIA_SMI, "--query-gpu=utilization.gpu,memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip().splitlines()[0]
        util, mem = (x.strip() for x in out.split(","))
        return float(util), float(mem)
    except Exception:
        return None, None


def log_resources(run_id: str, cpu, ram, gpu_util, gpu_mem):
    new = not RESOURCE_CSV.exists()
    with open(RESOURCE_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["timestamp", "run_id", "cpu_pct", "ram_mb", "gpu_util_pct", "gpu_mem_mb"])
        w.writerow([datetime.now().isoformat(timespec="seconds"), run_id, cpu, ram, gpu_util, gpu_mem])


def tail(path, n: int = 20) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 65536))
            lines = f.read().decode(errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except OSError:
        return ""


async def summarize(log_tail: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=180) as c:
            r = await c.post(CFG["ollama"]["host"] + "/api/chat", json={
                "model": CFG["ollama"]["model"],
                "stream": False,
                "keep_alive": CFG["ollama"].get("keep_alive", "30m"),
                "messages": [{"role": "user", "content":
                    "Summarize in exactly 3 short bullets what this Claude Code run accomplished:\n\n"
                    + log_tail[-4000:]}],
            })
        return r.json()["message"]["content"].strip()
    except Exception:
        return "(summary unavailable — log tail below)\n```\n" + log_tail[-800:] + "\n```"


async def watch(run: dict, proc) -> tuple[str, int | None]:
    """Liveness loop: 30s resource samples, stuck detection, heartbeats. Returns (outcome, exit_code)."""
    stuck_after = CC.get("stuck_threshold_sec", 900)
    beat_every = CC.get("heartbeat_interval_sec", 1800)
    last_size, last_growth, last_beat = 0, time.time(), time.time()
    while True:
        try:
            rc = await asyncio.wait_for(proc.wait(), 30)
            if run["status"] == "killed":
                return "killed", rc
            return ("ok", 0) if rc == 0 else ("crash", rc)
        except asyncio.TimeoutError:
            pass
        cpu, ram = tree_sample(proc.pid)
        gpu_util, gpu_mem = await asyncio.to_thread(gpu_sample)
        log_resources(run["id"], cpu, ram, gpu_util, gpu_mem)
        size = run["log"].stat().st_size if run["log"].exists() else 0
        if size != last_size:
            last_size, last_growth = size, time.time()
        elif time.time() - last_growth > stuck_after:
            kill_tree(proc.pid)
            await proc.wait()
            return "stuck", None
        if time.time() - last_beat >= beat_every:
            last_beat = time.time()
            mins = int((time.time() - run["started"]) / 60)
            await notify(f"💓 `{run['id']}` alive — {mins} min elapsed, cpu {cpu:.0f}%, log {size // 1024} KB")


async def supervise(run: dict):
    rid, max_restarts = run["id"], CC.get("max_restarts", 3)
    for attempt in range(1, max_restarts + 2):
        with open(run["log"], "ab") as fh:
            fh.write(f"===== attempt {attempt} @ {datetime.now():%Y-%m-%d %H:%M:%S} =====\n".encode())
            fh.flush()
            try:
                # cmd /c handles claude being a .cmd shim; prompt goes via stdin to dodge quoting
                proc = await asyncio.create_subprocess_exec(
                    "cmd", "/c", CC.get("binary", "claude"), "--dangerously-skip-permissions", "-p",
                    cwd=run["project"], stdin=asyncio.subprocess.PIPE,
                    stdout=fh, stderr=asyncio.subprocess.STDOUT,
                )
            except Exception as e:
                run["status"] = "failed"
                await notify(f"❌ Could not start Claude Code for `{rid}`: {e}")
                log_event("run_spawn_failed", run_id=rid, error=str(e))
                return
        run["proc"], run["pid"] = proc, proc.pid
        proc.stdin.write(run["prompt"].encode())
        await proc.stdin.drain()
        proc.stdin.close()

        outcome, rc = await watch(run, proc)
        last = tail(run["log"])
        if outcome == "ok":
            run["status"] = "done"
            await notify(f"✅ Run `{rid}` finished in `{run['project']}`.\n{await summarize(last)}")
            alarm()
            log_event("run_done", run_id=rid, attempts=attempt)
            return
        if outcome == "killed":
            await notify(f"🛑 Run `{rid}` killed by user.")
            log_event("run_killed", run_id=rid)
            return
        label = (f"⚠️ stuck — no log growth for {CC.get('stuck_threshold_sec', 900) // 60} min"
                 if outcome == "stuck" else f"❌ crashed (exit {rc})")
        log_event("run_" + outcome, run_id=rid, attempt=attempt, exit_code=rc)
        if attempt <= max_restarts:
            await notify(f"{label} — run `{rid}` attempt {attempt}. Restarting.\n```\n{last[-1500:]}\n```")
        else:
            run["status"] = "failed"
            await notify(f"🛑 Run `{rid}` giving up after {attempt} attempts ({label}).\n```\n{last[-1500:]}\n```")
            alarm()
            return


@mcp.tool
async def run_claude_code(prompt: str, project_path: str, ignore_limits: bool = False) -> dict:
    """Start a supervised, long-running Claude Code coding session in a project directory.
    Use this for ANY task that involves writing, fixing, refactoring, or reviewing code.
    The run happens in the background and can take hours — this returns immediately with
    a run_id, and progress/results are posted to Discord automatically.

    Example: run_claude_code(prompt="add retry logic to the API client", project_path="D:/projects/tameen-rag")
    """
    project = Path(project_path)
    if not project.is_dir():
        return _err(f"project_path does not exist or is not a directory: {project_path}")
    active = [r["id"] for r in RUNS.values() if r["status"] == "running"]
    if active:
        return _err(f"a run is already active ({active[0]}) and max_concurrent is 1 — kill_run it or wait")
    if not ignore_limits:
        try:
            pct, line = block_summary(await ccusage_active_block())
            headroom = CC.get("min_headroom_pct", 20)
            if pct is not None and 100 - pct < headroom:
                await notify(f"⛔ Refused to start a run: less than {headroom}% of the 5h window left.\n{line}")
                return _err(f"refused: only {100 - pct:.0f}% headroom left in the 5-hour usage window. "
                            "Wait for the reset, or call again with ignore_limits=true to override.")
        except Exception as e:
            print(f"[preflight] limit check failed, proceeding: {e}", file=sys.stderr)

    slug = re.sub(r"[^a-z0-9]+", "-", prompt.lower())[:24].strip("-")
    rid = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + slug
    run = {"id": rid, "prompt": prompt, "project": str(project), "log": RUN_LOGS / f"{rid}.log",
           "status": "running", "started": time.time(), "pid": None, "proc": None}
    RUNS[rid] = run
    run["task"] = asyncio.create_task(supervise(run))
    log_event("run_start", run_id=rid, project=str(project), prompt=prompt)
    await notify(f"🚀 Started Claude Code run `{rid}` in `{project}`.\nLog: `{run['log']}`")
    return {"ok": True, "result": {"run_id": rid, "log": str(run["log"])}}


@mcp.tool
async def kill_run(run_id: str) -> dict:
    """Terminate an active Claude Code run. Get run_ids from check_status.

    Example: kill_run(run_id="20260712_031500_fix-tests")
    """
    run = RUNS.get(run_id)
    if not run:
        return _err(f"unknown run_id '{run_id}'. Active runs: "
                    f"{[r for r, v in RUNS.items() if v['status'] == 'running'] or 'none'}")
    if run["status"] != "running":
        return _err(f"run {run_id} is not running (status: {run['status']})")
    run["status"] = "killed"
    kill_tree(run["pid"])
    return {"ok": True, "result": f"killed {run_id}"}


@mcp.tool
async def check_status() -> dict:
    """Report machine and agent status: active Claude Code runs, which Ollama model is
    loaded, GPU utilization/memory, CPU, RAM, and free disk. Takes no arguments.

    Example: check_status()
    """
    runs = [{"run_id": r["id"], "status": r["status"], "project": r["project"],
             "elapsed_min": int((time.time() - r["started"]) / 60),
             "last_log_line": tail(r["log"], 1)} for r in RUNS.values()]
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            models = (await c.get(CFG["ollama"]["host"] + "/api/ps")).json().get("models", [])
        ollama = [m.get("name") for m in models] or "running, no model loaded"
    except Exception as e:
        ollama = f"unreachable ({e})"
    gpu_util, gpu_mem = await asyncio.to_thread(gpu_sample)
    disk = psutil.disk_usage(str(Path(__file__).drive + "\\"))
    return {"ok": True, "result": {
        "claude_runs": runs or "none",
        "ollama_loaded": ollama,
        "gpu_util_pct": gpu_util, "gpu_mem_mb": gpu_mem,
        "cpu_pct": psutil.cpu_percent(0.3), "ram_pct": psutil.virtual_memory().percent,
        "disk_free_gb": round(disk.free / 2**30, 1),
        "watchdog_uptime_min": int((time.time() - BOOT) / 60),
    }}


@mcp.tool
async def check_limits() -> dict:
    """Check Claude usage in the current 5-hour billing window and when it resets.
    Call this before starting long coding runs. Takes no arguments.

    Example: check_limits()
    """
    try:
        _, line = block_summary(await ccusage_active_block())
        return {"ok": True, "result": line}
    except Exception as e:
        return _err(f"ccusage failed: {e}")


if __name__ == "__main__":
    mcp.run()
