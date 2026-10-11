"""Convert READ-ONLY MCP data to the paper-engine snapshot, never orders."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine as e


def normalize(packet, req):
    asof = packet["asof"]
    e.timestamp(asof)
    account = packet["account"]
    suffix = account.get("account_suffix")
    if (set(account) != {"account_suffix", "state", "account_type"}
            or not isinstance(suffix, str) or len(suffix) != 4 or not suffix.isascii() or not suffix.isdigit()):
        raise ValueError("Persist only the account suffix and non-secret account metadata")
    if account["state"] != "active":
        raise ValueError("Account is inactive")
    portfolio, approvals = packet["portfolio"], packet["approvals"]
    snapshot = {"source": "robinhood_mcp", "status": "ok", "asof": asof,
                "real_account": {"connected": True, "account_suffix": account["account_suffix"],
                    "state": account["state"], "value": portfolio["total_value"],
                    "cash": portfolio["cash"], "buying_power": portfolio["buying_power"]["buying_power"],
                    "approvals_on": approvals["human_must_approve_trades"],
                    "approval_updated_at": approvals.get("updated_at"), "checked_at": asof},
                "quotes": {x["quote"]["symbol"]: x["quote"] for x in packet["quotes"]["results"] if x and x.get("quote")},
                "intraday": {x["symbol"]: x.get("bars") or [] for x in packet.get("intraday", {}).get("results", []) if x}}
    cache = req.get("daily_cache")
    if cache:
        snapshot["daily_cache_ref"] = req["daily_cache_digest"]
    else:
        tradability = {}
        for item in packet["tradability"]["results"]:
            eligibility = next((x for x in (item.get("account_type_tradabilities") or [])
                                if x["account_type"] == account["account_type"]), None)
            tradability[item["symbol"]] = bool(item["state"] == "active" and item["tradeable"]
                and eligibility is not None and eligibility["account_type_tradability"] == "tradable")
        earnings = {}
        for symbol in req["symbols"]:
            future = sorted((x for x in packet["earnings"][symbol]["results"] if x and x.get("eps", {}).get("actual") is None
                             and x.get("report", {}).get("date", "") >= req["local_date"]), key=lambda x: x["report"]["date"])
            report = future[0]["report"] if future else {}
            earnings[symbol] = {"date": report.get("date"), "verified": report.get("verified", False), "checked_at": asof}
        cache = {"session_date": req["daily_session_date"], "checked_date": req["local_date"], "checked_at": asof,
                 "bars": {x["symbol"]: x.get("bars") or [] for x in packet["daily"]["results"] if x},
                 "tradability": tradability, "earnings": earnings}
        snapshot["daily_cache"] = cache
    snapshot["tradability"], snapshot["earnings"] = cache["tradability"], cache["earnings"]
    return snapshot


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--job", required=True, type=Path)
    args = parser.parse_args()
    req = e.read_json(args.job / "requirements.json")
    snap = normalize(e.read_json(args.job / "reads.json"), req)
    e.validate_snapshot_clock(snap, e.config(args.root))
    e.write_json(args.job / "snapshot.json", snap)
    resolved = e.resolve_snapshot_cache(args.root, snap)
    state = e.read_json(args.root / "data" / "paper_state.json")
    if not state:
        raise ValueError("Missing ledger; do not create a new balance")
    plan = e.PaperEngine(e.config(args.root), state).prepare(resolved)
    e.write_json(args.job / "plan.json", plan)
    print(json.dumps({k: plan[k] for k in ("plan_id", "status", "candidates", "forced_exits", "eligible_sells", "warnings", "blocks", "equity", "exposure")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
