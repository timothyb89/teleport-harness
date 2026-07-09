# teleport-harness

Spin up **disposable, browsable Teleport clusters** in Docker from any teleport clone/branch,
run **feature/version-gated test plans** (positive *and* negative cases), and get an
**inspectable report** (results + logs + rendered config) with the cluster left running.

Standalone repo — point it at any of your `teleport-*` clones with `--repo`; it never touches
their branch. Contributor docs & gotchas: **[CLAUDE.md](CLAUDE.md)**.

## Setup (one time)
1. Docker via lima with amd64 emulation; the messense cross toolchain
   (`brew install messense/macos-cross-toolchains/x86_64-unknown-linux-gnu`); `python3`, `jq`.
2. `cp targets/default.env.example targets/default.env` and fill in `HARNESS_DOMAIN`,
   `CF_DNS_API_TOKEN` (Cloudflare, Zone:DNS:Edit), `ACME_EMAIL`.
3. Add a wildcard DNS record you control: `*.lab.<HARNESS_DOMAIN>  A  127.0.0.1`.
4. `./bin/cluster doctor` — should be all green.

## Use
```bash
# Run the generic_oidc plan against a checked-out clone/branch:
./bin/cluster run-plan generic_oidc --repo ~/projects/teleport-b \
    --features generic_oidc --version v18
```
This builds Teleport (cached by commit SHA), brings up auth+proxy + an OIDC server + a
token-manager bot + positive/negative/scoped agents behind the shared ingress, verifies the
join outcomes, writes `runs/<ts>-<id>/`, and leaves the cluster up.

```bash
./bin/cluster ls                     # running clusters
./bin/cluster web <id>               # web URL (real TLS) + admin signup link
./bin/cluster logs <id> [service]
./bin/cluster teardown <id>          # or --all
```

Test a branch: `git -C <clone> checkout <branch>`, then `--repo <clone>`.

## Commands
`doctor` · `build` · `up` · `run-plan` · `ls` · `logs` · `web` · `report` · `teardown`.
See [CLAUDE.md](CLAUDE.md) for architecture, the module contract, and how to add a module.
