#!/usr/bin/env python3
"""Local paper trading and research engine. No broker order or network APIs."""
from __future__ import annotations

import argparse
import contextlib
import copy
import fcntl
import hashlib
import html
import json
import math
import os
import statistics
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = timezone.utc
ROOT = Path(__file__).resolve().parent
HOLIDAYS = {
    2026: {"01-01", "01-19", "02-16", "04-03", "05-25", "06-19", "07-03", "09-07", "11-26", "12-25"},
    2027: {"01-01", "01-18", "02-15", "03-26", "05-31", "06-18", "07-05", "09-06", "11-25", "12-24"},
}
EARLY_CLOSES = {2026: {"11-27", "12-24"}, 2027: {"11-26"}}


def timestamp(value: str) -> datetime:
    # Python accepts the nanosecond timestamps returned by the trading tools.
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timestamp must include a timezone")
    return result.astimezone(UTC)


def number(value, *, positive=False) -> float:
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0):
        raise ValueError("Invalid numeric market data")
    return result


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def read_json(path: Path, default=None):
    return json.loads(path.read_text()) if path.exists() else copy.deepcopy(default)


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextlib.contextmanager
def locked(root: Path):
    (root / "data").mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / "data" / ".lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def config(root=ROOT):
    cfg = read_json(root / "config.json")
    if cfg["mode"] != "paper" or cfg["broker_execution"] != "disabled":
        raise ValueError("This build supports PAPER ONLY. Live execution cannot be enabled by changing config.")
    for key in ("initial_capital", "max_exposure", "max_position_value", "risk_per_trade", "daily_loss_review"):
        number(cfg[key], positive=True)
    if not 0 < number(cfg["loss_review_pct"]) < 100:
        raise ValueError("Invalid loss review percentage")
    if cfg["max_exposure"] > cfg["initial_capital"] or cfg["max_position_value"] > cfg["max_exposure"]:
        raise ValueError("Exposure exceeds configured capital")
    if not 1 <= cfg["max_positions"] <= 10 or not 1 <= len(cfg["symbols"]) <= 9:
        raise ValueError("Invalid universe or position limit")
    return cfg


def session(day: date, cfg):
    if day.year not in cfg["calendar_years"] or day.year not in HOLIDAYS:
        raise ValueError("Exchange calendar requires updating for this year")
    if day.weekday() >= 5 or day.strftime("%m-%d") in HOLIDAYS[day.year]:
        return None
    close = time(13) if day.strftime("%m-%d") in EARLY_CLOSES.get(day.year, set()) else time(16)
    return datetime.combine(day, time(9, 30), ET), datetime.combine(day, close, ET)


def next_session(day: date, cfg):
    while True:
        day += timedelta(days=1)
        if session(day, cfg):
            return day


def previous_session(day: date, cfg):
    while True:
        day -= timedelta(days=1)
        if session(day, cfg):
            return day


def session_age(entry: date, current: date, cfg):
    count = 0
    while entry < current:
        entry += timedelta(days=1)
        if session(entry, cfg):
            count += 1
    return count


def initial_state(cfg):
    return {"version": 1, "revision": 0, "mode": "paper", "cash": cfg["initial_capital"],
            "positions": {}, "trades": [], "processed_cycles": [], "paused": False,
            "pause_reason": None, "day": None, "day_start_equity": cfg["initial_capital"],
            "daily_buys": 0, "last_bought_session": {}, "last_checked": None}


def requirements(root=ROOT):
    cfg = config(root)
    now = datetime.now(UTC)
    local = now.astimezone(ET)
    hours = session(local.date(), cfg)
    active = bool(hours and hours[0] <= local < hours[1])
    cache = read_json(root / "data" / "daily_cache.json", {})
    expected = previous_session(local.date(), cfg)
    daily_ready = cache.get("session_date") == expected.isoformat() and cache.get("checked_date") == local.date().isoformat()
    name = now.strftime("%Y%m%dT%H%M%S%fZ")
    state = read_json(root / "data" / "paper_state.json", {})
    return {"asof": now.isoformat(), "local_date": local.date().isoformat(), "market_open": active,
            "needs_bootstrap": not bool(state.get("last_real_account")),
            "next_session": next_session(local.date(), cfg).isoformat(),
            "symbols": cfg["symbols"], "all_symbols": cfg["symbols"] + [cfg["benchmark"]],
            "daily_start": (local.date() - timedelta(days=140)).isoformat() + "T00:00:00Z",
            "intraday_start": hours[0].astimezone(UTC).isoformat() if hours else None,
            "daily_cache": {k: v for k, v in cache.items() if k != "bars"} if daily_ready else None,
            "daily_cache_digest": digest(cache) if daily_ready else None,
            "daily_session_date": expected.isoformat(),
            "snapshot_path": str(root / "data" / "snapshots" / (name + ".json"))}


def normalize_quote(quote, asof, cfg):
    if quote.get("state") != "active" or not quote.get("has_traded"):
        raise ValueError("Instrument is not actively traded")
    age = (asof - timestamp(quote["venue_last_trade_time"])).total_seconds()
    if not -10 <= age <= cfg["quote_max_age_seconds"]:
        raise ValueError("Regular-hours trade quote is stale")
    bid, ask = number(quote["bid_price"], positive=True), number(quote["ask_price"], positive=True)
    for field in ("venue_bid_time", "venue_ask_time"):
        age = (asof - timestamp(quote[field])).total_seconds()
        if not -10 <= age <= cfg["quote_max_age_seconds"]:
            raise ValueError("Bid/ask quote is stale")
    if ask < bid or 10000 * (ask - bid) / ((ask + bid) / 2) > cfg["max_spread_bps"]:
        raise ValueError("Bid/ask spread exceeds the limit")
    return {"bid": bid, "ask": ask, "mark": bid, "last": number(quote["last_trade_price"], positive=True)}


def daily_stats(bars, day, cfg):
    # Day bars from this MCP use UTC midnight as the session-date label.
    # Filter on that label, not ET conversion, and exclude the current session.
    rows = [b for b in bars if b and not b.get("interpolated") and date.fromisoformat(b["begins_at"][:10]) < day]
    rows.sort(key=lambda b: b["begins_at"])
    if len(rows) < 50:
        raise ValueError("At least 50 completed daily bars are required")
    if date.fromisoformat(rows[-1]["begins_at"][:10]) != previous_session(day, cfg):
        raise ValueError("Daily history is stale")
    closes = [number(b["close_price"], positive=True) for b in rows]
    true_ranges = []
    for i in range(1, len(rows)):
        high, low = number(rows[i]["high_price"], positive=True), number(rows[i]["low_price"], positive=True)
        if high < low:
            raise ValueError("Invalid daily bar")
        true_ranges.append(max(high - low, abs(high - closes[i - 1]), abs(low - closes[i - 1])))
    return {"sma20": statistics.mean(closes[-20:]), "sma50": statistics.mean(closes[-50:]),
            "atr14": statistics.mean(true_ranges[-14:]), "close": closes[-1],
            "high5": max(number(b["high_price"]) for b in rows[-5:])}


def intraday_stats(bars, asof, cfg):
    local_day = asof.astimezone(ET).date()
    hours = session(local_day, cfg)
    rows = [b for b in bars if b and not b.get("interpolated") and b.get("session", "reg") == "reg"
            and timestamp(b["begins_at"]).astimezone(ET).date() == local_day
            and hours[0] <= timestamp(b["begins_at"]).astimezone(ET)
            and timestamp(b["begins_at"]) + timedelta(minutes=5) <= asof]
    rows.sort(key=lambda b: b["begins_at"])
    if not rows:
        raise ValueError("No completed intraday bars")
    if (asof - timestamp(rows[-1]["begins_at"]) - timedelta(minutes=5)).total_seconds() > 600:
        raise ValueError("Intraday bars are stale")
    opening = [b for b in rows if timestamp(b["begins_at"]).astimezone(ET) < hours[0] + timedelta(minutes=30)]
    volume = sum(number(b["volume"]) for b in rows)
    if volume <= 0 or len(opening) != 6:
        raise ValueError("Opening range or volume is incomplete")
    vwap = sum((number(b["high_price"]) + number(b["low_price"]) + number(b["close_price"])) / 3
               * number(b["volume"]) for b in rows) / volume
    preceding = rows[max(0, len(rows) - 21):-1]
    avg_volume = statistics.mean(number(b["volume"]) for b in preceding) if preceding else 0
    return {"vwap": vwap, "opening_high": max(number(b["high_price"]) for b in opening),
            "last_close": number(rows[-1]["close_price"]),
            "volume_ratio": number(rows[-1]["volume"]) / avg_volume if avg_volume else 0,
            "after_opening_range": timestamp(rows[-1]["begins_at"]).astimezone(ET) >= hours[0] + timedelta(minutes=30),
            "rows": rows}


def earnings_safe(info, day, cfg):
    if not info or not info.get("checked_at") or not info.get("date"):
        return False
    checked = timestamp(info["checked_at"]).astimezone(ET).date()
    if checked != day:
        return False
    last = day
    for _ in range(cfg["max_holding_sessions"]):
        last = next_session(last, cfg)
    # Tentative dates are treated just as conservatively as verified dates.
    return date.fromisoformat(info["date"]) > last


class PaperEngine:
    def __init__(self, cfg, state):
        self.cfg = cfg
        self.state = copy.deepcopy(state)

    def prepare(self, snap):
        cfg, state = self.cfg, self.state
        now = timestamp(snap["asof"])
        local = now.astimezone(ET)
        hours = session(local.date(), cfg)
        open_now = bool(hours and hours[0] <= local < hours[1])
        plan = {"cycle_id": digest(snap), "plan_id": digest([snap, state["revision"]]), "asof": snap["asof"],
                "mode": "paper", "market_open": open_now, "candidates": [], "forced_exits": [],
                "warnings": [], "blocks": {}, "marks": {}, "quotes": {}, "eligible_sells": [],
                "real_account": snap.get("real_account", {}), "equity": state["cash"], "exposure": 0,
                "loss_review_floor": round(cfg["initial_capital"] * (1 - cfg["loss_review_pct"] / 100), 2)}
        if snap.get("source") != "robinhood_mcp":
            raise ValueError("Snapshot must explicitly identify its data source")
        if snap.get("status") == "error":
            plan["warnings"].append("DATA_COLLECTION_FAILED")
            plan["collection_error"] = snap.get("error", "unknown error")
            open_now = plan["market_open"] = False
        for symbol, position in state["positions"].items():
            plan["marks"][symbol] = position["last_mark"]
        if open_now:
            for symbol in set(cfg["symbols"] + [cfg["benchmark"]] + list(state["positions"])):
                try:
                    q = normalize_quote(snap.get("quotes", {})[symbol], now, cfg)
                    plan["quotes"][symbol] = q
                    if symbol in state["positions"]:
                        plan["marks"][symbol] = q["mark"]
                except (KeyError, ValueError, TypeError) as exc:
                    plan["blocks"][symbol] = str(exc)
            if any(s not in plan["quotes"] for s in state["positions"]):
                plan["warnings"].append("POSITION_MARK_UNAVAILABLE")
        for symbol, position in state["positions"].items():
            plan["exposure"] += position["quantity"] * plan["marks"][symbol]
        plan["equity"] = round(state["cash"] + plan["exposure"], 4)
        plan["exposure"] = round(plan["exposure"], 4)
        plan["daily_reference"] = plan["equity"] if state["day"] != local.date().isoformat() else state["day_start_equity"]
        account = plan["real_account"]
        if account.get("state") != "active" or not account.get("connected"):
            plan["warnings"].append("BROKER_CONNECTION_NOT_CONFIRMED")
        elif number(account.get("value", 0)) <= plan["loss_review_floor"]:
            plan["warnings"].append("REAL_ACCOUNT_LOSS_REVIEW")
        if plan["equity"] <= plan["loss_review_floor"]:
            plan["warnings"].append("PAPER_ACCOUNT_LOSS_REVIEW")
        if plan["daily_reference"] - plan["equity"] >= cfg["daily_loss_review"]:
            plan["warnings"].append("DAILY_LOSS_REVIEW")
        if state["paused"]:
            plan["warnings"].append("MANUAL_REVIEW_REQUIRED")
        if not open_now:
            plan["status"] = "DATA_ERROR" if snap.get("status") == "error" else "MARKET_CLOSED"
            return plan
        for symbol, pos in state["positions"].items():
            if symbol not in plan["quotes"]:
                continue
            q = plan["quotes"][symbol]
            plan["eligible_sells"].append(symbol)
            recent = [b for b in snap.get("intraday", {}).get(symbol, []) if b and not b.get("interpolated")
                      and timestamp(b["begins_at"]) >= timestamp(pos["opened_at"])
                      and timestamp(b["begins_at"]) > timestamp(pos.get("last_observed_at", pos["opened_at"]))
                      and timestamp(b["begins_at"]) + timedelta(minutes=5) <= now]
            stop_touched = q["bid"] <= pos["stop"] or any(number(b["low_price"]) <= pos["stop"] for b in recent)
            target_touched = q["bid"] >= pos["target"] or any(number(b["high_price"]) >= pos["target"] for b in recent)
            reason = None
            if stop_touched:
                reason = "stop"
            elif target_touched:
                reason = "target"
            elif pos["style"] == "intraday" and local >= hours[1] - timedelta(minutes=cfg["flatten_minutes_before_close"]):
                reason = "intraday_close"
            elif pos["style"] == "intraday" and date.fromisoformat(pos["entry_session"]) < local.date():
                reason = "missed_intraday_close"
            elif session_age(date.fromisoformat(pos["entry_session"]), local.date(), cfg) >= cfg["max_holding_sessions"] - 1:
                reason = "holding_time_limit"
            if reason:
                plan["forced_exits"].append({"symbol": symbol, "reason": reason})
        start = datetime.combine(local.date(), time.fromisoformat(cfg["strategy_start"]), ET)
        last_entry = min(datetime.combine(local.date(), time.fromisoformat(cfg["last_entry"]), ET), hours[1] - timedelta(minutes=60))
        if plan["warnings"] or not start <= local <= last_entry:
            plan["status"] = "RISK_REVIEW" if plan["warnings"] else "OUTSIDE_ENTRY_WINDOW"
            return plan
        if not snap.get("daily_cache") or snap["daily_cache"].get("session_date") != previous_session(local.date(), cfg).isoformat():
            plan["warnings"].append("DAILY_CACHE_UNAVAILABLE")
            plan["status"] = "DATA_ERROR"
            return plan
        bars = snap["daily_cache"].get("bars", {})
        try:
            market = daily_stats(bars[cfg["benchmark"]], local.date(), cfg)
            benchmark = plan["quotes"][cfg["benchmark"]]
            market_ok = benchmark["last"] > market["sma20"]
        except (KeyError, ValueError, TypeError) as exc:
            plan["warnings"].append("BENCHMARK_UNAVAILABLE")
            plan["status"] = "DATA_ERROR"
            return plan
        if not market_ok:
            plan["status"] = "NO_MARKET_TREND"
            return plan
        for symbol in cfg["symbols"]:
            if symbol in state["positions"] or symbol not in plan["quotes"]:
                continue
            try:
                if not snap["tradability"].get(symbol, False):
                    raise ValueError("Account tradability not confirmed")
                if not earnings_safe(snap.get("earnings", {}).get(symbol), local.date(), cfg):
                    raise ValueError("Earnings date missing, stale, or within the holding window")
                daily = daily_stats(bars[symbol], local.date(), cfg)
                intra = intraday_stats(snap.get("intraday", {}).get(symbol, []), now, cfg)
                q = plan["quotes"][symbol]
                if not q["last"] > daily["sma20"] > daily["sma50"]:
                    raise ValueError("Trend filter not met")
                style = None
                if intra["after_opening_range"] and intra["last_close"] > intra["opening_high"] and q["last"] > intra["vwap"] and intra["volume_ratio"] >= 1.2:
                    style = "intraday"
                elif intra["last_close"] > daily["high5"] and q["last"] > intra["vwap"] and intra["volume_ratio"] >= 1.2:
                    style = "swing"
                if not style:
                    raise ValueError("No completed-bar breakout with volume confirmation")
                entry = q["ask"] * (1 + cfg["paper_slippage_bps"] / 10000)
                stop_distance = max(daily["atr14"] * (0.6 if style == "intraday" else 1.5), entry * 0.01)
                available = min(cfg["max_position_value"], cfg["max_exposure"] - plan["exposure"], state["cash"] - cfg["paper_fee_per_order"])
                modeled_stop_loss = stop_distance + (entry - stop_distance) * cfg["paper_slippage_bps"] / 10000
                quantity = min(math.floor(available / entry), math.floor((cfg["risk_per_trade"] - cfg["paper_fee_per_order"] * 2) / modeled_stop_loss))
                if quantity < 1:
                    raise ValueError("One whole share does not fit the position or risk budget")
                if state["last_bought_session"].get(symbol) == local.date().isoformat():
                    raise ValueError("Symbol has already been bought this session")
                round_trip_fee_per_share = cfg["paper_fee_per_order"] * 2 / quantity
                target = (entry + cfg["minimum_reward_risk"] * (modeled_stop_loss + round_trip_fee_per_share) + round_trip_fee_per_share) / (1 - cfg["paper_slippage_bps"] / 10000)
                plan["candidates"].append({"symbol": symbol, "style": style, "quantity": quantity,
                                           "entry": round(entry, 4), "stop": round(entry - stop_distance, 4),
                                           "target": round(target, 4),
                                           "planned_loss": round(quantity * modeled_stop_loss + 2 * cfg["paper_fee_per_order"], 4),
                                           "volume_ratio": round(intra["volume_ratio"], 3),
                                           "score": round(intra["volume_ratio"], 3),
                                           "reason": "Completed-bar breakout, rising daily trend, above VWAP"})
            except (KeyError, ValueError, TypeError, ZeroDivisionError) as exc:
                plan["blocks"][symbol] = str(exc)
        plan["candidates"].sort(key=lambda x: (-x["score"], x["symbol"]))
        plan["status"] = "CANDIDATES" if plan["candidates"] else "NO_SIGNAL"
        return plan

    def apply(self, snap, decisions):
        plan = self.prepare(snap)
        state, cfg = self.state, self.cfg
        if plan["cycle_id"] in state["processed_cycles"]:
            return state, {"status": "DUPLICATE_CYCLE", "trades": [], "plan": plan}
        if decisions.get("plan_id") != plan["plan_id"]:
            raise ValueError("Plan changed or snapshot does not match: re-run prepare")
        if set(decisions) - {"plan_id", "actions", "summary"}:
            raise ValueError("Unknown AI decision fields")
        actions = decisions.get("actions", [])
        if not isinstance(actions, list) or len(actions) > 10:
            raise ValueError("Invalid AI actions")
        candidates = {x["symbol"]: x for x in plan["candidates"]}
        seen = set()
        for action in actions:
            if not isinstance(action, dict) or set(action) - {"symbol", "action", "reason"}:
                raise ValueError("AI may select actions, not change size, price, or risk limits")
            symbol = action.get("symbol")
            if symbol not in set(cfg["symbols"]) | set(state["positions"]) or not isinstance(action.get("reason", ""), str):
                raise ValueError("AI action contains an unknown symbol or invalid rationale")
            if symbol in seen or action.get("action") not in {"buy", "sell", "hold"}:
                raise ValueError("Duplicate or invalid AI action")
            seen.add(symbol)
            if action["action"] == "buy" and symbol not in candidates:
                raise ValueError("AI tried to buy outside the eligible candidate list")
            if action["action"] == "sell" and symbol not in plan["eligible_sells"]:
                raise ValueError("AI tried to sell an unavailable paper position")
        day = timestamp(snap["asof"]).astimezone(ET).date().isoformat()
        if state["day"] != day:
            state.update(day=day, day_start_equity=plan["equity"], daily_buys=0)
        trades = []

        def sell(symbol, reason, ai_reason=""):
            if symbol not in state["positions"]:
                return
            pos = state["positions"][symbol]
            quote = plan["quotes"][symbol]
            price = quote["bid"]
            if reason == "stop":
                price = min(price, pos["stop"])
            elif reason == "target":
                price = min(price, pos["target"])
            price *= 1 - cfg["paper_slippage_bps"] / 10000
            fee = cfg["paper_fee_per_order"]
            state["cash"] = round(state["cash"] + pos["quantity"] * price - fee, 6)
            trades.append({"asof": snap["asof"], "symbol": symbol, "side": "sell", "quantity": pos["quantity"],
                           "price": round(price, 6), "fee": fee,
                           "realized_pnl": round((price - pos["entry"]) * pos["quantity"] - pos["entry_fee"] - fee, 6),
                           "reason": reason, "ai_reason": str(ai_reason)[:1000], "mode": "paper"})
            del state["positions"][symbol]

        for item in plan["forced_exits"]:
            sell(item["symbol"], item["reason"])
        for action in actions:
            if action["action"] == "sell":
                sell(action["symbol"], "ai_exit", action.get("reason", ""))
        severe = [x for x in plan["warnings"] if x in {"REAL_ACCOUNT_LOSS_REVIEW", "PAPER_ACCOUNT_LOSS_REVIEW", "DAILY_LOSS_REVIEW"}]
        if severe:
            state["paused"], state["pause_reason"] = True, severe[0]
        # Recompute equity after exits: modeled costs can push it below a floor.
        post_exit_equity = state["cash"] + sum(pos["quantity"] * plan["marks"][symbol] for symbol, pos in state["positions"].items())
        if post_exit_equity <= plan["loss_review_floor"] or state["day_start_equity"] - post_exit_equity >= cfg["daily_loss_review"]:
            state["paused"], state["pause_reason"] = True, "POST_EXIT_LOSS_REVIEW"
        for action in actions:
            if action["action"] != "buy" or state["paused"] or plan["warnings"] or not plan["market_open"]:
                continue
            if len(state["positions"]) >= cfg["max_positions"] or state["daily_buys"] >= cfg["max_new_positions_per_day"]:
                continue
            item = candidates[action["symbol"]]
            exposure = sum(pos["quantity"] * plan["quotes"].get(symbol, {}).get("mark", pos["last_mark"]) for symbol, pos in state["positions"].items())
            available = min(cfg["max_position_value"], cfg["max_exposure"] - exposure, state["cash"] - cfg["paper_fee_per_order"])
            quantity = min(item["quantity"], math.floor(available / item["entry"]))
            if quantity < 1:
                continue
            entry, fee = item["entry"], cfg["paper_fee_per_order"]
            state["cash"] = round(state["cash"] - quantity * entry - fee, 6)
            state["positions"][item["symbol"]] = {"quantity": quantity, "entry": entry, "entry_fee": fee,
                    "stop": item["stop"], "target": item["target"], "style": item["style"],
                    "opened_at": snap["asof"], "entry_session": day, "last_mark": plan["quotes"][item["symbol"]]["mark"],
                    "last_observed_at": snap["asof"]}
            state["daily_buys"] += 1
            state["last_bought_session"][item["symbol"]] = day
            trades.append({"asof": snap["asof"], "symbol": item["symbol"], "side": "buy", "quantity": quantity,
                           "price": entry, "fee": fee, "reason": item["style"],
                           "ai_reason": str(action.get("reason", ""))[:1000], "mode": "paper"})
        for symbol, pos in state["positions"].items():
            if symbol in plan["quotes"]:
                pos["last_mark"] = plan["marks"].get(symbol, plan["quotes"][symbol]["mark"])
                pos["last_observed_at"] = snap["asof"]
        state["trades"].extend(trades)
        state["revision"] += 1
        state["processed_cycles"].append(plan["cycle_id"])
        state["last_checked"] = snap["asof"]
        state["last_status"] = plan["status"]
        state["last_warnings"] = plan["warnings"]
        state["last_real_account"] = plan["real_account"]
        state["last_ai_summary"] = str(decisions.get("summary", ""))[:2000]
        return state, {"status": plan["status"], "trades": trades, "plan": plan}


def render_report(root, cfg, state):
    equity = state["cash"] + sum(p["quantity"] * p["last_mark"] for p in state["positions"].values())
    floor = cfg["initial_capital"] * (1 - cfg["loss_review_pct"] / 100)
    checked = state.get("last_checked")
    checked = timestamp(checked).astimezone(ET).strftime("%Y-%m-%d %H:%M:%S 美东") if checked else "尚未运行"
    schedule = read_json(root / "data" / "automation.json", {})
    e = html.escape
    rows = "".join(f"<tr><td>{e(s)}</td><td>{p['quantity']}</td><td>${p['entry']:.2f}</td><td>${p['last_mark']:.2f}</td><td>${p['stop']:.2f}</td><td>${p['target']:.2f}</td></tr>" for s, p in state["positions"].items())
    trades = "".join(f"<tr><td>{e(timestamp(t['asof']).astimezone(ET).strftime('%m-%d %H:%M'))}</td><td>{e(t['symbol'])}</td><td>{'模拟买入' if t['side']=='buy' else '模拟卖出'}</td><td>{t['quantity']}</td><td>${t['price']:.2f}</td><td>{e(t['reason'])}</td></tr>" for t in reversed(state["trades"][-30:]))
    real = state.get("last_real_account", {})
    summary = {"mode": "paper", "paper_equity": round(equity, 2), "paper_cash": round(state["cash"], 2),
               "paper_pnl": round(equity - cfg["initial_capital"], 2), "real_account_value": real.get("value"),
               "paused": state["paused"], "pause_reason": state["pause_reason"], "last_checked": state.get("last_checked"),
               "status": state.get("last_status", "NOT_STARTED"), "warnings": state.get("last_warnings", []),
               "trade_count": len(state["trades"]), "position_count": len(state["positions"]), "loss_review_floor": floor,
               "scheduler_status": schedule.get("status", "NOT_CONFIGURED")}
    write_json(root / "status.json", summary)
    document = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>800 美元 AI 短线试验</title>
<style>body{{margin:0;background:#f4f7f8;color:#17323b;font:16px/1.6 system-ui,-apple-system,sans-serif}}main{{max-width:1050px;margin:auto;padding:36px 22px}}h1{{font-size:30px;margin:0}}.badge{{display:inline-block;background:#d4efea;color:#126556;padding:4px 12px;border-radius:18px}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:16px;margin:24px 0}}.card,section{{background:white;border:1px solid #dce5e7;border-radius:16px;padding:20px;margin-bottom:18px}}.value{{font-size:30px;font-weight:650}}.muted{{color:#62777e}}table{{width:100%;border-collapse:collapse;font-size:14px}}th,td{{text-align:left;padding:10px 6px;border-bottom:1px solid #edf0f1}}.note{{background:#fff6de;border:1px solid #f0d990;padding:14px;border-radius:12px}}.scroll{{overflow:auto}}h2{{font-size:20px}}code{{overflow-wrap:anywhere}}</style>
<main><span class="badge">模拟交易 · 实盘执行关闭</span><h1>800 美元 AI 短线试验</h1><p class="muted">最近检查：{e(checked)}。这是已保存的快照；刷新页面可查看最近一次生成的结果。</p>
<div class="grid"><div class="card">模拟账户总值<div class="value">${equity:.2f}</div></div><div class="card">模拟收益／亏损<div class="value">${equity-cfg['initial_capital']:+.2f}</div></div><div class="card">模拟可用现金<div class="value">${state['cash']:.2f}</div></div><div class="card">亏损复核线<div class="value">${floor:.2f}</div></div></div>
<section><h2>当前运行状态</h2><p>策略状态：<code>{e(state.get('last_status','NOT_STARTED'))}</code>　定时任务：<code>{e(schedule.get('status','NOT_CONFIGURED'))}</code></p><p>真实账户最近读取的总值：{('$'+format(float(real['value']),'.2f')) if real.get('value') is not None else '尚未读取'}。模拟账本与真实账户分别记录。</p><p>{e(state.get('last_ai_summary','等待首次分析。'))}</p><p>风险提醒：{e(', '.join(state.get('last_warnings',[])) or '当前没有新的风险提醒')}</p></section>
<section><h2>模拟持仓</h2><div class="scroll"><table><tr><th>股票</th><th>股数</th><th>模拟买入价</th><th>估值价</th><th>止损触发价</th><th>止盈触发价</th></tr>{rows or '<tr><td colspan="6">当前没有模拟持仓</td></tr>'}</table></div></section>
<section><h2>最近模拟交易</h2><div class="scroll"><table><tr><th>美东时间</th><th>股票</th><th>操作</th><th>股数</th><th>模拟成交价</th><th>原因</th></tr>{trades or '<tr><td colspan="6">尚无模拟成交</td></tr>'}</table></div></section>
<section><h2>试验规则</h2><p>当天退出或最多持有 {cfg['max_holding_sessions']} 个交易日；模拟总仓位最多 ${cfg['max_exposure']:.0f}，单只最多 ${cfg['max_position_value']:.0f}，最多 {cfg['max_positions']} 只。每笔计划风险最多 ${cfg['risk_per_trade']:.0f}，当日亏损达到 ${cfg['daily_loss_review']:.0f} 时暂停并复核。</p><p>信号基于完成的 K 线、均线趋势、突破、成交量和 VWAP。Codex 可以否决候选，资金与风控由本地程序重新校验。</p></section>
<div class="note">本系统没有发送券商订单的功能，也不会改变交易审批设置。止损是模拟规则，不能保证实盘亏损上限。模拟计入每笔 $0.05 成本和单边 5 个基点价格偏差；这些是假设，真实执行、流动性和费用可能不同。当前构建尚未验证盈利能力。</div></main></html>'''
    (root / "status.html").write_text(document)
    return summary


def validate_snapshot_clock(snap, cfg):
    age = (datetime.now(UTC) - timestamp(snap["asof"])).total_seconds()
    if not -10 <= age <= cfg["snapshot_max_age_seconds"]:
        raise ValueError("Snapshot is old or from the future; collect a new snapshot")


def resolve_snapshot_cache(root, snap):
    if snap.get("daily_cache_ref"):
        cache = read_json(root / "data" / "daily_cache.json", {})
        if digest(cache) != snap["daily_cache_ref"]:
            raise ValueError("Daily cache changed; collect a new snapshot")
        snap = copy.deepcopy(snap)
        snap["daily_cache"] = cache
    return snap


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["requirements", "prepare", "apply", "status", "record-closed"])
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--decisions", type=Path)
    args = parser.parse_args()
    root, cfg = args.root.resolve(), config(args.root.resolve())
    if args.command == "requirements":
        print(json.dumps(requirements(root), ensure_ascii=False))
        return
    with locked(root):
        state = read_json(root / "data" / "paper_state.json", initial_state(cfg))
        if args.command == "record-closed":
            req = requirements(root)
            if req["market_open"]:
                raise ValueError("Market is open; collect a full snapshot")
            state.update(last_checked=req["asof"], last_status="MARKET_CLOSED",
                         last_ai_summary="常规交易时段已结束，等待下一个交易日。")
            state["revision"] += 1
            write_json(root / "data" / "paper_state.json", state)
            result = render_report(root, cfg, state)
        elif args.command == "status":
            result = render_report(root, cfg, state)
        else:
            if not args.snapshot:
                raise ValueError("--snapshot is required")
            snap = resolve_snapshot_cache(root, read_json(args.snapshot))
            validate_snapshot_clock(snap, cfg)
            engine = PaperEngine(cfg, state)
            if args.command == "prepare":
                result = engine.prepare(snap)
                if snap.get("daily_cache") and not any(x in result["warnings"] for x in ["DAILY_CACHE_UNAVAILABLE", "DATA_COLLECTION_FAILED"]):
                    write_json(root / "data" / "daily_cache.json", snap["daily_cache"])
                write_json(root / "data" / "latest_plan.json", result)
            else:
                if not args.decisions:
                    raise ValueError("--decisions is required")
                state, result = engine.apply(snap, read_json(args.decisions))
                if result["status"] != "DUPLICATE_CYCLE":
                    write_json(root / "data" / "paper_state.json", state)
                    with (root / "data" / "audit.jsonl").open("a") as stream:
                        stream.write(json.dumps({"asof": snap["asof"], "cycle_id": result["plan"]["cycle_id"],
                                                 "status": result["status"], "trades": result["trades"],
                                                 "warnings": result["plan"]["warnings"], "summary": read_json(args.decisions).get("summary", "")}, ensure_ascii=False) + "\n")
                result["summary"] = render_report(root, cfg, state)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
