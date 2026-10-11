# Validation scope

## Offline software checks

The exported source includes 23 engine tests and 28 cloud-control tests. These use synthetic data and mocked collection, not real account history or broker orders.

Coverage includes risk and exposure constraints, repeated snapshots, stale quotes, invalid caches, incomplete bars, gap assumptions, loss-review pauses, holiday/early-close scheduling, daylight saving time, controller locking, migration verification, missing-ledger refusal, authentication-directory handling, read-only tool configuration, connected-app restrictions and agent timeouts.

Run from the repository root:

```sh
python3 -m unittest -v test_engine.py cloud/test_cloud.py
```

## Reference deployment observations

The original Debian 12/GCP deployment verified native account login and an actual read-only MCP probe. It collected complete daily histories for nine candidate symbols and the SPY benchmark, plus account, eligibility and earnings information. A complete market-closed paper cycle passed in the service's isolation environment, with no paper fills.

Service restarts preserved the paper ledger byte-for-byte, including after scheduler enablement. A native Linux sandbox helper was checked for job-directory writes, denied writes outside the job and denied direct networking. These observations apply to that tested deployment; this public export contains no operational account records or proof of another installation.

## Not established

Market-hours forward behavior, sustained availability, strategy backtesting, profitability, live-order execution and push notifications were not established at the initial export. The runtime remains paper-only. Tests of control logic cannot demonstrate an investment return.
