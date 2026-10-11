# GCP / Debian installation

This is a generic deployment guide. No existing cloud project, account, billing resource or machine identity is included.

## Provisioning

Provision a dedicated Debian 12 VM in a project you control. The reference deployment used an e2-micro VM and a 20 GB standard persistent disk. Verify current regional availability, capacity and prices in your own account before creating resources; small-machine capacity is not guaranteed for every workload.

Keep the ledger disk persistent and verify the disk-deletion setting. Do not expose financial reports or OAuth callbacks on the public internet. The reference service binds to `127.0.0.1`; restrict SSH to an appropriate administrative source or configure an independently reviewed access method. Cloud costs and trial expiry remain your responsibility.

## Install

Prepare the demo bundle with `python3 tools/prepare_demo.py`, upload it through your own administrative connection and extract it on the fresh VM. After reviewing the installer:

```sh
sudo bash cloud/gcp/install.sh
```

The installer creates `papercloud`, root-owned application source and private persistent storage. It installs the pinned Codex client and enables the service at boot. `CLOUD_RUN_ENABLED=0` keeps new installations in standby. Never run it as an upgrade or ledger-reset procedure.

## Native login

Follow the current official client documentation for a new ChatGPT login. If device login is unavailable, the reference deployment used a temporary SSH forward of port 1455 and the native browser callback:

```sh
ssh -L 127.0.0.1:1455:127.0.0.1:1455 ADMIN_USER@VM_HOST
sudo -u papercloud -H codex login
```

For Robinhood, temporarily forward localhost port 5555 and use the native MCP login:

```sh
ssh -L 127.0.0.1:5555:127.0.0.1:5555 ADMIN_USER@VM_HOST
sudo -u papercloud -H codex mcp login robinhood-readonly
```

Use the actual authorization URL returned by the client and complete login yourself on the official page. Close temporary tunnels after success. Do not publish authorization URLs or copy authentication caches into the repository. The ongoing cloud process does not depend on these tunnels.

## Readiness and cutover

In the service user's environment, set `PAPER_CLOUD_STORAGE=/var/lib/ai-short-term-paper` and run `cloud/runner.py --probe`, then `--once`. For representative verification, retain the systemd service's isolation settings when dispatching these checks. Verify real data, complete history, a successful paper cycle and persistent state; a health response alone is insufficient.

`verify_runtime.py` is a root-run restart check that requires standby and preserves the ledger. Run `verify_sandbox.py` only through the native sandbox in a fresh job directory, not directly as an unrestricted process. Confirm that allowed job writes succeed, outside writes are refused and direct networking is denied.

Pause any old scheduler and reconcile revisions, cash, positions, trades and pause state before enabling the new scheduler. Set `CLOUD_RUN_ENABLED=1` in the private `/etc/ai-short-term-paper.env`, restart the service and verify its schedule and unchanged ledger. Do not publish that environment file or resume two independent writers.

Open-market quotes and forward behavior must be observed during actual sessions. The existing validation does not establish profitability or permanent availability.

## Official references

- [Google Cloud free-program terms](https://docs.cloud.google.com/free/docs/free-cloud-features)
- [Codex authentication](https://learn.chatgpt.com/docs/auth)
- [Codex MCP setup](https://learn.chatgpt.com/docs/extend/mcp)
