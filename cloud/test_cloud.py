"""Durability, failed collection, scheduling and isolation checks; no live service."""
import copy
import os
import sys
import tempfile
import time
import tomllib
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
sys.path.insert(0, str(SOURCE / "cloud"))
import engine as e
import runner as r
import adapter
import server
from test_engine import fixture


NOW = datetime(2026, 10, 12, 14, 35, tzinfo=timezone.utc)


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


class CloudTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.source = self.folder / "source"
        self.persistent = self.folder / "disk"
        self.root = self.persistent / "system"
        self.cfg, self.snap = fixture()
        self.snap["daily_cache"].update(tradability=self.snap["tradability"], earnings=self.snap["earnings"])
        self.seed = e.initial_state(self.cfg)
        self.seed["revision"] = 17
        e.write_json(self.source / "config.json", self.cfg)
        e.write_json(self.source / "cloud" / "migration" / "paper_state.json", self.seed)
        e.write_json(self.source / "cloud" / "migration" / "manifest.json", {
            "state_digest": e.digest(self.seed), "config_digest": e.digest(self.cfg)})
        r.initialize(self.root, self.source)
        self.clock = patch.object(e, "datetime", Clock)
        self.clock.start()
        r.STOP.clear()

    def tearDown(self):
        r.STOP.clear()
        self.clock.stop()
        self.temp.cleanup()

    def ledger(self):
        return e.read_json(self.root / "data" / "paper_state.json")

    def agent(self, snap=None, actions=None):
        snapshot = copy.deepcopy(self.snap if snap is None else snap)
        def collect(job, root):
            e.write_json(job / "snapshot.json", snapshot)
            plan = e.PaperEngine(e.config(root), self.ledger()).prepare(snapshot)
            return {"plan_id": plan["plan_id"], "actions": actions or [], "summary": "Test fixture, not live market data"}
        return collect

    def test_restart_preserves_existing_ledger_and_pause(self):
        state = self.ledger()
        state.update(cash=680, paused=True, pause_reason="PAPER_ACCOUNT_LOSS_REVIEW", revision=18)
        e.write_json(self.root / "data" / "paper_state.json", state)
        r.initialize(self.root, self.source)
        self.assertEqual(self.ledger(), state)

    def test_missing_ledger_never_reseeds_balance(self):
        (self.root / "data" / "paper_state.json").unlink()
        with self.assertRaisesRegex(r.CloudError, "LEDGER_MISSING"):
            r.initialize(self.root, self.source)
        self.assertFalse((self.root / "data" / "paper_state.json").exists())

    def test_partial_initialization_does_not_overwrite(self):
        (self.root / "data" / ".initialized.json").unlink()
        before = self.ledger()
        with self.assertRaisesRegex(r.CloudError, "PARTIAL_INITIALIZATION"):
            r.initialize(self.root, self.source)
        self.assertEqual(self.ledger(), before)

    def test_corrupt_migration_fails_before_writing(self):
        manifest = self.source / "cloud" / "migration" / "manifest.json"
        e.write_json(manifest, {"state_digest": "wrong", "config_digest": e.digest(self.cfg)})
        empty = self.persistent / "new-system"
        with self.assertRaisesRegex(r.CloudError, "MIGRATION_DIGEST"):
            r.initialize(empty, self.source)
        self.assertFalse((empty / "config.json").exists())

    def test_configuration_change_needs_review(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["max_exposure"] = 400
        e.write_json(self.source / "config.json", cfg)
        with self.assertRaisesRegex(r.CloudError, "CONFIG_CHANGED"):
            r.initialize(self.root, self.source)
        self.assertEqual(e.config(self.root), self.cfg)

    def test_no_concurrent_cloud_writers(self):
        with r.controller_lock(self.root):
            with self.assertRaisesRegex(r.CloudError, "ANOTHER_CLOUD_RUN"):
                with r.controller_lock(self.root):
                    self.fail("Concurrent writer entered")

    def test_allowlist_rejects_order_tool_and_disabled_server(self):
        text = (SOURCE / "cloud" / "codex-config.toml").read_text()
        path = self.folder / "codex.toml"
        path.write_text(text)
        r.validate_codex_config(path)
        for invalid in (text.replace('"get_accounts",', '"place_equity_order", "get_accounts",'),
                        text.replace("enabled = true", "enabled = false"),
                        text.replace("network_access = false", "network_access = true")):
            path.write_text(invalid)
            with self.assertRaises(r.CloudError):
                r.validate_codex_config(path)

    def test_unexpected_api_billing_is_rejected(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-value"}):
            with self.assertRaisesRegex(r.CloudError, "UNEXPECTED_API_BILLING"):
                r.preflight(SOURCE / "cloud" / "codex-config.toml")

    def test_connected_apps_cannot_be_reenabled(self):
        text = (SOURCE / "cloud" / "codex-config.toml").read_text()
        path = self.folder / "codex.toml"
        path.write_text(text.replace("apps = false", "apps = true"))
        with self.assertRaisesRegex(r.CloudError, "UNEXPECTED_CONNECTED_APP"):
            r.validate_codex_config(path)

    def test_connected_app_override_is_rejected(self):
        text = (SOURCE / "cloud" / "codex-config.toml").read_text()
        path = self.folder / "codex.toml"
        path.write_text(text + '\n[apps.google_drive]\nenabled = true\n')
        with self.assertRaisesRegex(r.CloudError, "UNEXPECTED_CONNECTED_APP"):
            r.validate_codex_config(path)

    def test_schedule_dst_closed_days_and_no_catchup(self):
        def slot(iso):
            return r.scheduled_slot(datetime.fromisoformat(iso), self.cfg)
        self.assertIn("T10:00:00-04:00", slot("2026-10-12T14:00:00+00:00"))
        self.assertIn("T10:00:00-05:00", slot("2026-11-02T15:00:00+00:00"))
        self.assertIsNone(slot("2026-10-11T14:00:00+00:00"))
        self.assertIsNone(slot("2026-11-26T15:00:00+00:00"))
        self.assertIsNone(slot("2026-11-27T18:00:00+00:00"))
        self.assertIsNone(slot("2026-10-12T14:02:01+00:00"))
        self.assertIsNone(slot("2026-10-12T13:45:00+00:00"))

    def test_probe_checks_data_and_never_changes_ledger(self):
        before = self.ledger()
        result = r.execute_cycle(self.root, self.persistent, self.agent(), probe=True)
        self.assertEqual(result["status"], "PROBE_VERIFIED")
        self.assertEqual(self.ledger(), before)
        self.assertEqual(e.read_json(self.root / "data" / "daily_cache.json"), self.snap["daily_cache"])
        self.assertTrue((self.root / "data" / "cloud_ready.json").exists())

    def test_probe_rejects_incomplete_history(self):
        snap = copy.deepcopy(self.snap)
        snap["daily_cache"]["bars"]["SPY"] = []
        with self.assertRaisesRegex(r.CloudError, "DAILY_HISTORY_NOT_VERIFIED"):
            r.execute_cycle(self.root, self.persistent, self.agent(snap), probe=True)
        self.assertEqual(self.ledger(), self.seed)
        self.assertFalse((self.root / "data" / "cloud_ready.json").exists())

    def test_valid_paper_cycle_persists_and_duplicate_does_not_repeat(self):
        agent = self.agent(actions=[{"symbol": "WMT", "action": "buy", "reason": "fixture signal"}])
        first = r.execute_cycle(self.root, self.persistent, agent)
        self.assertEqual(len(first["trades"]), 1)
        saved = self.ledger()
        r.initialize(self.root, self.source)
        second = r.execute_cycle(self.root, self.persistent, agent)
        self.assertEqual(second["status"], "DUPLICATE_CYCLE")
        self.assertEqual(self.ledger(), saved)
        self.assertEqual(len((self.root / "data" / "audit.jsonl").read_text().splitlines()), 1)

    def test_stale_snapshot_and_missing_connection_never_write(self):
        stale = copy.deepcopy(self.snap)
        stale["asof"] = "2026-10-09T19:00:00+00:00"
        with self.assertRaisesRegex(ValueError, "Snapshot is old"):
            r.execute_cycle(self.root, self.persistent, self.agent(stale))
        disconnected = copy.deepcopy(self.snap)
        disconnected["real_account"]["connected"] = False
        with self.assertRaisesRegex(r.CloudError, "ROBINHOOD_READ_NOT_VERIFIED"):
            r.execute_cycle(self.root, self.persistent, self.agent(disconnected))
        self.assertEqual(self.ledger(), self.seed)

    def test_failed_agent_and_injected_order_size_never_write(self):
        def failed(*args):
            raise r.CloudError("AGENT_FAILED_CHECK_LOGIN_OR_QUOTA")
        with self.assertRaises(r.CloudError):
            r.execute_cycle(self.root, self.persistent, failed)
        actions = [{"symbol": "WMT", "action": "buy", "quantity": 1000}]
        with self.assertRaisesRegex(ValueError, "not change size"):
            r.execute_cycle(self.root, self.persistent, self.agent(actions=actions))
        self.assertEqual(self.ledger(), self.seed)

    def test_stopping_service_discards_uncommitted_decision(self):
        agent = self.agent(actions=[{"symbol": "WMT", "action": "buy"}])
        def stopping(job, root):
            result = agent(job, root)
            r.STOP.set()
            return result
        with self.assertRaisesRegex(r.CloudError, "SERVICE_STOPPING"):
            r.execute_cycle(self.root, self.persistent, stopping)
        self.assertEqual(self.ledger(), self.seed)

    def test_snapshot_symlink_is_rejected(self):
        target = self.folder / "other.json"
        e.write_json(target, self.snap)
        def linked(job, root):
            (job / "snapshot.json").symlink_to(target)
            return {}
        with self.assertRaisesRegex(r.CloudError, "SNAPSHOT_MISSING_OR_INVALID"):
            r.execute_cycle(self.root, self.persistent, linked)
        self.assertEqual(self.ledger(), self.seed)

    def test_ledger_change_during_collection_is_detected(self):
        agent = self.agent()
        def changed(job, root):
            result = agent(job, root)
            state = self.ledger()
            state["revision"] += 1
            e.write_json(root / "data" / "paper_state.json", state)
            return result
        with self.assertRaisesRegex(r.CloudError, "LEDGER_OR_CONFIG_CHANGED"):
            r.execute_cycle(self.root, self.persistent, changed)
        self.assertEqual(self.ledger()["trades"], [])

    def test_timeout_terminates_collector(self):
        job = self.folder / "job"
        job.mkdir()
        binary = self.folder / "fake-codex"
        binary.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
        binary.chmod(0o700)
        started = time.monotonic()
        with self.assertRaisesRegex(r.CloudError, "AGENT_TIMEOUT_OR_INTERRUPTED"):
            r.call_agent(job, self.root, binary=str(binary), timeout=0.2)
        self.assertLess(time.monotonic() - started, 5)

    def test_adapter_masks_account_and_fails_closed_without_eligibility(self):
        req = e.requirements(self.root)
        quotes = [{"quote": {"symbol": s, **q}} for s, q in self.snap["quotes"].items()]
        packet = {"asof": self.snap["asof"],
            "account": {"account_suffix": "1234", "state": "active", "account_type": "limited_margin"},
            "portfolio": {"total_value": "800", "cash": "800", "buying_power": {"buying_power": "800"}},
            "approvals": {"human_must_approve_trades": False}, "quotes": {"results": quotes},
            "daily": {"results": [{"symbol": s, "bars": bars} for s, bars in self.snap["daily_cache"]["bars"].items()]},
            "tradability": {"results": [{"symbol": s, "state": "active", "tradeable": True,
                "account_type_tradabilities": []} for s in req["symbols"]]},
            "earnings": {s: {"results": [{"eps": {"actual": None}, "report": {"date": "2026-11-19", "verified": False}}]}
                for s in req["symbols"]}}
        snapshot = adapter.normalize(packet, req)
        self.assertEqual(snapshot["real_account"]["account_suffix"], "1234")
        self.assertFalse(snapshot["tradability"]["WMT"])
        packet["account"]["account_suffix"] = "123456789"
        with self.assertRaises(ValueError):
            adapter.normalize(packet, req)

    def supervise_once(self):
        def stop_wait(_):
            r.STOP.set()
        with patch.object(r, "initialize"), patch.object(r.STOP, "wait", side_effect=stop_wait):
            r.supervise(self.root, self.persistent, SOURCE / "cloud" / "codex-config.toml")

    def test_default_standby_never_calls_agent(self):
        with patch.dict(os.environ, {"CLOUD_RUN_ENABLED": "0"}), patch.object(r, "execute_cycle") as execute:
            self.supervise_once()
            execute.assert_not_called()
        self.assertEqual(e.read_json(self.root / "data" / "cloud_status.json")["status"], "STANDBY_NOT_ENABLED")

    def test_scheduled_run_requires_first_verified_probe(self):
        with patch.dict(os.environ, {"CLOUD_RUN_ENABLED": "1"}), patch.object(r, "execute_cycle") as execute:
            self.supervise_once()
            execute.assert_not_called()
        self.assertEqual(e.read_json(self.root / "data" / "cloud_status.json")["status"], "BLOCKED_FIRST_PROBE_REQUIRED")

    def test_restart_does_not_repeat_interrupted_slot(self):
        e.write_json(self.root / "data" / "cloud_ready.json", {"fixture": True})
        with patch.dict(os.environ, {"CLOUD_RUN_ENABLED": "1"}), patch.object(r, "scheduled_slot", return_value="fixed-slot"), \
                patch.object(r, "preflight"), patch.object(r, "execute_cycle", side_effect=r.CloudError("AGENT_TIMEOUT_OR_INTERRUPTED")) as execute:
            self.supervise_once()
            self.assertEqual(execute.call_count, 1)
            self.assertEqual(e.read_json(self.root / "data" / "last_slot.json")["status"], "STARTED")
            r.STOP.clear()
            self.supervise_once()
            self.assertEqual(execute.call_count, 1)
        self.assertEqual(self.ledger(), self.seed)

    def test_cloud_auth_uses_fresh_persistent_directory(self):
        home = self.folder / "home"
        home.mkdir()
        with patch.object(Path, "home", return_value=home), \
                patch.dict(os.environ, {"RENDER_EXTERNAL_URL": "https://example.onrender.com"}):
            config_path = server.prepare_codex(self.persistent)
            self.assertTrue((home / ".codex").is_symlink())
            self.assertIn('mcp_oauth_callback_url = "https://example.onrender.com/callback"', config_path.read_text())
            self.assertEqual(tomllib.loads(config_path.read_text())["mcp_oauth_callback_port"], 5555)
            self.assertEqual(list((self.persistent / "codex").iterdir()), [config_path])
            server.prepare_codex(self.persistent)
        self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)

    def test_gcp_login_uses_loopback_without_public_callback(self):
        home = self.folder / "gcp-home"
        home.mkdir()
        with patch.object(Path, "home", return_value=home), \
                patch.dict(os.environ, {"RENDER_EXTERNAL_URL": "", "CLOUD_BIND_HOST": "127.0.0.1"}):
            path = server.prepare_codex(self.persistent)
            cfg = tomllib.loads(path.read_text())
            self.assertEqual(cfg["mcp_oauth_callback_port"], 5555)
            self.assertNotIn("mcp_oauth_callback_url", cfg)
            self.assertEqual(server.bind_host(), "127.0.0.1")
        with patch.dict(os.environ, {"CLOUD_BIND_HOST": "192.0.2.1"}):
            with self.assertRaisesRegex(r.CloudError, "INVALID_LISTEN_ADDRESS"):
                server.bind_host()

    def test_cloud_does_not_copy_or_replace_existing_credentials(self):
        home = self.folder / "home"
        home.mkdir()
        (home / ".codex").mkdir()
        original = home / ".codex" / "auth.json"
        original.write_text("test-placeholder-not-a-token")
        with patch.object(Path, "home", return_value=home):
            with self.assertRaisesRegex(r.CloudError, "EXISTING_CREDENTIAL_DIRECTORY"):
                server.prepare_codex(self.persistent)
        self.assertEqual(original.read_text(), "test-placeholder-not-a-token")

    def test_public_routes_do_not_expose_ledger_or_reports(self):
        handler = server.Handler.__new__(server.Handler)
        handler.reply = unittest.mock.Mock()
        for path in ("/", "/status.json", "/status.html", "/data/paper_state.json", "/jobs", "/admin"):
            handler.path = path
            handler.do_GET()
            self.assertEqual(handler.reply.call_args.args[0], 404)
        handler.path = "/healthz"
        handler.do_GET()
        self.assertEqual(handler.reply.call_args.args[0], 200)
        self.assertNotIn(b"800", handler.reply.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
