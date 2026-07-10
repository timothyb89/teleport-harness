# teleport-harness

Spin up **disposable, browsable Teleport clusters** in Docker from any teleport clone/branch,
run **feature/version-gated test plans** (positive *and* negative cases) that compose one or
more test modules into a cluster, and get an **inspectable report** (results + logs + rendered
config) with the cluster left running.

Standalone repo — point it at any of your `teleport-*` clones with `--repo`; it never touches
their branch. Contributor docs, architecture & gotchas: **[CLAUDE.md](CLAUDE.md)**.

## How it's built
- **Shell** (`bin/cluster`, `lib/*.sh`) owns orchestration: docker/compose, the shared nginx
  ingress + Let's Encrypt wildcard cert, SHA-cached cross-builds.
- A **typed Python brain** (`harness/`, run via [`uv`](https://docs.astral.sh/uv/)) owns the
  data + decisions: YAML parsing, feature/version gating, `checks:` validation, the verifier
  (over a docker seam, so asserts are unit-tested), and jinja2 compose rendering. Tests:
  `uv run --extra dev pytest`.

## Setup (one time)
1. Docker via lima with amd64 emulation; **`uv`**; the messense cross toolchain
   (`brew install messense/macos-cross-toolchains/x86_64-unknown-linux-gnu`); `python3`, `jq`.
2. `cp targets/default.env.example targets/default.env` and fill in `HARNESS_DOMAIN`,
   `CF_DNS_API_TOKEN` (Cloudflare, Zone:DNS:Edit), `ACME_EMAIL`.
3. Add a wildcard DNS record you control: `*.lab.<HARNESS_DOMAIN>  A  127.0.0.1`.
4. `./bin/cluster doctor` — should be all green.

## Use
```bash
# Validate the test modules (schema + check verbs), no cluster needed:
./bin/cluster validate

# Run a single module against a checked-out clone/branch:
./bin/cluster run-plan generic_oidc --repo ~/projects/teleport-b \
    --features generic_oidc --version v18

# Run a multi-module PLAN — several modules composed into ONE cluster (shared auth +
# shared components), each gated + verified independently:
./bin/cluster run-plan oidc-caching --repo ~/projects/teleport-e \
    --features kubernetes --version v18
```
`run-plan` builds Teleport (cached by commit SHA), composes + brings up the cluster behind the
shared ingress, verifies the join outcomes, writes `runs/<ts>-<id>/` (per-module `results-*.json`
+ logs + rendered config), and leaves the cluster up. A module gated out by `--features`/`--version`
is SKIPped (logged, not silently). `--features` describes what the target build actually provides.

```bash
./bin/cluster ls                     # running clusters
./bin/cluster admin <id>             # privileged admin bot identity (MFA-free CLI)
./bin/cluster tctl <id> get nodes    # admin tctl via the bot identity
./bin/cluster tsh  <id> ls           # tsh via the bot identity + proxy
./bin/cluster web  <id>              # web URL (real TLS) + admin signup link (break-glass)
./bin/cluster logs <id> [service]
./bin/cluster teardown <id>          # or --all
```

Test a branch: `git -C <clone> checkout <branch>`, then `--repo <clone>`.

## Layout
- `modules/<name>/` — a join-method test unit: `module.yaml` (gating + declarative `checks:`),
  `services.yml.j2` (its bots/agents), `render.yaml`, `config/*.j2`, `bootstrap/` (roles/tokens).
  Today: `generic_oidc`, `tbot`, `bound_keypair`, `kubernetes`.
- `components/<name>/` — a shared service dependency modules pull in via `components:`
  (today: `oidc-server`, a trivial IdP reused by `generic_oidc` + `kubernetes`).
- `plans/<name>.yaml` — several modules composed into one cluster (today: `bots`, `oidc-caching`).
- `harness/` — the Python brain (`models`, `checks`, `verify`, `cluster`, `render`, `report`, `cli`); `tests/`.
- `skills/` — the Claude Code skill(s) for this harness, version-controlled with the code.

## Claude Code skill
The `teleport-cluster` skill (running plans + authoring modules) lives in `skills/` and is
installed as a **personal** skill (available everywhere, incl. when working inside a teleport
clone) via a symlink:
```bash
./bin/install-skills             # symlink skills/* into ~/.claude/skills/ (idempotent)
./bin/install-skills --uninstall # remove the symlinks
```
The repo is the source of truth (edits are live). Authoring guidance for new modules/components/
plans/verbs is in `skills/teleport-cluster/references/authoring.md`.

## Commands
`doctor` · `validate` · `build` · `up` · `run-plan` · `ls` · `logs` · `admin` · `tctl` · `tsh` ·
`web` · `report` · `teardown`. See [CLAUDE.md](CLAUDE.md) for architecture, the module/component/
plan contracts, and how to add a module.
