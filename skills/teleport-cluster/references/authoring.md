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
    audit_event         bot.join bot_name=my-bot method=<method> success=true  # structured proof
    output_file         my-bot /out/id/identity
    identity_authorized my-bot /out/id/identity
    log_contains        my-bot-deny denied|not found|unauthorized   # negative case
    no_output_file      my-bot-deny /out/id/identity
  ```

### Write claims, not just checks
Reports are read by people with none of your context (that's the whole point of `share`). A
flat check list gives them verdicts and makes them reverse-engineer the argument. Prefer
`preconditions:` + `claims:` — see `modules/bound_keypair_status/` for a worked example:

```yaml
preconditions: |             # scaffolding: proves the SCENARIO was set up
  bot_joined  my-bot bound_keypair
  log_count   mutator ge 1 RESULT mutator: DONE

claims:
  - id: preserved-on-update
    statement: If the token already exists, .status is preserved across any update.
    why: >                   # the LOGICAL CHAIN — how these checks establish the statement,
      The bot bound a key first, so status is server-owned and non-empty. Two updates were
      then ACCEPTED (each carries a distinct sentinel the mutator waits for), and after each,
      every status field still reads its pre-update value. So this is preservation through a
      SUCCESSFUL update, not one that was rejected.
    checks: |
      resource_field_not token/t status.bound_keypair.bound_public_key ForgedKey
      log_count          mutator ge 1 RESULT preserved: PASS
```

- **A claim is what a reviewer would write down as the test item.** If you keep a list of
  manual test items for a fix, those are your claims — one each, verbatim.
- **`why` is the deliverable.** Checks say what held; `why` says why that's sufficient. State
  what the checks rule OUT (a rejected update, a stale read, a forged value passing a presence
  check), because that's the part a reader can't infer.
- **Write `why` as short markdown, not a paragraph.** It renders as markdown directly under the
  claim heading, so use bold leads and bullets; a wall of prose is exactly what this replaced.
  A good shape is one bullet per thing the reader must accept, e.g. *what was done* then *why
  the comparison is meaningful*. Keep sentences short. Don't restate the statement.
- **`artifacts:`** — module-relative paths (`scripts/mutate.sh`, `config/token.yaml.j2`,
  `bootstrap/token.yaml.j2`) to the files a reader would open to judge the evidence. They are
  mapped to their bundle locations and rendered as links, and `cluster validate` fails on a
  path that doesn't exist, so a typo can't become a dead link in a shared gist. Always list
  the script behind a log-scraping check.
- **`# note` per check.** A trailing ` # ...` on a check line replaces the verb's message in
  the report's detail column. Use it wherever the verb can't say anything useful on its own —
  `log_count svc ge 1 RESULT foo: PASS` otherwise reports "1 match(es) for /RESULT foo: PASS/",
  which just restates the check. Say what the match MEANS ("after the forged apply, every
  status field still matches"). The verb's own message stays visible under the proof, so
  nothing is duplicated. Split is on `" # "` (spaces both sides), so `#` inside a regex is safe.
- **Preconditions are not evidence.** If one fails the claims are marked UNTESTED, not
  DISPROVEN. Put "did the scenario even happen" checks here (`bot_joined`, the actor ran to
  completion) and keep claim checks to things that actually discriminate.
- **A claim with no checks is a validation error** — it would render as vacuously PROVEN.

### Scripts: act and record, don't assert
Driving state from a script is fine and often necessary — a script mutating the cluster is
frequently the only thing that can see a before/after across its own mutation. But keep it out
of the judging seat. If it decides the verdict, the module can only grep for `RESULT foo: PASS`,
the report cites a magic string whose meaning lives in the script, and the detail column can do
no better than restate the check.

**The pattern:** the script appends one record per case to `/out/observations.json` —

```json
{"case": "spec-only-reapply", "at": "2026-08-11T04:00:00Z", "token": "bks-token",
 "applied": "config/token-spec-only.yaml",
 "before": {"status.bound_keypair.bound_public_key": "ssh-ed25519 AAAA…", "spec.bound_keypair.recovery.limit": "1"},
 "after":  {"status.bound_keypair.bound_public_key": "ssh-ed25519 AAAA…", "spec.bound_keypair.recovery.limit": "7"}}
```

— and the checks do the asserting:

```
observation_unchanged bkstatus spec-only-reapply status.bound_keypair.bound_public_key status.bound_keypair.recovery_count
observation_equals    bkstatus spec-only-reapply spec.bound_keypair.recovery.limit 7
```

What this buys over a `log_count` on the script's own verdict:
- **The proof is the observed values**, rendered as JSON, not a log line.
- **The detail is generated** from the data (`bound_public_key: 'ssh-real' -> ''`), so it can't
  go stale the way a hand-written `# note` can when the script's logic changes.
- **`at` is recorded**, so *when* each mutation happened is in the report.
- **Missing data fails loudly** — an unrecorded field is a FAIL, not a silent pass, and a
  missing case lists the cases that *were* recorded.
- **`observation_equals` proves a write LANDED**, so "unchanged" is distinguishable from "the
  mutation never happened" — a distinction a preservation test lives or dies on.

Keep an `output_file <actor> /out/observations.json` precondition so a crashed actor reads as
scaffolding failure (claims UNTESTED) rather than claim failures it caused. And still record
on the unhappy path: a timed-out barrier should record what it last saw, because a missing
record is indistinguishable from a crash while a recorded bad value is a finding.

Two rules that apply to any script, observations or not:
- Mount from **`{{ scripts }}`**, never `{{ module_dir }}/scripts` — the renderer copies
  `scripts/` into the bundle, so the actor ships with the report and appears in `share` gists.
  Otherwise a reviewer sees the inputs and the verdicts but not the thing connecting them.
- Have the script **log its inputs and observed values**, not just a verdict, and put the
  interpretation in the claim's `why`. Better still, where a declarative verb can assert the
  same fact against live cluster state (`resource_field`, `resource_field_not`), prefer that:
  it carries its own proof. Reserve script output for facts only the script can know — e.g.
  a before/after comparison across a mutation it performed.
- **Say WHEN.** Cluster state is time-dependent; a check reads state at verify time, which may
  be long after the script ran and after anything else the plan did to the cluster. If a claim
  depends on ordering, say so in `why` — and prefer asserting on state nothing else rewrites
  (see the bound_keypair_status note about bootstrap re-application reverting a spec edit).
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
  image, or build a binary from the clone), run with the render context as `UPPER_CASE` env
  (`module_dir` → `$MODULE_DIR`, `out` → `$OUT`, and `repo` → `$REPO`, the teleport clone path —
  how `components/terraform-runner/` builds the provider).
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
       # c: the Cluster seam (get_nodes/logs/exec_out/file_nonempty/file_size/tsh_ssh/audit_events)
       ok = ...
       return CheckResult(PASS if ok else FAIL, "<message>",
                          proofs=[ProofItem("text", "<title>", "<full content>")])
   ```
2. Register it in `harness/checks.py` — add a `VerbSpec(name, min_args, max_args, usage)`.
   A test (`verb_impls_match_registry`) enforces IMPLS ↔ REGISTRY parity.
3. Attach **proof(s)** — evidence is a first-class `ProofItem{kind,title,content,lang,source}`,
   decoupled from the check so several checks can cite ONE proof (proof `id` is a content hash →
   identical proofs dedup). `content` is kept in FULL (never truncated). Pick a `kind`:
   `node-record`/`file`/`command`/`text` for short proofs; `log-excerpt` via
   `_log_proof(cname, suffix, title, _excerpt(lines, idxs))` (grep -C3 window + a `logs/<svc>.log`
   link); `audit-event` via `_audit_proof(ev)` (the FULL event as pretty JSON, `lang="json"`).
   For a structured audit assertion, prefer the `audit_event` verb over scraping text logs.
   The report renders each proof once as a linkable, anchored section; the check table links to it.
4. Unit-test it in `tests/test_verify.py` with a `FakeCluster` (no docker) — assert status +
   `res.proofs`. `FakeCluster(events=[...])` feeds structured audit events. This is the fast,
   reliable way to get a verb right.

A verb can prove a *system property*, not just a single fact. Worked example: `log_count
<suffix> <op> <n> <regex…>` counts matching log lines and asserts the tally, so the
`oidc_caching` module proves the auth server's caching validator works by showing "≥3 fresh
joins drove `/k8s/token` traffic, yet discovery + JWKS were each fetched ≤1× on a DEDICATED
IdP" — many joins, one fetch. Isolating the IdP (its own issuer + data volume, not the shared
`oidc-server`) keeps the request-count ledger unambiguous even when composed in a plan.

## Add a shared component
`components/<name>/` has the same shape as a module minus `module.yaml`/`checks:` — a
`services.yml.j2` fragment (the service + its `volumes:`), optional `render.yaml`/`prebuild.sh`/
`config/*.j2`/`bootstrap/`. Modules opt in via `components: [<name>]` (deduped across a plan, so
two modules sharing one component get a single instance). See `components/oidc-server/`.

## Add a Terraform-provider test (`terraform-runner` component)
Test a change that's managed via the Teleport **Terraform provider** by driving a DEV build of
the provider (from the clone's working tree) against the cluster. The shared
`components/terraform-runner/` does the heavy lifting; a new test is a thin module:

- `render.yaml`: `components: [terraform-runner]` + the engine vars `tf_image`/`tf_bin`
  (module fragments can't see a component's render vars, so set them here — default
  `hashicorp/terraform:1.9` / `terraform`; OpenTofu is `ghcr.io/opentofu/opentofu:1.8` / `tofu`).
- `services.yml.j2`: one runner container (copy `modules/terraform_bot/`). Key bits —
  `image: {{ tf_image }}`, `entrypoint: ["sh", "/scripts/tf-entrypoint.sh"]`, env
  `TF_BIN`/`TF_TELEPORT_ADDR: {{ cluster_id }}-auth:3025`/`TF_TELEPORT_IDENTITY_FILE_PATH: /id/identity`
  (+ any `TF_VAR_*`), mounts `{{ out }}/tf-plugins:/plugins:ro` (the built provider),
  `{{ shared_scripts }}/tf-entrypoint.sh`, `{{ module_dir }}/tf:/work:ro` (your HCL), and
  `tf-identity:/id:ro`; `depends_on: { tf-idbot: { condition: service_healthy } }`.
- `tf/*.tf`: your Terraform config (mounted from source, so cluster values come via `TF_VAR_*`,
  not templating). The provider block is empty — `addr`/identity come from env. The entrypoint
  uses `dev_overrides` → NO `terraform init`/lockfiles.
- `checks:`: `log_contains <svc> Apply complete` + `resource_present <kind/name>` /
  `resource_field <kind/name> <dotted.path> [expected]` to assert what apply created in the cluster.

Notes: auth is `identity_file_path` (the provider REJECTS the token join method); the provider
binary is rebuilt every render (an uncommitted provider fix always takes — no SHA cache). A
known-failing-that-flips test (see `terraform_generic_oidc`, the `must_match_fields` bug) just
sets the not-yet-supported field and lets the resource checks FAIL until the provider is fixed.

## Add an agent-driven test (`agent-runner` component)
Test whether a real task is *doable* — e.g. "can someone follow this guide?" — by letting a
locked-down AI agent drive the cluster and report what it hit. The shared
`components/agent-runner/` provides a pre-seeded admin identity (agent runs `tctl` with no
login/MFA) + the `/out` bind; a new test is a thin module (copy `modules/docs_bound_keypair/`):

- `render.yaml`: `components: [agent-runner]` + an `agent:` block the host step reads —
  `provider: claude`, `model:`, `prompt: prompt.md`, `timeout_seconds:`.
- `services.yml.j2`: ONE idle runner container **named `workbench`** (`container_name:
  {{ cluster_id }}-workbench` — the name the driver execs into), `entrypoint: ["sh","-lc","mkdir -p
  /work && sleep infinity"]`, mounting whatever the task needs *read-only*, the identity
  `agent-identity:/id:ro`, and `{{ out }}/agent/out:/out`; `depends_on: { agent-idbot: { condition:
  service_healthy } }`. For a DOC-follow task, mount the WHOLE docs tree (`{{ repo }}/docs:/docs:ro`),
  not just one page — Teleport docs use `(!docs/pages/includes/…!)` directives, and the includes
  live outside any single page's subtree; mounting the tree makes them resolvable (`docs/X` →
  `/docs/X`). The image (`{{ image }}`) has `tctl`/`tbot`/`jq`/`curl` baked in (see `lib/build.sh`);
  if a task needs another tool, add it to that apt-get line (rebuild with `REBUILD_IMAGE=1`).
- `prompt.md` (rendered as jinja with `{{ cluster_id }}`/`{{ fqdn }}`/`{{ port }}`/`{{ auth_addr }}`):
  the task. Tell the agent its ONLY tool is `run(cmd)` (execs inside the container), to background
  long-runners, NOT to fabricate success, to use fixed resource names, and to finish by writing
  `/out/agent-result.json`. For raw-source docs, tell it how to resolve `(!…!)` includes (read the
  named file under `/docs`) so it follows the guide's actual steps instead of guessing.
- `checks:`: `agent_result` (advisory — surfaces the findings; FAILs only if no valid result) PLUS
  **objective** verbs that actually gate on the resulting cluster state (`bot_joined`,
  `resource_present <kind/name>` with the names you pinned in the prompt). Never let the agent's
  self-verdict be the gate.

How it runs: `run-plan` calls `lib/agent.sh::run_agents` after the cluster is healthy and before
verify; it waits for the `workbench` container, then `harness agent-run` drives `claude -p` on the
host (subscription auth, no API key). Containment is by construction — the agent has exactly one
tool (`harness/agent_mcp.py`'s `run`) that execs inside the one workbench (no docker socket, no
host access); the lockdown flags live in `harness/agent.py`. Requires `claude` on PATH + logged in
(`doctor` warns if missing). A future API-key driver (Agent SDK / Codex) is a drop-in behind
`agent.provider` and unlocks CI — the module contract is unchanged.

## Add an in-Kubernetes operator test (`k8s-runner` component)
Test a change that's managed via the Teleport **Kubernetes operator** by applying custom resources
to a disposable in-cluster k3s and asserting on BOTH sides (what Kubernetes stored, what reached
Teleport). The shared `components/k8s-runner/` provides the k3s node, the CRDs and the operator
built from the clone; a new test is a thin module (copy `modules/operator_generic_oidc/`). Full
design, host requirements and troubleshooting: **`docs/kubernetes.md`**.

- `render.yaml`: `components: [k8s-runner]` + `k3s_version:` (module fragments can't see a
  component's render vars, so re-declare it here).
- `services.yml.j2`: one CR runner. Key bits — `image: rancher/k3s:{{ k3s_version }}` (the k3s
  image IS the runner: kubectl + a shell already inside, no extra pull, no version skew),
  `entrypoint: ["/bin/sh", "/scripts/k8s-apply-entrypoint.sh"]`, env
  `K3S_HOST: {{ cluster_id }}-k3s`, mounts `{{ shared_scripts }}/k8s-common.sh` +
  `{{ shared_scripts }}/k8s-apply-entrypoint.sh`, `k8s-kubeconfig:/kubeconfig:ro`, and each
  rendered CR at `/work/NN-<name>.yaml` (applied in FILENAME order — number them);
  `depends_on: { k8s-operator: { condition: service_healthy } }` so you never apply a CR into a
  cluster with no controller watching. Healthcheck on `test -f /tmp/apply-done`.
- `config/*.yaml.j2`: your CRs (rendered, so `{{ fqdn }}`/`{{ lab_domain }}` work). Namespace is
  `teleport`.
- `checks:`: `k8s_resource_present`/`k8s_resource_field`/`k8s_condition` for the KUBERNETES side +
  `resource_present`/`resource_field` for the TELEPORT side. Asserting both localises a failure
  precisely — CRD schema rejected it vs operator never reconciled it vs it never reached Teleport.
  Add a behavioural check (an agent that joins with the resulting token) whenever the resource is
  something enforced at runtime; "stored correctly" is weaker than "actually evaluated".

Notes: the apply runner NEVER aborts on a rejected manifest — a rejection is frequently the finding
— and logs a `RESULT <file>: accepted|REJECTED` line per file you can `log_contains` on. Use
`log_contains` for the diagnostic ("here is today's exact error") because it SKIPs (neutral) once
the bug is fixed, and the `k8s_*`/`resource_*` verbs for the gates that flip FAIL→PASS. CRDs come
from the CHECKED-IN generated files, so after editing `crdgen` run
`make -C integrations/operator crd-manifests` or you'll test the old schema.

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
- [ ] When finished, suggest creating a commit. Unless specified, commits
      directly to main are fine.
