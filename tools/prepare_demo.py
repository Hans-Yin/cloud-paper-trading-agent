#!/usr/bin/env python3
"""Create an explicitly fictional empty seed; never reset existing state."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import engine as e
from cloud.prepare_bundle import prepare

with e.locked(ROOT):
    ledger = ROOT / 'data/paper_state.json'
    if ledger.exists():
        raise SystemExit('Existing ledger found; refusing to overwrite it with a demo')
    cfg = e.config(ROOT)
    seed = e.initial_state(cfg)
    seed['last_real_account'] = {}
    seed['last_ai_summary'] = 'Fictional empty demo initialization; no broker account is connected.'
    e.write_json(ledger, seed)
    e.render_report(ROOT, cfg, seed)

archive = ROOT / 'paper-demo-bundle.zip'
result = prepare(archive)
print('Prepared fictional demo state and standby bundle:', result['archive'])
print('No broker account was read; do not overwrite an existing installation with this seed.')
