# Authoring for the teleport-harness — modules, components, plans, check verbs

How to add tests to the harness. Read `~/projects/teleport-harness/CLAUDE.md` for the
architecture + invariants; this is the step-by-step authoring procedure. Everything lives in
`~/projects/teleport-harness`.

## Mental model
- **module** (`modules/<name>/`) — a join-method / feature test unit: its services (bots/agents)
  + gating + a declarative `checks:` block. This is what you'll add most often.
- **component** (`components/<name>/`) — a shared service dependency (e.g. `oidc-server`) that
  modules pull in via `components:`. Add one only when 2+ modules need the same side service.
- **plan** (`plans/<name>.yaml`) — several modules composed into ONE cluster, gated + reported
  together.
- **check verb** — a declarative assertion (`node_present`, `log_contains`, …) implemented in
  `harness/verify.py` + registered in `harness/checks.py`. Add one when a module needs a new
  kind of proof.

A cluster is **composed** by the renderer: a base scaffold (auth+proxy) + each declared
component + each module's `services.yml.j2` fragment, deep-merged into one docker-compose.
You do NOT write compose boilerplate, an auth service, or an auth-entrypoint — you contribute
a services fragment + declarative bootstrap.

## The dev loop
```bash
cd ~/projects/teleport-harness
./bin/cluster validate <name>                 # schema + verb/arity check — run first, run often
uv run --extra dev pytest                      # unit tests (models, render, verify) — no docker
./bin/cluster up <name> --repo <clone>         # render + bring up, iterate (leaves it up)
./bin/cluster run-plan <name> --repo <clone> [--features a,b] [--version vNN]   # gate+verify+report
./bin/cluster logs <id> [svc]                  # inspect a service
./bin/cluster tctl <id> get nodes|tokens|bots  # admin CLI via the bot identity
./bin/cluster teardown <id>                    # clean up when done
```
Fastest inner loop: `validate` + `pytest` catch most mistakes before you ever start docker.
Rendering is pure (jinja); `up`/`run-plan` are the only docker-touching steps.

## Add a join-method module (the common case)
Copy `modules/tbot/` (simplest bot-join shape) or `modules/kubernetes/` (delegated-join +
shared component + bootstrap hook). A module directory:

- **`module.yaml`** — gating + the source-of-truth checks:
  ```yaml
  name: <name>                 # MUST equal the directory name
  description: >
    one or two lines.
  provides_feature: <name>     # capability this exercises
  requires_features: [<name>]  # gated on the target providing these via --features ([] = always runs)
  min_version: v18             # gated on --version
  checks: |                    # declarative; verified by harness/verify.py (see verbs below)
    bot_joined          my-bot <method>
    output_file         my-bot /out/id/identity
    identity_authorized my-bot /out/id/identity
    log_contains        my-bot-deny denied|not found|unauthorized   # negative case
    no_output_file      my-bot-deny /out/id/identity
  ```
- **`services.yml.j2`** — a compose FRAGMENT (`services:` + optional `volumes:`), your bots/agents
  only (no auth — that's the base). Jinja context: `cluster_id fqdn port image out module_dir
  lab_domain harness_domain` + everything in `render.yaml`. Container names become
  `{{ cluster_id }}-<service>`; checks reference the suffix after `<id>-`. Example:
  ```yaml
  services:
    my-bot:
      image: {{ image }}
      platform: linux/amd64
      container_name: {{ cluster_id }}-my-bot
      command: [tbot, start, -c, /etc/tbot.yaml, "--token={{ bot_token }}", "--join-method=<method>"]
      volumes: [ "{{ out }}/config/tbot.yaml:/etc/tbot.yaml:ro" ]
      networks: [internal]
      depends_on: { auth: { condition: service_healthy } }
  ```
  Include a **negative** service (e.g. wrong secret) + `*_absent`/`log_contains`/`no_output_file`
  checks — proving denial is half the value.
- **`render.yaml`** *(optional)* — extra jinja context, merged into the render:
  ```yaml
  components: [oidc-server]          # shared deps to compose in (omit if none)
  auth_env: { TELEPORT_UNSTABLE_SCOPES: "yes" }   # env vars added to the auth service
  bot_token: harness-my-secret       # arbitrary vars usable in your templates
  bots:                              # bots created at bootstrap (see below)
    - {name: my-bot, roles: <role>, token: harness-my-secret}
  ```
- **`config/*.j2`** — teleport/tbot configs, rendered to `$OUT/config/<name>` (`.j2` stripped).
  The shared `auth.yaml` comes from the base; override only by shipping `config/auth.yaml.j2`.
- **`bootstrap/*.yaml[.j2]`** — roles + provision-token resources applied at cluster bootstrap.
  `.j2` are rendered. You do NOT script `tctl create` — the shared `auth-entrypoint.sh` applies
  every `bootstrap/*.yaml`, then adds every `bots:` entry, then signals readiness. Order is:
  static resources → `bootstrap/hooks/*.sh` → `bots add`. A token with `bot_name` may be created
  before its bot exists.
- **`bootstrap/hooks/*.sh[.j2]`** *(optional)* — local-admin scripts (run inside the auth
  container) for resources that must be built at runtime, e.g. a `static_jwks` token whose JWKS
  is fetched from a running component. `$CONFIG` (auth.yaml path) is exported.
- **`prebuild.sh`** *(optional)* — an imperative pre-render step (e.g. `docker build` a side
  image), run with the render context as `UPPER_CASE` env (`module_dir` → `$MODULE_DIR`).
- **`checks.py`** *(optional escape hatch)* — `def checks(cluster, nodes) -> list[CheckResult]`
  for a proof no declarative verb expresses. Prefer adding a verb (below) if it's reusable.

### Bots + delegated joins
`bots:` entries become `tctl bots add <name> --roles=<roles> [--token=<token>]`. Leave `token`
empty when the bot joins via a delegated method (kubernetes/oidc/…): create the join token as a
`bootstrap/*.yaml` (or hook) with `bot_name: <name>` — it authorizes the join, and the empty
manifest token just creates the bot. See `modules/kubernetes/`.

## Add a check verb
1. Implement it in `harness/verify.py` — add to `IMPLS`:
   ```python
   def _my_verb(c, nodes, args):
       # c: the Cluster seam (get_nodes/logs/exec_out/file_nonempty/file_size/tsh_ssh)
       ok = ...
       return CheckResult(PASS if ok else FAIL, "<message>", evidence=["<one-line proof>"])
       # for log-based proof use excerpt=_excerpt(lines, match_idxs) instead of evidence
   ```
2. Register it in `harness/checks.py` — add a `VerbSpec(name, min_args, max_args, usage)`.
   A test (`verb_impls_match_registry`) enforces IMPLS ↔ REGISTRY parity.
3. Capture **evidence**: node/file/identity checks set `evidence=[...]` (a short proof line);
   log checks set `excerpt=_excerpt(...)` (a grep -C3 line-numbered window). It shows in the
   console, `results-*.json`, and `results.md`.
4. Unit-test it in `tests/test_verify.py` with a `FakeCluster` (no docker) — assert status +
   evidence. This is the fast, reliable way to get a verb right.

## Add a shared component
`components/<name>/` has the same shape as a module minus `module.yaml`/`checks:` — a
`services.yml.j2` fragment (the service + its `volumes:`), optional `render.yaml`/`prebuild.sh`/
`config/*.j2`/`bootstrap/`. Modules opt in via `components: [<name>]` (deduped across a plan, so
two modules sharing one component get a single instance). See `components/oidc-server/`.

## Write a plan
`plans/<name>.yaml` (name MUST equal filename):
```yaml
name: <name>
description: > one line.
modules: [module_a, module_b]
```
`run-plan <name>` composes all listed modules (+ their components) into one cluster, gates each
independently (gated-out → SKIP, left out of the compose), verifies each, one report.

## Authoring gotchas (the ones that bite)
- **All ports = the ingress `{{ port }}`** end-to-end (proxy/public_addr/agent proxy_server). A
  split breaks agent reverse tunnels.
- **East-west dials the FQDN**, not the container/service name — the wildcard cert only matches
  `*.lab.<domain>`. Agents reach auth at its FQDN alias; a component needing system-trusted TLS
  (e.g. kube `oidc`, no custom-CA) must serve the wildcard cert at `oidc.{{ lab_domain }}` and be
  referenced by that host. Intra-cluster gRPC to `auth:3025` is fine (mTLS via the identity CA).
- **Don't write an auth-entrypoint or `touch /tmp/bootstrap-done`** — the shared entrypoint does
  bootstrap + readiness. Bots `depends_on: auth service_healthy`, which already waits for bootstrap.
- **Scoped tokens/resources** need `auth_env: { TELEPORT_UNSTABLE_SCOPES: "yes" }`.
- **Prove denial**, not just success: every module should have a negative service + `node_absent`/
  `log_contains`/`no_output_file` check.
- **`validate` before `up`** — it catches typo'd verbs, wrong arity, unknown YAML keys, name≠dir.

## Checklist for a new module
- [ ] `module.yaml` name == dir; sensible `provides_feature`/`requires_features`/`min_version`.
- [ ] `services.yml.j2` fragment: positive + negative services; `{{ port }}` everywhere; FQDN for east-west.
- [ ] `render.yaml`: `components:`/`auth_env:`/`bots:`/vars as needed.
- [ ] `bootstrap/`: roles + tokens (+ hook if runtime-built); config `*.j2`.
- [ ] `checks:`: positive + negative + a usability check (`identity_authorized`/`tsh_ssh`).
- [ ] new verb? → `verify.py` IMPLS + `checks.py` VerbSpec + `FakeCluster` test.
- [ ] `cluster validate <name>` clean; `uv run --extra dev pytest` green.
- [ ] `run-plan <name> --repo <clone>` passes; skim `runs/<ts>-<id>/results.md` evidence.
