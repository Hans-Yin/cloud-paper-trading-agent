import copy
import json
import tempfile
import unittest
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

import engine as e


def fixture():
    cfg = e.config()
    cfg["symbols"] = ["WMT", "XOM"]
    asof = datetime(2026, 10, 12, 14, 35, tzinfo=timezone.utc)
    dates = []
    d = date(2026, 10, 9)
    while len(dates) < 80:
        if e.session(d, cfg):
            dates.append(d)
        d -= timedelta(days=1)
    dates.reverse()
    daily = [{"begins_at": d.isoformat() + "T00:00:00Z", "open_price": str(84 + i * 0.2),
              "close_price": str(84.1 + i * 0.2), "high_price": str(84.5 + i * 0.2),
              "low_price": str(83.8 + i * 0.2), "volume": 1000000, "session": "reg"} for i, d in enumerate(dates)]
    bars = []
    for i in range(13):
        close = 100.1 if i < 6 else 100.3
        high = 100.4 if i < 6 else 100.5
        if i == 12:
            close, high = 101.0, 101.1
        bars.append({"begins_at": (datetime(2026, 10, 12, 13, 30, tzinfo=timezone.utc) + timedelta(minutes=5 * i)).isoformat(),
                     "open_price": str(close - 0.05), "close_price": str(close), "high_price": str(high),
                     "low_price": str(close - 0.2), "volume": 1800 if i == 12 else 1000, "session": "reg"})
    def quote():
        return {"state": "active", "has_traded": True, "bid_price": "101.00", "ask_price": "101.02",
                "last_trade_price": "101.01", "venue_last_trade_time": asof.isoformat(),
                "venue_bid_time": asof.isoformat(), "venue_ask_time": asof.isoformat()}
    snap = {"source": "robinhood_mcp", "status": "ok", "asof": asof.isoformat(),
            "real_account": {"connected": True, "state": "active", "value": "800", "cash": "800"},
            "quotes": {s: quote() for s in ["WMT", "XOM", "SPY"]},
            "intraday": {s: copy.deepcopy(bars) for s in ["WMT", "XOM", "SPY"]},
            "daily_cache": {"session_date": "2026-10-09", "checked_date": "2026-10-12",
                            "bars": {s: copy.deepcopy(daily) for s in ["WMT", "XOM", "SPY"]}},
            "tradability": {"WMT": True, "XOM": True},
            "earnings": {s: {"date": "2026-11-19", "checked_at": asof.isoformat()} for s in ["WMT", "XOM"]}}
    return cfg, snap


def run(cfg, state, snap, actions):
    paper = e.PaperEngine(cfg, state)
    plan = paper.prepare(snap)
    return paper.apply(snap, {"plan_id": plan["plan_id"], "actions": actions, "summary": "Unit test decisions"})


def advanced(snap, minutes=15, bid=101.0):
    snap = copy.deepcopy(snap)
    now = e.timestamp(snap["asof"]) + timedelta(minutes=minutes)
    snap["asof"] = now.isoformat()
    for q in snap["quotes"].values():
        q.update(bid_price=str(bid), ask_price=str(bid + 0.02), last_trade_price=str(bid + 0.01),
                 venue_last_trade_time=now.isoformat(), venue_bid_time=now.isoformat(), venue_ask_time=now.isoformat())
    for rows in snap["intraday"].values():
        rows.append({"begins_at": (now - timedelta(minutes=5)).isoformat(), "open_price": str(bid),
                     "high_price": str(bid + 0.03), "low_price": str(bid - 0.03), "close_price": str(bid),
                     "volume": 1800, "session": "reg"})
    return snap


class EngineTests(unittest.TestCase):
    def test_cache_reference_verifies_content_and_rejects_changed_data(self):
        _, snap = fixture()
        cache = snap["daily_cache"]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            e.write_json(root / "data" / "daily_cache.json", cache)
            linked = {"daily_cache_ref": e.digest(cache)}
            self.assertEqual(e.resolve_snapshot_cache(root, linked)["daily_cache"], cache)
            cache["session_date"] = "2026-10-08"
            e.write_json(root / "data" / "daily_cache.json", cache)
            with self.assertRaisesRegex(ValueError, "cache changed"):
                e.resolve_snapshot_cache(root, linked)

    def test_candidates_and_two_position_cap(self):
        cfg, snap = fixture()
        state = e.initial_state(cfg)
        plan = e.PaperEngine(cfg, state).prepare(snap)
        self.assertEqual(len(plan["candidates"]), 2)
        self.assertTrue(all(c["planned_loss"] <= cfg["risk_per_trade"] for c in plan["candidates"]))
        state, result = run(cfg, state, snap, [{"symbol": s, "action": "buy", "reason": "valid signal"} for s in cfg["symbols"]])
        self.assertEqual(len(state["positions"]), 2)
        self.assertEqual(len(result["trades"]), 2)
        self.assertLessEqual(sum(p["quantity"] * p["entry"] for p in state["positions"].values()), cfg["max_exposure"])
        self.assertGreaterEqual(state["cash"], 500)

    def test_duplicate_cycle_does_not_trade_twice(self):
        cfg, snap = fixture()
        state, _ = run(cfg, e.initial_state(cfg), snap, [{"symbol": "WMT", "action": "buy"}])
        repeated, result = run(cfg, state, snap, [{"symbol": "WMT", "action": "buy"}])
        self.assertEqual(result["status"], "DUPLICATE_CYCLE")
        self.assertEqual(repeated, state)

    def test_ai_cannot_change_quantity_or_limits(self):
        cfg, snap = fixture()
        with self.assertRaisesRegex(ValueError, "not change size"):
            run(cfg, e.initial_state(cfg), snap, [{"symbol": "WMT", "action": "buy", "quantity": 1000}])

    def test_ai_cannot_buy_unqualified_symbol(self):
        cfg, snap = fixture()
        with self.assertRaises(ValueError):
            run(cfg, e.initial_state(cfg), snap, [{"symbol": "TSLA", "action": "buy"}])

    def test_stale_trade_quote_blocks_entry(self):
        cfg, snap = fixture()
        snap["quotes"]["WMT"]["venue_last_trade_time"] = "2026-10-09T19:59:59Z"
        plan = e.PaperEngine(cfg, e.initial_state(cfg)).prepare(snap)
        self.assertNotIn("WMT", [c["symbol"] for c in plan["candidates"]])
        self.assertIn("stale", plan["blocks"]["WMT"])

    def test_stale_bid_or_wide_spread_blocks_entry(self):
        cfg, snap = fixture()
        snap["quotes"]["WMT"]["venue_bid_time"] = "2026-10-09T19:59:59Z"
        snap["quotes"]["XOM"]["ask_price"] = "120"
        plan = e.PaperEngine(cfg, e.initial_state(cfg)).prepare(snap)
        self.assertEqual(plan["candidates"], [])

    def test_gap_loss_is_not_capped_at_stop(self):
        cfg, snap = fixture()
        state, _ = run(cfg, e.initial_state(cfg), snap, [{"symbol": "WMT", "action": "buy"}])
        stop = state["positions"]["WMT"]["stop"]
        down = advanced(snap, bid=90)
        state, result = run(cfg, state, down, [])
        self.assertEqual(result["trades"][0]["reason"], "stop")
        self.assertLess(result["trades"][0]["price"], stop)
        self.assertLess(result["trades"][0]["realized_pnl"], -8)
        self.assertEqual(state["positions"], {})

    def test_stop_wins_if_both_levels_touched(self):
        cfg, snap = fixture()
        state, _ = run(cfg, e.initial_state(cfg), snap, [{"symbol": "WMT", "action": "buy"}])
        later = advanced(snap)
        later["intraday"]["WMT"][-1].update(low_price="95", high_price="110")
        state, result = run(cfg, state, later, [])
        self.assertEqual(result["trades"][0]["reason"], "stop")
        self.assertLess(result["trades"][0]["realized_pnl"], 0)

    def test_680_floor_pauses_new_entries(self):
        cfg, snap = fixture()
        snap["real_account"]["value"] = "680"
        state, result = run(cfg, e.initial_state(cfg), snap, [])
        self.assertTrue(state["paused"])
        self.assertEqual(result["trades"], [])
        self.assertIn("REAL_ACCOUNT_LOSS_REVIEW", result["plan"]["warnings"])

    def test_daily_loss_pause(self):
        cfg, snap = fixture()
        state = e.initial_state(cfg)
        state.update(day="2026-10-12", day_start_equity=800, cash=783)
        state, result = run(cfg, state, snap, [])
        self.assertTrue(state["paused"])
        self.assertIn("DAILY_LOSS_REVIEW", result["plan"]["warnings"])

    def test_near_earnings_is_excluded_even_if_tentative(self):
        cfg, snap = fixture()
        snap["earnings"]["WMT"].update(date="2026-10-14", verified=False)
        plan = e.PaperEngine(cfg, e.initial_state(cfg)).prepare(snap)
        self.assertNotIn("WMT", [c["symbol"] for c in plan["candidates"]])

    def test_missing_or_stale_earnings_is_excluded(self):
        cfg, snap = fixture()
        snap["earnings"]["WMT"]["date"] = None
        snap["earnings"]["XOM"]["checked_at"] = "2026-10-09T14:00:00Z"
        self.assertEqual(e.PaperEngine(cfg, e.initial_state(cfg)).prepare(snap)["candidates"], [])

    def test_incomplete_future_bar_is_not_used(self):
        cfg, snap = fixture()
        prior = e.intraday_stats(snap["intraday"]["WMT"], e.timestamp(snap["asof"]), cfg)
        snap["intraday"]["WMT"].append({"begins_at": snap["asof"], "close_price": "999", "high_price": "999",
                                            "low_price": "1", "volume": 999999, "session": "reg"})
        after = e.intraday_stats(snap["intraday"]["WMT"], e.timestamp(snap["asof"]), cfg)
        self.assertEqual(prior, after)

    def test_daily_history_excludes_current_session(self):
        cfg, snap = fixture()
        bars = snap["daily_cache"]["bars"]["WMT"]
        prior = e.daily_stats(bars, date(2026, 10, 12), cfg)
        bars.append({"begins_at": "2026-10-12T00:00:00Z", "close_price": "999", "high_price": "999", "low_price": "1"})
        self.assertEqual(prior, e.daily_stats(bars, date(2026, 10, 12), cfg))

    def test_missing_mark_prevents_more_buys(self):
        cfg, snap = fixture()
        state, _ = run(cfg, e.initial_state(cfg), snap, [{"symbol": "WMT", "action": "buy"}])
        later = advanced(snap)
        del later["quotes"]["WMT"]
        plan = e.PaperEngine(cfg, state).prepare(later)
        self.assertIn("POSITION_MARK_UNAVAILABLE", plan["warnings"])
        self.assertEqual(plan["candidates"], [])

    def test_intraday_position_exits_before_close(self):
        cfg, snap = fixture()
        state, _ = run(cfg, e.initial_state(cfg), snap, [{"symbol": "WMT", "action": "buy"}])
        later = advanced(snap, minutes=310)
        self.assertEqual(e.timestamp(later["asof"]).astimezone(e.ET).strftime("%H:%M"), "15:45")
        state, result = run(cfg, state, later, [])
        self.assertEqual(result["trades"][0]["reason"], "intraday_close")
        self.assertEqual(state["positions"], {})

    def test_closed_market_never_creates_candidates(self):
        cfg, snap = fixture()
        snap["asof"] = "2026-10-11T02:30:00Z"
        plan = e.PaperEngine(cfg, e.initial_state(cfg)).prepare(snap)
        self.assertFalse(plan["market_open"])
        self.assertEqual(plan["status"], "MARKET_CLOSED")

    def test_exchange_calendar_and_early_close(self):
        cfg, _ = fixture()
        self.assertIsNone(e.session(date(2026, 11, 26), cfg))
        self.assertEqual(e.session(date(2026, 11, 27), cfg)[1].hour, 13)
        self.assertIsNotNone(e.session(date(2026, 10, 12), cfg))
        with self.assertRaises(ValueError):
            e.session(date(2028, 1, 3), cfg)

    def test_collection_failure_never_buys(self):
        cfg, snap = fixture()
        snap.update(status="error", error="read failed", real_account={"connected": False})
        state, result = run(cfg, e.initial_state(cfg), snap, [])
        self.assertEqual(result["status"], "DATA_ERROR")
        self.assertEqual(state["cash"], 800)
        self.assertEqual(result["trades"], [])

    def test_plan_revision_mismatch_rejected(self):
        cfg, snap = fixture()
        paper = e.PaperEngine(cfg, e.initial_state(cfg))
        with self.assertRaisesRegex(ValueError, "Plan changed"):
            paper.apply(snap, {"plan_id": "wrong", "actions": []})

    def test_live_execution_cannot_be_enabled_in_config(self):
        cfg, _ = fixture()
        with tempfile.TemporaryDirectory() as folder:
            cfg["mode"] = "live"
            root = Path(folder)
            (root / "config.json").write_text(json.dumps(cfg))
            with self.assertRaisesRegex(ValueError, "PAPER ONLY"):
                e.config(root)

    def test_dashboard_escapes_ai_text(self):
        cfg, _ = fixture()
        state = e.initial_state(cfg)
        state["last_ai_summary"] = "<script>alert(1)</script>"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            e.render_report(root, cfg, state)
            page = (root / "status.html").read_text()
            self.assertNotIn("<script>alert(1)</script>", page)
            self.assertIn("&lt;script&gt;", page)


if __name__ == "__main__":
    unittest.main()
