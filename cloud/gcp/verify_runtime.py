#!/usr/bin/env python3
"""Deployment verification; never reads authentication caches or resets a ledger."""
import hashlib
import json
import subprocess
import time
import urllib.request
from pathlib import Path

root = Path('/var/lib/ai-short-term-paper/system')
ledger = root / 'data/paper_state.json'
before = ledger.read_bytes()
state = json.loads(before)
initial_status = json.loads((root / 'data/cloud_status.json').read_text())
assert initial_status['status'] == 'STANDBY_NOT_ENABLED', 'Restart verification requires standby'
subprocess.run(['systemctl', 'restart', 'ai-short-term-paper.service'], check=True)
healthy = False
for _ in range(20):
    try:
        with urllib.request.urlopen('http://127.0.0.1:10000/healthz', timeout=1) as response:
            healthy = json.load(response).get('process_running') is True
        if healthy:
            break
    except (OSError, ValueError):
        time.sleep(0.25)
after = ledger.read_bytes()
assert healthy, 'Service did not become healthy after restart'
assert before == after, 'Ledger changed during standby restart'
status = json.loads((root / 'data/cloud_status.json').read_text())
assert status['status'] == 'STANDBY_NOT_ENABLED', 'Standby not verified'
print(json.dumps({'service_restart_verified': True, 'ledger_preserved': True,
                  'ledger_sha256': hashlib.sha256(after).hexdigest(),
                  'paper_revision': state['revision'], 'cash': state['cash'],
                  'positions': len(state['positions']), 'trades': len(state['trades']),
                  'status': status['status']}))
