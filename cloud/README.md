# Cloud runtime

The reference deployment uses a dedicated Debian 12 VM and a systemd service. Source lives under `/opt/ai-short-term-paper`; private runtime state and native credentials live under `/var/lib/ai-short-term-paper`.

## Lifecycle

1. Run offline tests and prepare a fresh demo seed, or privately export a reviewed current paper ledger using `prepare_bundle.py`.
2. Review and install the bundle on a new dedicated Linux VM. The installer refuses existing installations rather than overwriting state.
3. Complete fresh native ChatGPT and Robinhood OAuth login as the service user. Do not copy credential caches from another computer.
4. Run `runner.py --probe` in the service environment. This verifies account access and required data without changing the paper ledger.
5. Run a complete paper cycle with `--once`, verify the report and ledger, then verify sandbox behavior and restart persistence.
6. Stop any earlier scheduler, compare the source and destination state, and enable `CLOUD_RUN_ENABLED=1` only after a reviewed cutover.

The controller uses America/New_York trading sessions and 15-minute slots. It records a slot before dispatch so a restart does not replay a partially started turn. It rejects concurrent controllers and never reseeds a missing ledger after initialization.

## Permissions and authentication

`codex-config.toml` allows only seven Robinhood reads and disables additional apps. Agent turns use a strict configuration, workspace sandbox, no direct command-network access and JSON output constraints. The host revalidates the plan and persistent state before applying a paper decision.

The native client uses a fresh ChatGPT login. The runtime rejects API-key environment variables to avoid switching accidentally to separately billed API usage. Authentication and MCP compatibility depend on the installed client and account features; use current official documentation when configuring a new installation.

The service health response only reports process availability. It does not demonstrate that login, market data or the last cycle succeeded; inspect the private `system/data/cloud_status.json` and ledger.

## Optional container route

`Dockerfile`, `render.yaml` and `entrypoint.sh` retain the alternative container deployment template. No Render resource or fee is authorized by these files. The container build, provider quote, memory capacity and OAuth callbacks require separate validation. GCP uses the native installer instead.

## Limitations

Only paper accounting is supported. There is no order submission or reconciliation module. Notifications are not integrated. A static local report does not automatically download fresh cloud state. Model quota, invalid data, timeouts or invalid login stop the affected cycle.
