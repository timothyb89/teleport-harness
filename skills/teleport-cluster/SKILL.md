---
name: teleport-cluster
description: >
  Spin up disposable, browsable Teleport clusters in Docker from any teleport clone/branch
  and run feature/version-gated test plans (positive + negative cases) that produce an
  inspectable report. Use when asked to test/verify a Teleport feature, branch, PR, or
  backport end-to-end (not just unit tests) — e.g. "test the v18 generic_oidc backport",
  "spin up a cluster from this branch", "does joining still work on <branch>", or to author
  a new test module / component / plan for the harness. The harness lives OUTSIDE the teleport
  clones at ~/projects/teleport-harness and works against any clone via --repo.
---

# teleport-cluster — agentic Teleport test harness

A standalone project at **`~/projects/teleport-harness`** (its own git repo, independent of
the many `~/projects/teleport-*` clones). It builds Teleport from a clone's working tree,
runs it as a disposable dockerized cluster reachable in the browser over real TLS, and runs
declarative, gated test plans that leave behind a report + a live cluster to poke at.

**Always read `~/projects/teleport-harness/CLAUDE.md` first** — it has the architecture,
invariants, and hard-won gotchas. This file is just the entry point.

## When to use
- "Test / verify / exercise <feature|branch|PR|backport> end-to-end" (esp. joining, agents, bots).
- "Spin up a cluster from <clone/branch>" or "give me a web UI for this branch".
- **Authoring** — "add a module / join method / component / plan / check verb": read
  **`references/authoring.md`** in this skill directory for the step-by-step contract + worked example.

## Quickstart
```bash
cd ~/projects/teleport-harness
./bin/cluster doctor                        # preflight (lima, emulation, DNS, cert, toolchain)
./bin/cluster validate                      # schema-check all modules/plans (no cluster needed)
# run a single module, or a multi-module plan, against a checked-out clone/branch:
./bin/cluster run-plan generic_oidc  --repo ~/projects/teleport-b --features generic_oidc --version v18
./bin/cluster run-plan oidc-caching  --repo ~/projects/teleport-e --features kubernetes   --version v18
# -> builds (cached by SHA), composes + brings up the cluster, verifies positive+negative
#    joins, writes runs/<ts>-<id>/ (rich results.md + results-*.json), leaves the cluster UP.
./bin/cluster admin <id>                     # create a privileged admin bot identity
./bin/cluster tctl <id> get nodes            # admin tctl (MFA-free via bot identity)
./bin/cluster tsh  <id> ls                   # tsh via bot identity + proxy
./bin/cluster web  <id>                       # web URL + admin signup link (break-glass)
./bin/cluster ls                             # running clusters
./bin/cluster teardown <id>|--all
```
Admin CLI uses a **bot** identity (bots are exempt from Teleport's admin-action MFA;
user identity files are not). The web UI is break-glass.
To test a specific branch: `git -C <clone> checkout <branch>` first, then point `--repo` at
it (the harness builds whatever is checked out; it never switches the clone's branch).
`--features`/`--version` describe what the *target build* provides; a module that needs more
is SKIPped (logged), not silently dropped.

## Key facts (details in CLAUDE.md)
- Docker is via **lima** (context `lima-docker`); binaries are linux/amd64 (emulated).
- The harness "brain" (YAML parsing, gating, `checks:` validation, the verifier, compose
  rendering) is a typed Python package (`harness/`, run via **uv**); `cluster validate [module]`
  schema-checks before you spin anything up. Docker/nginx/cert/build plumbing stays in `lib/*.sh`.
  Tests: `uv run --extra dev pytest`.
- Access model: one shared **nginx SNI-passthrough ingress** on `:8443` + a **Let's Encrypt
  wildcard cert via Cloudflare DNS-01** for `*.lab.<domain>`. Clusters are `<id>.lab.<domain>`,
  publicly trusted (no CA imports). Config in gitignored `targets/<name>.env`.
- **Never verify TLS with macOS system `curl`** (LibreSSL lies) — use `python3`/`tsh`/an
  in-network curl container. This trips people up constantly.
- A cluster is **composed** from a base scaffold + shared `components/` + one-or-more module
  `services.yml.j2` fragments; `plans/<name>.yaml` runs several modules in ONE cluster. Checks are
  a declarative `checks:` block verified by `harness/verify.py`; each check captures **evidence**
  (matched log line w/ context, node record, command + exit) shown in the console + `results.md`.
- **Authoring anything new** (module/component/plan/verb): see `references/authoring.md`.
