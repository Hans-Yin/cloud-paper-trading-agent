#!/bin/bash
# Run only after reviewing an unpacked bundle on a fresh, dedicated Debian 12 VM.
set -euo pipefail
if [[ "$(uname -s)" != "Linux" || "$(id -u)" != "0" ]]; then
    echo 'Run with sudo on the new dedicated Linux VM; this script does not create GCP resources.' >&2
    exit 1
fi
task_source="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
task_install=/opt/ai-short-term-paper
task_storage=/var/lib/ai-short-term-paper
task_env=/etc/ai-short-term-paper.env
if [[ -e "$task_install" || -e "$task_storage" || -e "$task_env" ]] || id papercloud >/dev/null 2>&1; then
    echo 'Existing installation or user found. Stop for migration review; never reset a ledger.' >&2
    exit 1
fi
python3 - "$task_source" <<'PY'
import json,sys
from pathlib import Path
root=Path(sys.argv[1])
sys.path.insert(0,str(root))
import engine as e
cfg=e.config(root)
seed=e.read_json(root/'cloud/migration/paper_state.json')
manifest=e.read_json(root/'cloud/migration/manifest.json')
if (not seed or seed.get('mode')!='paper' or seed.get('last_real_account')
        or e.digest(seed)!=manifest['state_digest'] or e.digest(cfg)!=manifest['config_digest']):
    raise SystemExit('Bundle migration verification failed')
PY
apt-get update
apt-get install -y --no-install-recommends python3 nodejs npm ca-certificates tzdata
npm install --global @openai/codex@0.162.1
codex --version
useradd --system --create-home --home-dir /home/papercloud --shell /bin/bash papercloud
install -d -m 0755 "$task_install/cloud/migration" "$task_install/cloud/gcp"
for task_file in engine.py config.json cloud/runner.py cloud/server.py cloud/adapter.py \
    cloud/codex-config.toml cloud/decision.schema.json cloud/AGENT_RUNBOOK.md \
    cloud/migration/paper_state.json cloud/migration/manifest.json; do
    install -o root -g root -m 0644 "$task_source/$task_file" "$task_install/$task_file"
done
install -d -o papercloud -g papercloud -m 0700 "$task_storage"
install -o root -g root -m 0644 "$task_source/cloud/gcp/paper-cloud.service" /etc/systemd/system/ai-short-term-paper.service
install -o root -g root -m 0600 /dev/null "$task_env"
cat > "$task_env" <<'ENV'
PAPER_CLOUD_STORAGE=/var/lib/ai-short-term-paper
CLOUD_RUN_ENABLED=0
CLOUD_BIND_HOST=127.0.0.1
PORT=10000
TZ=America/New_York
ENV
runuser -u papercloud -- env PAPER_CLOUD_STORAGE="$task_storage" python3 "$task_install/cloud/runner.py" --initialize
systemctl daemon-reload
systemctl enable --now ai-short-term-paper.service
echo 'Installed in STANDBY. Fresh Codex/Robinhood login, real-data probe and cutover are still required.'
