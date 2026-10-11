#!/usr/bin/env python3
"""Run in a fresh job through the native sandbox; contains no financial or auth data."""
import json
import socket
from pathlib import Path

inside = Path.cwd() / 'sandbox-write-check.txt'
outside = Path('/var/lib/ai-short-term-paper/sandbox-outside-sentinel.txt')
assert not outside.exists(), 'Unexpected existing test sentinel'
inside.write_text('paper cloud verification\n')
assert inside.read_text() == 'paper cloud verification\n'
try:
    outside.write_text('outside workspace write unexpectedly allowed\n')
except OSError:
    outside_blocked = True
else:
    outside_blocked = False
    outside.unlink()
try:
    connection = socket.create_connection(('127.0.0.1', 10000), timeout=1)
except OSError:
    network_blocked = True
else:
    network_blocked = False
    connection.close()
assert outside_blocked, 'Sandbox allowed a write outside its job'
assert network_blocked, 'Sandbox allowed direct network access'
print(json.dumps({'job_write_verified': True, 'outside_write_blocked': True,
                  'direct_network_blocked': True}))
