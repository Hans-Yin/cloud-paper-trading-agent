#!/usr/bin/env python3
"""Build a reviewed source bundle; never copies credentials or account snapshots."""
import copy
import json
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import engine as e

FILES = ["engine.py", "config.json", "Dockerfile", "render.yaml", ".dockerignore", "cloud/runner.py",
         "cloud/server.py", "cloud/adapter.py", "cloud/entrypoint.sh", "cloud/codex-config.toml",
         "cloud/decision.schema.json", "cloud/AGENT_RUNBOOK.md", "cloud/README.md",
         "cloud/migration/paper_state.json", "cloud/migration/manifest.json",
         "cloud/gcp/README.md", "cloud/gcp/install.sh", "cloud/gcp/paper-cloud.service"]


def prepare(archive):
    with e.locked(ROOT):
        cfg = e.config(ROOT)
        state = e.read_json(ROOT / "data" / "paper_state.json")
        if not state:
            raise ValueError("Missing source ledger")
        seed = copy.deepcopy(state)
        seed["last_real_account"] = {}
        seed["last_ai_summary"] = "等待在云端重新验证 Robinhood 连接。"
        e.write_json(ROOT / "cloud" / "migration" / "paper_state.json", seed)
        manifest = {"generated_at": datetime.now(e.UTC).isoformat(), "mode": "paper",
                    "source_revision": state["revision"], "state_digest": e.digest(seed), "config_digest": e.digest(cfg),
                    "trade_count": len(state["trades"]), "position_count": len(state["positions"]),
                    "account_and_credential_caches_included": False, "cutover_completed": False}
        e.write_json(ROOT / "cloud" / "migration" / "manifest.json", manifest)
    archive = Path(archive)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as stream:
        for item in FILES:
            stream.write(ROOT / item, item)
    return {"archive": str(archive), "file_count": len(FILES), "source_revision": state["revision"], "mode": "paper"}


if __name__ == "__main__":
    print(json.dumps(prepare(sys.argv[1]), ensure_ascii=False))
