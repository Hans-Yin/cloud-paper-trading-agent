#!/usr/bin/env python3
"""Render supervisor: durable ledger, read-only Codex collector, paper execution."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import signal
import subprocess
import sys
import threading
import time
import tomllib
from datetime import datetime
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
import engine as e

READ_TOOLS = {"get_accounts", "get_portfolio", "get_trade_approval_setting", "get_equity_quotes",
              "get_equity_historicals", "get_equity_tradability", "get_earnings_results"}
MCP_URL = "https://agent.robinhood.com/mcp/trading"
STOP = threading.Event()


class CloudError(RuntimeError):
    pass


def health(root, status, **fields):
    value = {"status": status, "mode": "paper", "checked_at": datetime.now(e.UTC).isoformat(), **fields}
    e.write_json(root / "data" / "cloud_status.json", value)
    print(json.dumps({k: value[k] for k in ("status", "mode", "checked_at")}), flush=True)
    return value


def initialize(root: Path, source=SOURCE):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = root / "data" / ".initialized.json"
    ledger = root / "data" / "paper_state.json"
    source_cfg = e.read_json(source / "config.json")
    if marker.exists():
        if not ledger.exists():
            raise CloudError("LEDGER_MISSING")
        if e.read_json(root / "config.json") != source_cfg:
            raise CloudError("CONFIG_CHANGED_REVIEW_REQUIRED")
        e.config(root)
        e.read_json(ledger)
        return
    if ledger.exists() or (root / "config.json").exists():
        raise CloudError("PARTIAL_INITIALIZATION_REVIEW_REQUIRED")
    seed = e.read_json(source / "cloud" / "migration" / "paper_state.json")
    manifest = e.read_json(source / "cloud" / "migration" / "manifest.json")
    if not seed or e.digest(seed) != manifest["state_digest"] or e.digest(source_cfg) != manifest["config_digest"]:
        raise CloudError("MIGRATION_DIGEST_MISMATCH")
    if seed.get("mode") != "paper" or not isinstance(seed.get("positions"), dict):
        raise CloudError("INVALID_MIGRATION")
    e.write_json(root / "config.json", source_cfg)
    e.write_json(ledger, seed)
    e.write_json(marker, {"state_digest": manifest["state_digest"], "initialized_at": datetime.now(e.UTC).isoformat()})
    e.render_report(root, e.config(root), seed)


def validate_codex_config(path):
    cfg = tomllib.loads(path.read_text())
    servers = cfg.get("mcp_servers", {})
    server = servers.get("robinhood-readonly", {})
    if set(servers) != {"robinhood-readonly"} or server.get("url") != MCP_URL:
        raise CloudError("UNEXPECTED_MCP_CONFIGURATION")
    if (set(server.get("enabled_tools", [])) != READ_TOOLS or server.get("required") is not True
            or server.get("enabled") is not True or server.get("default_tools_approval_mode") != "writes"):
        raise CloudError("READ_ONLY_ALLOWLIST_REQUIRED")
    if cfg.get("sandbox_mode") != "workspace-write" or cfg.get("sandbox_workspace_write", {}).get("network_access") is not False:
        raise CloudError("SANDBOX_CONFIGURATION_CHANGED")
    if cfg.get("approval_policy") not in {"on-request", "untrusted"}:
        raise CloudError("APPROVAL_CONFIGURATION_CHANGED")
    if (cfg.get("features", {}).get("apps") is not False
            or cfg.get("apps") != {"_default": {"enabled": False}}):
        raise CloudError("UNEXPECTED_CONNECTED_APP_CONFIGURATION")


@contextlib.contextmanager
def controller_lock(root):
    with (root / "data" / ".controller.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CloudError("ANOTHER_CLOUD_RUN_IS_ACTIVE") from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def scheduled_slot(now, cfg):
    local = now.astimezone(e.ET)
    hours = e.session(local.date(), cfg)
    if not hours or local < hours[0] or local >= hours[1] or not 10 <= local.hour <= 15:
        return None
    minute = local.minute - local.minute % 15
    due = local.replace(minute=minute, second=0, microsecond=0)
    if (local - due).total_seconds() > 120:
        return None
    return due.isoformat()


def call_agent(job, root, source=SOURCE, binary="codex", timeout=240):
    schema = source / "cloud" / "decision.schema.json"
    output = job / "decision.json"
    prompt = (source / "cloud" / "AGENT_RUNBOOK.md").read_text()
    prompt += f"\n实际 --root={root}，--job={job}，adapter 路径={source / 'cloud' / 'adapter.py'}。只向本轮目录写文件。"
    command = [binary, "exec", "--strict-config", "--sandbox", "workspace-write", "--skip-git-repo-check", "--ephemeral",
               "--cd", str(job), "--output-schema", str(schema), "--output-last-message", str(output), "-"]
    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            text=True, start_new_session=True)
    deadline = time.monotonic() + timeout
    first = True
    try:
        while True:
            if STOP.is_set() or time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                proc.communicate(prompt if first else None, timeout=min(1, deadline - time.monotonic()))
                break
            except subprocess.TimeoutExpired:
                first = False
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        raise CloudError("AGENT_TIMEOUT_OR_INTERRUPTED") from None
    if proc.returncode != 0 or not output.exists():
        raise CloudError("AGENT_FAILED_CHECK_LOGIN_OR_QUOTA")
    return e.read_json(output)


def preflight(codex_config, binary="codex"):
    validate_codex_config(codex_config)
    if os.environ.get("OPENAI_API_KEY") or os.environ.get("CODEX_API_KEY"):
        raise CloudError("UNEXPECTED_API_BILLING_CONFIGURATION")
    try:
        result = subprocess.run([binary, "login", "status"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        raise CloudError("CODEX_UNAVAILABLE") from None
    if result.returncode != 0 or "chatgpt" not in (result.stdout + result.stderr).lower():
        raise CloudError("CHATGPT_LOGIN_REQUIRED")


def execute_cycle(root, persistent, agent=call_agent, probe=False):
    with controller_lock(root):
        if STOP.is_set():
            raise CloudError("SERVICE_STOPPING")
        cfg = e.config(root)
        before = e.read_json(root / "data" / "paper_state.json")
        req = e.requirements(root)
        job = persistent / "jobs" / datetime.now(e.UTC).strftime("%Y%m%dT%H%M%S%fZ")
        job.mkdir(parents=True, mode=0o700)
        e.write_json(job / "requirements.json", req)
        decisions = agent(job, root)
        if STOP.is_set():
            raise CloudError("SERVICE_STOPPING")
        snap_path = job / "snapshot.json"
        if not snap_path.is_file() or snap_path.is_symlink() or snap_path.stat().st_size > 4_000_000:
            raise CloudError("SNAPSHOT_MISSING_OR_INVALID")
        snap = e.resolve_snapshot_cache(root, e.read_json(snap_path))
        e.validate_snapshot_clock(snap, cfg)
        actual = snap.get("real_account", {})
        if snap.get("status") != "ok" or not actual.get("connected") or actual.get("state") != "active":
            raise CloudError("ROBINHOOD_READ_NOT_VERIFIED")
        if e.read_json(root / "data" / "paper_state.json") != before or e.config(root) != cfg:
            raise CloudError("LEDGER_OR_CONFIG_CHANGED_DURING_COLLECTION")
        if probe:
            # A readiness probe never creates a paper trade, even during market hours.
            plan = e.PaperEngine(cfg, before).prepare(snap)
            if req["market_open"] and (not plan["quotes"] or "SPY" not in plan["quotes"]):
                raise CloudError("LIVE_QUOTES_NOT_VERIFIED")
            cache = snap.get("daily_cache", {})
            if (cache.get("session_date") != req["daily_session_date"]
                    or cache.get("checked_date") != req["local_date"]):
                raise CloudError("DAILY_CACHE_NOT_VERIFIED")
            for symbol in req["all_symbols"]:
                try:
                    e.daily_stats(cache.get("bars", {})[symbol], e.timestamp(snap["asof"]).astimezone(e.ET).date(), cfg)
                except (KeyError, ValueError, TypeError):
                    raise CloudError("DAILY_HISTORY_NOT_VERIFIED") from None
            if any(s not in snap.get("tradability", {}) or s not in snap.get("earnings", {}) for s in req["symbols"]):
                raise CloudError("ELIGIBILITY_DATA_NOT_VERIFIED")
            e.write_json(root / "data" / "daily_cache.json", cache)
            e.write_json(root / "data" / "cloud_ready.json", {"source": "robinhood_mcp", "checked_at": snap["asof"],
                         "paper_revision": before["revision"], "market_open": req["market_open"]})
            health(root, "PROBE_VERIFIED", scheduled=False)
            return {"status": "PROBE_VERIFIED", "trades": []}
        with e.locked(root):
            state = e.read_json(root / "data" / "paper_state.json")
            state, result = e.PaperEngine(cfg, state).apply(snap, decisions)
            if result["status"] != "DUPLICATE_CYCLE":
                if snap.get("daily_cache"):
                    e.write_json(root / "data" / "daily_cache.json", snap["daily_cache"])
                e.write_json(root / "data" / "paper_state.json", state)
                with (root / "data" / "audit.jsonl").open("a") as stream:
                    stream.write(json.dumps({"asof": snap["asof"], "cycle_id": result["plan"]["cycle_id"],
                        "status": result["status"], "trades": result["trades"], "summary": decisions.get("summary", "")}, ensure_ascii=False) + "\n")
            e.render_report(root, cfg, state)
        health(root, "CYCLE_COMPLETED", trade_count=len(result["trades"]), last_cycle=snap["asof"])
        return result


def supervise(root, persistent, codex_config):
    while not STOP.is_set():
        try:
            initialize(root)
            if os.environ.get("CLOUD_RUN_ENABLED", "0") != "1":
                health(root, "STANDBY_NOT_ENABLED")
            elif not (root / "data" / "cloud_ready.json").exists():
                health(root, "BLOCKED_FIRST_PROBE_REQUIRED")
            else:
                slot = scheduled_slot(datetime.now(e.UTC), e.config(root))
                last = e.read_json(root / "data" / "last_slot.json", {})
                if slot and slot != last.get("slot"):
                    preflight(codex_config)
                    # Persist before dispatch. Restart doesn't repeat a partially completed turn.
                    e.write_json(root / "data" / "last_slot.json", {"slot": slot, "status": "STARTED"})
                    execute_cycle(root, persistent)
                    e.write_json(root / "data" / "last_slot.json", {"slot": slot, "status": "COMPLETED"})
                elif not slot:
                    health(root, "OUTSIDE_SCHEDULE")
        except Exception as exc:
            # Never dump external messages, quotes, credentials or account identifiers to Render logs.
            code = str(exc) if isinstance(exc, CloudError) else "CLOUD_CYCLE_FAILED"
            health(root, code, scheduled=False)
        STOP.wait(60)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--initialize", action="store_true")
    args = parser.parse_args()
    persistent = Path(os.environ.get("PAPER_CLOUD_STORAGE", "/var/data"))
    root = persistent / "system"
    try:
        initialize(root)
        codex_config = persistent / "codex" / "config.toml"
        if args.initialize:
            health(root, "INITIALIZED_NOT_RUNNING")
        elif args.probe or args.once:
            preflight(codex_config)
            execute_cycle(root, persistent, probe=args.probe)
        else:
            supervise(root, persistent, codex_config)
    except Exception as exc:
        code = str(exc) if isinstance(exc, CloudError) else "CLOUD_STARTUP_FAILED"
        health(root, code)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
