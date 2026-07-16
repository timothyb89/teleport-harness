# teleport-harness — architecture & contributor guide

Disposable, browsable dockerized Teleport clusters for end-to-end testing of any
teleport clone/branch, with feature/version-gated test plans (positive + negative)
that produce inspectable reports. Standalone repo, independent of the `~/projects/teleport-*`
clones — point it at any clone with `--repo`.

Grew out of a one-off kit in `teleport-b/genericoidc-test/docker/`. First real use: the
v18 `generic_oidc` backport.

## Environment (host)
- Docker via **lima** (`docker context` = `lima-docker`); the VM does linux/amd64 emulation.
  All images/binaries are **linux/amd64** (`platform: linux/amd64` everywhere).
- Host tools: `uv` (runs the `harness/` Python brain), `mkcert` (fallback TLS only), `jq`,
  `python3`, the messense glibc cross toolchain `x86_64-unknown-linux-gnu-gcc` (override via `HARNESS_CC`).
- Per-target secrets in gitignored `targets/<name>.env` (default `default`): `HARNESS_DOMAIN`,
  `DNS_PROVIDER=cloudflare`, `CF_DNS_API_TOKEN` (Zone:DNS:Edit), `ACME_EMAIL`, `INGRESS_PORT`.
- Requires a wildcard DNS record you control: `*.lab.<HARNESS_DOMAIN> A 127.0.0.1`.
- The Claude Code skill lives in-repo at `skills/` (version-controlled with the code) and installs
  as a personal skill via `bin/install-skills` (symlinks `skills/*` → `~/.claude/skills/`; backups
  go to `~/.claude/skill-backups/`, never inside the skills dir — anything there loads as a skill).

## Architecture
### Shared, long-lived infra (`ingress/`, one per host)
- `harness-ingress` (nginx `stream` + `ssl_preread`): L4 **SNI-passthrough** on host
  `:INGRESS_PORT` (default 8443). Per-cluster routes are files `ingress/dynamic/<id>.map`
  (`<fqdn> <container>:<port>;`); the harness writes them and runs `nginx -s reload`.
  Teleport still terminates its own TLS (keeps ALPN multiplexing).
- `harness-certs` (acme.sh): issues + renews ONE wildcard LE cert `*.lab.<domain>` via
  Cloudflare **DNS-01** into the shared `harness-certs` volume (issue-once, persisted →
  prod rate-limit safe). Every cluster proxy mounts it; browser/tsh trust it (public CA).
- Shared external docker network `teleport-harness` + volumes `harness-certs`/`harness-acme`.

### Per-cluster stack (`teleport-harness-<id>`, disposable)
Composed into `state/<id>/` by the brain from a **base scaffold + shared components +
one-or-more module fragments** (all jinja), then `docker compose up`. A single cluster can
run several modules (a plan) sharing one auth + shared components. The auth+proxy container
is `${id}-auth`, listens on `${PORT}`, mounts the wildcard cert, joins `teleport-harness`
with network alias `<id>.lab.<domain>` (east-west agents dial the FQDN so TLS matches; the
ingress reaches it by container name), `public_addr = <fqdn>:<port>`. Its bootstrap is
declarative + shared: the renderer collects every unit's roles/tokens into `$OUT/bootstrap`
+ a `bots.manifest`, and one shared `auth-entrypoint.sh` applies them (see Invariants).

### Build (`lib/build.sh`, SHA-cached)
`build_image <clone> [ent]` cross-builds `teleport`/`tctl`/`tbot` (linux/amd64, glibc) from
the clone's **currently checked-out** working tree — never switches branches — reusing the
clone's prebuilt webassets. Keyed by `git rev-parse HEAD` → `.cache/bin/<sha>-<variant>/`
and image `teleport-harness:<sha>-<variant>`. Repeat builds are instant.

### Python brain (`harness/`, run via `uv`)
The data + decision layer — YAML parsing, feature/version gating, `checks:` validation,
the verifier, AND compose rendering — lives in the typed `harness/` Python package (pydantic
models, real YAML parser, jinja2, a docker `Cluster` seam), NOT in grep/sed/awk/heredocs. The
shell shells out via the `pybrain` helper (`lib/common.sh` → `uv run --project $HARNESS_ROOT harness …`).
Subcommands: `validate [module]` (schema + verb/arity check — used by `doctor`),
`gate <module> [--features] [--version]` (exit 3 == skip), `meta <module> <field>`,
`checks <module>` (validated `verb args` lines), `verify <module> --cluster-id <id>` (run checks,
structured JSON w/ a proof registry), `render --modules a,b,c --out <dir> …` (compose N modules +
components; also emits `setup.json` — a provenance manifest of the roles/tokens/bots/services it
created, each with a source link, that the report renders directly),
`plan-resolve <plan> [--features] [--version]` (gate each module → run/skip JSON),
`report-md --state-dir <dir>` (rich markdown report). All unit-tested
(`tests/`, `uv run --extra dev pytest`) — the harness's correctness bar. A bad `module.yaml`
(typo'd verb, wrong arity, unknown key, bad version) fails fast with a clear message instead of
deep in the verify retry loop. Docker/nginx/cert/build **plumbing stays in `lib/*.sh`** — the
brain owns decisions + rendering, the shell owns orchestration.

### Module contract (`modules/<name>/`)
- `module.yaml` — gating (`provides_feature`, `requires_features`, `min_version`)
  **plus** the verification spec: a `checks: |` block of `<assert-verb> <args...>` lines
  (the source of truth). `#` comment lines allowed. Parsed + validated by the Python
  brain (`harness/models.py`); run `cluster validate <name>` to check it.
- `services.yml.j2` — a jinja **fragment** (a partial compose: `services:` + optional `volumes:`)
  with just this module's bots/agents. The renderer (`harness/render.py`) deep-merges it onto the
  base auth scaffold + any shared components. Context = cluster vars (`cluster_id`/`fqdn`/`port`/
  `image`/`out`/`module_dir`) merged with the module's `render.yaml`.
- `render.yaml` *(optional)* — render context: `components: [oidc-server]` (shared deps to pull in),
  `auth_env: {…}` (unioned onto the auth service), `bots: [{name,roles,token}]` (bootstrapped bots),
  and any template vars. `config/*.j2` — teleport/tbot configs. `bootstrap/*.yaml[.j2]` — roles +
  provision-token resources applied at bootstrap (rendered if `.j2`). `prebuild.sh` *(optional)* —
  imperative pre-step (build a side image), run with the context as `UPPER_CASE` env.
- `checks.py` *(optional escape hatch)* — Python: define `def checks(cluster, nodes) ->
  list[CheckResult]` for checks not expressible as a declarative verb. Gets the same
  `Cluster` seam (`cluster.exec_rc/logs/file_nonempty/get_nodes`) the built-in asserts use,
  so it's consistent + testable. (The old bash `checks.sh` is gone — all three modules are
  now fully declarative; add a verb to `harness/verify.py` + `harness/checks.py` before
  reaching for the escape hatch.)
- Plus whatever the module needs (config templates, scripts, extra service images, resource generators).

### Components (`components/<name>/`)
A **shared service dependency** (not a test unit — no `checks:`) that multiple modules can pull
in via `components: [<name>]` in their `render.yaml`. Same fragment shape as a module
(`services.yml.j2` + optional `render.yaml`/`prebuild.sh`/`config/*.j2`/`bootstrap/`). The
renderer includes each referenced component once (deduped) and merges its services/volumes. Today:
`components/oidc-server/` — the trivial in-cluster IdP (OIDC discovery + JWKS + `/token` +
`/k8s/token` for k8s SA JWTs), reused by `generic_oidc` (and, once built, `kubernetes`).

### Plans (`plans/<name>.yaml`)
A **multi-module plan**: `name`, `description`, `modules: [a, b]`. `run-plan <name>` composes all
listed modules (+ their transitive components) into ONE cluster, gates each independently (gated-out
modules are SKIPped and left out of the compose), verifies each, and writes one report with per-module
`results-<module>.json`. `run-plan <name>` resolves `<name>` to a plan file or, failing that, a single
module (back-compat). Today: `plans/bots.yaml` (tbot + bound_keypair — composition smoke test).

### Verification (`harness/verify.py`, `harness/cluster.py`; `lib/verify.sh` is a 6-line shim)
`run-plan` calls `harness verify <module> --cluster-id <id>`: the brain parses + verb/arity-
validates the module (invalid → immediate FAIL), then runs each check against the live cluster
and prints `  PASS|FAIL|SKIP <msg>` lines + one `RESULT: PASS|FAIL` (only FAIL fails the run;
SKIP is a neutral not-yet-satisfied soft check), exiting non-zero on FAIL. Evidence is a
first-class **`ProofItem`** (Foundation A): a check no longer welds its proof inline — it
references one or more shared, run-level proof items by id, so several checks can cite ONE proof
and the FULL (untruncated) content is preserved for review. A ProofItem is
`{id (content-hash), kind (log-excerpt|audit-event|node-record|command|file|text), title,
content, lang, source}`, where `source` is a bundle-relative link to the artifact (a per-service
`logs/<svc>.log`, a rendered resource). `collect_proofs` hoists+dedups them. Shown indented
(`↳ <title>` + content) in the console, in `state/<id>/results-<module>.json`
(`{status,verb,args,msg,proof_refs,assertions}` per check — `assertions` are the individual
conditions the verb published, e.g. audit-event `field = value` pairs, rendered as a "Checks
against this proof" list under each proof — + a top-level `proofs` registry + a captured node
inventory), and in the markdown report as a check TABLE linking to per-proof anchored sections
(untruncated, fenced). The report reader falls back to the legacy inline evidence/excerpt shape
for older bundles.
All docker interaction goes through the `Cluster` seam (`harness/cluster.py`) so asserts are
unit-testable with a `FakeCluster` (they never were in bash). Adding a verb = an impl in
`harness/verify.py` `IMPLS` + a `VerbSpec` in `harness/checks.py` (a test enforces they match).
Current verbs: `node_present`/`node_absent`/`node_scope`/`node_count`/`scoped_node_count`,
`log_contains <suffix> <regex…>` (case-insensitive; SKIP on no match),
`log_count <suffix> <eq|ne|lt|le|gt|ge> <n> <regex…>` (assert the COUNT of matching log
lines against a threshold — proves e.g. "≥3 joins drove traffic yet discovery was fetched
≤1×"; proof lists the matched, line-numbered lines),
`audit_event <event-type> [field=value…]` (inspect a STRUCTURED audit event from the JSON file
backend — matches one event of that type where every `field=value` holds, value-compare
case-insensitive; renders the FULL event as pretty-JSON proof. Two lines selecting the same event
dedup to one proof both checks cite. FAIL if none matches), `bot_joined <name>
[method]` (prefers the structured `bot.join` audit event → JSON proof; falls back to scraping the
text log), `output_file`/`no_output_file <suffix> <path>`, `identity_authorized <suffix>
<identity-path> [auth-server]` (runs `tctl --identity … tokens ls`), `identity_scope <suffix>
<identity-path> <scope>` (asserts `tsh status --identity` shows the scope — scope-pin proof),
`tsh_ssh <suffix> [login]` (admin identity), `tsh_ssh_as <suffix> <identity-path> <node-suffix>
[login]` (tsh ssh from a container using ITS OWN identity → `echo harness-ok`; a bot's practical
end-to-end access test).
Node/container args reference the nodename suffix after `<id>-`.

Modules today: `generic_oidc` (agents AND bots join via OIDC JWTs — discovery over a
custom CA via a self-signed `oidc-ca` server + static_jwks, unscoped + scoped), `tbot` (Machine ID bot joins +
identity output, token method), `bound_keypair` (bot joins via bound_keypair with a preset
registration secret), `kubernetes` (bots join via k8s SA JWTs — both `oidc` and `static_jwks`
types — minted by the shared `oidc-server` component), `oidc_caching` (a repeated-join probe
proves the auth server's shared `oidc.CachingTokenValidator` actually caches — N fresh kube
`oidc` joins against a DEDICATED in-cluster IdP, whose isolated request log shows discovery +
JWKS fetched only once across all joins via `log_count`), `oidc_response_limit` (proves the
shared `lib/oidc.OIDCRoundTripper` response-size cap, ~1 MiB, is enforced end-to-end AND fails
fast: three kube-`oidc` bots validate through the caching validator — one joins a well-behaved
IdP (succeeds), two join dedicated hostile IdPs that oversize the discovery doc (which then
HANGS the connection open) or the JWKS, and are denied with the size error at the right fetch
step, producing no identity; the hang case proves the fetch aborts instead of draining an
over-limit body — the behavior removed in the teleport.e→OSS move of this round tripper).
`tbot`/`bound_keypair` differ only in join method + bootstrap + config; a new join-method module
is a ~25-line `services.yml.j2` fragment + `bootstrap/` + `checks:`.
Components today: `oidc-server` (shared IdP; serves the wildcard LE cert so the kube `oidc`
type — system-trusted, no custom-CA — validates it; opt-in HOSTILE flags
`-oversize-endpoints=discovery,jwks` / `-oversize-bytes` / `-hang-after-oversize` make a
dedicated instance bloat + optionally never-close a response, to test a client's size cap).
Plans today: `bots` (tbot+bound_keypair),
`oidc-caching` (generic_oidc + kubernetes + oidc_caching — each gated independently, so on a
target with only `kubernetes` generic_oidc SKIPs while the other two run).

### CLI (`bin/cluster`, `lib/*.sh`)
`doctor` · `validate [module]` · `build --repo` · `up <module> --repo [--id]` · `run-plan <plan|module> --repo [--features a,b] [--version vNN] [--id]`
· `ls` · `logs <id> [svc]` · `admin <id>` · `tctl <id> …` · `tsh <id> …` · `web <id>` · `report <id>` ·
`share <run-bundle|id> [--public]` · `teardown <id|--all>`.
`run-plan <plan|module>` gates each module on `requires_features`/`min_version` (SKIP with a
logged reason — no silent skips), composes the cluster up (or reuses an existing `--id`), verifies
every running module, and writes `runs/<ts>-<id>/`: a rich **`results.md`** (built by `harness
report-md` from the structured data — summary table; a cluster-setup section rendered from
`setup.json` as services/roles/tokens/bots TABLES with source links; node inventory; and a
per-module check TABLE linking to anchored, untruncated proof sections), per-module
`results-*.json`, the renderer's `setup.json` provenance manifest, the raw `console.txt`,
per-service `logs/`, and `rendered/` (compose + config + bootstrap). Leaves the cluster up.
`share <run-bundle|id>` publishes a bundle as a GitHub gist (`gh gist create`, secret by default;
`--public` opts in with a secrets warning): the brain (`harness gist-stage` → `harness/share.py`)
flattens the bundle (gists are flat — `rendered/config/x.yaml` → `rendered--config--x.yaml`) and
rewrites `results.md`'s relative links to gist per-file anchors (`#file-<slug>`; dir links demoted
to text), so the shared report stays navigable. Given a bare id it makes a fresh bundle first.

### Admin access (`lib/admin.sh`)
Teleport's **admin-action MFA** (v15+) blocks user-minted identity files but **exempts
bot identities**. So admin CLI access uses a privileged **bot**, not `tctl auth sign --user`
(that path can't satisfy the MFA requirement). `cluster admin <id>` creates a
`harness-admin` bot (roles `editor,access,auditor`) and a long-running tbot that writes a
renewable identity to volume `harness-admin-<id>` (+ a host copy at `state/<id>/identity`).
`cluster tctl`/`cluster tsh` run the cluster's own image (version-matched) with
`--identity` against `<id>-auth:3025` / the proxy — no login, no MFA. The **web UI is
break-glass** (`cluster web` mints an invite; the browser flow still needs a password and,
if the cluster enforces it, an MFA device).

## Invariants / gotchas (do NOT relearn)
- **`--ent` builds need a license or auth exits 1** ("Failed to load license file … /var/lib/teleport/license.pem").
  `cluster up/run-plan --ent` resolves the clone's bundled test license
  (`$REPO/e/fixtures/license-all-features.pem`, override via `HARNESS_LICENSE_FILE`), and render
  mounts it read-only at `/etc/teleport/license.pem` + sets `auth_service.license_file` (both
  gated on the render `--license-file` arg, so OSS runs are unchanged). Most modules test OSS
  `lib/*` code and run fine as OSS (the default); pass `--ent` only when you want the enterprise
  auth binary (e.g. exercising `e/…`).
- **All ports = the ingress port end-to-end** (proxy `web_listen_addr`, `public_addr`, agent
  `proxy_server`, ingress backend). A public_addr↔dial port split breaks agent reverse tunnels.
- **nginx SNI passthrough, not Traefik** — lima blocks the docker socket even for root
  containers, so label discovery is impossible. Route via `*.map` files + `nginx -s reload`.
- **Never verify TLS with macOS system `curl`** (LibreSSL → bogus 000 / "bad decrypt"). Use
  `python3`/`tsh`/an in-network `curlimages/curl` container. `curl --resolve` needs an IP, not a name.
- **`pipefail` + `grep -q`**: the harness runs `set -o pipefail`, so `docker logs X | grep -q RE`
  returns the producer's SIGPIPE (non-zero) on an early match — looks like "no match". Always
  capture first: `logs="$(docker logs X 2>&1)"; grep -qiE RE <<<"$logs"`. (assert_log_contains does this.)
- **East-west agents dial the FQDN** (via docker network alias), not the service/container name —
  the wildcard cert only matches `*.lab.<domain>`. Intra-cluster gRPC to `auth:3025` is fine
  (mTLS via the identity file's cluster CA, not the proxy cert).
- Editing `nginx.conf` then reload can hit a lima mount stale-read; `docker restart harness-ingress`.
  Adding/removing `*.map` + reload is fine.
- Scoped tokens need `TELEPORT_UNSTABLE_SCOPES=yes` on auth + tctl. An **unscoped** bot can create
  scoped tokens if its **classic** role grants `scoped_token` (the scoped authorizer wraps the
  unscoped checker) — no scoped_role_assignment needed.
- **Bootstrap race**: the auth healthcheck requires BOTH `/healthz` AND `/tmp/bootstrap-done`
  (touched by the shared `auth-entrypoint.sh` after applying `/bootstrap/*.yaml` + `bots.manifest`).
  Without it, a bot `depends_on: auth service_healthy` would start the instant teleport answers
  `/healthz` — before its user exists — and `tbot start` exits 1 on a failed initial join (no retry).
  Bootstrap is now declarative + shared: modules contribute `bootstrap/*.yaml[.j2]` (roles/tokens)
  + `bots:` (render.yaml), and one shared entrypoint applies them all — no per-module entrypoint.
- **Audit events are JSON on disk**: the shared `auth.yaml.j2` sets
  `teleport.storage.audit_events_uri: file:///var/lib/teleport/audit/events`, so the file audit
  backend writes NDJSON (one event per line) IN ADDITION to the text log. The `audit_event` verb
  reads it via `Cluster.audit_events()` (`find … -exec cat`, parse each line as JSON). Event field
  names are the JSON tags from `api/proto/.../events.proto` (source of truth): type=`event`,
  `code`, `success` (bool), `bot_name`, `method`, `token_name`, `impersonator`, `scope`, …. If the
  backend ever isn't emitting, `bot_joined` still works (text-log fallback), but a raw `audit_event`
  line FAILs — validate a new event's field names against a live run.

## Adding a module
(Full step-by-step + gotchas + checklist: `skills/teleport-cluster/references/authoring.md`.)
1. `modules/<name>/` with `module.yaml` (gating + `checks:`), `services.yml.j2` (fragment: its
   bots/agents), `render.yaml` (`components:`, `bots:`, vars), `config/*.j2`, and `bootstrap/*.yaml[.j2]`
   (roles + tokens; NO per-module auth-entrypoint — the shared one applies them). Optionally
   `prebuild.sh`/`checks.py`. For a bot-join module this is ~just the join config + `checks:` — copy
   `modules/tbot/`. To add a declarative verb: an impl in `harness/verify.py` `IMPLS` AND a `VerbSpec`
   in `harness/checks.py` (unit-test with a `FakeCluster`). Shared services (an IdP, a DB) go in
   `components/<name>/` and are pulled in via `components:`.
2. `cluster validate <name>` — catches typo'd verbs / bad arity / schema errors before you spin anything up.
3. Follow the per-cluster rules (auth named `${id}-auth`, wildcard cert, FQDN alias, all-ports=PORT).
4. `cluster up <name> --repo <clone>` to iterate; `cluster run-plan <name> ...` to gate+verify+report.
5. Copy `modules/tbot/` (simplest) or `modules/generic_oidc/` (agents + shared component) as a template.
   A multi-module `plans/<name>.yaml` composes several modules into one cluster.

## Roadmap (not yet built)

### Architecture / DX
- **Python brain — DONE (phases 1–4)**: YAML parsing, gating, `checks:` validation, the verifier,
  AND compose rendering all moved from grep/sed/awk/`lib/assert.sh`/heredoc-`render.sh` into the
  typed `harness/` package (pydantic + real YAML + jinja2 + a docker `Cluster` seam + pytest),
  called by the shell via `pybrain`. Asserts are structured (`{status,verb,args,msg}`) → JSON
  report; `lib/verify.sh` is a 6-line shim; all three modules are fully declarative (bash
  `checks.sh`/`render.sh` retired). **Extract-a-shared-base is DONE**.
- **Composition + shared components + multi-module plans — DONE**: a cluster is composed from a
  base scaffold + shared `components/` + one-or-more module `services.yml.j2` fragments (Python
  YAML-merge); bootstrap is declarative + shared (`bootstrap/*.yaml[.j2]` + `bots:` → one shared
  `auth-entrypoint.sh`). `oidc-server` extracted to `components/`; `plans/<name>.yaml` composes
  several modules into ONE cluster with per-module gating + one report. Verified live on
  `teleport-b`: all 3 modules pass, and `plans/bots.yaml` (tbot + bound_keypair) shares one auth.

### Coverage (new modules / deeper checks)
- **`kubernetes` module + `plans/oidc-caching.yaml` — DONE**: a `kubernetes` module joins bots via
  k8s SA JWTs from the shared `oidc-server` (`/k8s/token`), validated BOTH ways — `oidc` (fetch
  discovery+JWKS at join → the shared `oidc.CachingTokenValidator`, the backport's surface) and
  `static_jwks` (embedded JWKS). The oidc-server serves the wildcard LE cert (`-tls-cert/-tls-key`)
  at `oidc.<lab_domain>` so the kube `oidc` type (no custom-CA) trusts it with no CA install. SA
  tokens are pointed at via `KUBERNETES_TOKEN_PATH`; the static_jwks token is built at bootstrap by
  a hook (`bootstrap/hooks/*.sh`, JWKS fetched from the running oidc-server). Verified live: the
  full `oidc-caching` plan (generic_oidc+kubernetes) passes on teleport-b; on **teleport-e** (the
  actual v18 caching backport) `generic_oidc` correctly gates out (not in that backport) and the
  **kube module passes both types** — the caching backport doesn't break kube joining.
- **More join methods**: `github`, `iam`/`ec2`, `azure`, etc. — each ~ a `services.yml.j2` fragment
  + `bootstrap/` + `checks:` now that composition + shared components exist.
- **Deepen `tbot`**: multiple output types (`ssh`, `kubernetes`, `database`, `application`) with
  artifact + usability checks; and exercise the `tsh_ssh` primitive end-to-end by joining a
  target SSH node and proving the bot identity can actually `tsh ssh` into it (needs a node +
  login RBAC — the primitive exists but no module uses it yet).
- **Scoped coverage** beyond generic_oidc as scoping expands (scoped agents/bots for other methods).

### Build / deploy
- `--target homelab`: enterprise amd64 binary + the scp/systemctl swap one-liner for the
  long-lived homelab cluster (the builder already supports `ent`).
- **Worktree-based build isolation**: build arbitrary branches in a `git worktree` without
  touching the clone's checkout (today `build` uses the clone's currently-checked-out tree).

### Access / TLS / DNS
- Admin CLI via a privileged bot identity is **DONE** (`cluster admin/tctl/tsh`). Remaining
  **web-UI break-glass polish**: optional per-cluster MFA relaxation
  (`cluster_auth_preference.second_factor: off/optional`) so browser login is password-only,
  and/or a passwordless dev-login helper. (Fully headless *web* user seeding is impossible —
  password is only set via the invite flow — so the bot-identity CLI stays primary.)
- `bound_keypair` join for the admin bot (currently token method) as a hardening option.
- **TLS provider**: `mkcert` offline fallback (no domain / air-gapped).
- **Access providers**: cloudflare-tunnel for public access; offline `.test` + dnsmasq DNS
  provider (today relies on the public `*.lab.<domain>` → 127.0.0.1 wildcard record).

### Acceptance goal — DONE
- **The v18 caching-OIDC backport** was driven through the harness (the original motivation):
  `run-plan oidc-caching --repo ~/projects/teleport-e --features kubernetes --version v18` — the
  kube `oidc` + `static_jwks` bots join against the backport's shared caching validator (PASS), and
  `generic_oidc` gates out (that join method isn't in this backport — the harness surfaced it as a
  loud `unknown join method` when `--features generic_oidc` was wrongly passed). On
  `~/projects/teleport-b` (generic-oidc-impl prealpha) the full plan (generic_oidc+kubernetes) passes.
  NOTE the correct `--features` per branch: teleport-e has `kubernetes` (not `generic_oidc`).
