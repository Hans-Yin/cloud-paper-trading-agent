# Data and publication boundaries

Source, synthetic tests and generic deployment templates are suitable for this repository. Account data and operational state belong in private runtime storage.

Keep the following outside Git, even when a repository is private:

- Native Codex/Robinhood credentials, OAuth codes, callback URLs, passwords and SSH private keys.
- Cloud SDK credential directories, billing records and private environment files.
- Broker responses, account snapshots, paper ledgers, audit history and downloaded reports.
- Deployment-specific project IDs, machine addresses, administrative access rules and personal filesystem paths.

The runtime stores only an account suffix when normalizing account metadata, but the resulting files still contain financial information and are not public samples. The export was made from an explicit source-file allowlist, rather than by recursively copying an operational workspace. `.gitignore` excludes known runtime paths; it does not make an arbitrary file safe to publish.

`tools/prepare_demo.py` creates fictional initialization data locally and refuses to overwrite an existing ledger. Generated migration files and ZIP bundles are ignored. Never replace a running ledger with a demo or initial migration seed.

If a credential is accidentally published, removing the current file is insufficient: revoke the credential through its provider and review repository history. This export contains no credential cache.
