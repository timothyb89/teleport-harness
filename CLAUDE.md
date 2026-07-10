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
Rendered into `state/<id>/` by a module's `render.sh`, then `docker compose up`. The
auth+proxy container is `${id}-auth`, listens on `${PORT}`, mounts the wildcard cert,
joins `teleport-harness` with network alias `<id>.lab.<domain>` (east-west agents dial the
FQDN so TLS matches; the ingress reaches it by container name), `public_addr = <fqdn>:<port>`.

### Build (`lib/build.sh`, SHA-cached)
`build_image <clone> [ent]` cross-builds `teleport`/`tctl`/`tbot` (linux/amd64, glibc) from
the clone's **currently checked-out** working tree — never switches branches — reusing the
clone's prebuilt webassets. Keyed by `git rev-parse HEAD` → `.cache/bin/<sha>-<variant>/`
and image `teleport-harness:<sha>-<variant>`. Repeat builds are instant.

### Python brain (`harness/`, run via `uv`)
The data + decision layer — YAML parsing, feature/version gating, and `checks:`
validation — lives in the typed `harness/` Python package (pydantic models, real
YAML parser), NOT in grep/sed/awk. The shell layer shells out to it via the
`pybrain` helper (`lib/common.sh` → `uv run --project $HARNESS_ROOT harness …`).
Subcommands: `validate [module]` (schema + verb/arity check — used by `doctor`),
`gate <module> [--features] [--version]` (exit 3 == skip), `meta <module> <field>`,
`checks <module>` (emit validated `verb args` lines). Models + gating are unit-tested
(`tests/`, `uv run --extra dev pytest`) — the harness's minimum correctness bar.
A bad `module.yaml` (typo'd verb, wrong arity, unknown key, bad version) now fails
fast with a clear message instead of deep in the verify retry loop. Docker/nginx/
cert/build **plumbing stays in `lib/*.sh`** — the brain owns decisions, not orchestration.

### Module contract (`modules/<name>/`)
- `module.yaml` — gating (`provides_feature`, `requires_features`, `min_version`)
  **plus** the verification spec: a `checks: |` block of `<assert-verb> <args...>` lines
  (the source of truth). `#` comment lines allowed. Parsed + validated by the Python
  brain (`harness/models.py`); run `cluster validate <name>` to check it.
- `render.sh` — invoked with `CLUSTER_ID FQDN PORT IMAGE HARNESS_DOMAIN LAB_DOMAIN OUT`;
  must emit a self-contained `$OUT/docker-compose.yml` (+ configs) per the per-cluster rules above.
- `checks.sh` *(optional escape hatch)* — SOURCED by the verifier; shares `$ASSERT_ID`,
  the cached `$_assert_nodes` JSON, `$_assert_fail`, `_al`, and every `assert_*`. For checks
  not expressible as a declarative verb. No shebang, no `exit`.
- Plus whatever the module needs (config templates, scripts, extra service images, resource generators).

### Verification (`lib/verify.sh` + `lib/assert.sh`)
`run-plan` gets the module's declarative `checks:` from the Python brain (parsed + verb/arity-
validated up front — an invalid module FAILs immediately), dispatches each line to its shared
**assert primitive**, then sources `checks.sh` if present, then prints one `RESULT: PASS|FAIL`.
The declarative vocabulary is the registry in `harness/checks.py` (mirrors `lib/assert.sh`);
adding a verb means adding both an `assert_<name>` in `lib/assert.sh` AND a `VerbSpec` in
`harness/checks.py`. A module's `checks.sh` escape hatch stays free-form bash (runs after the
declarative block, so it can define/call any `assert_*` without registry entry).
Current primitives: `node_present`/`node_absent`/`node_scope` (jq over `tctl get nodes`),
`log_contains <container-suffix> <regex…>`, `bot_joined <name> [method]` (bot.join audit event,
optionally checking the join method), `output_file`/`no_output_file <container-suffix> <path>`
(tbot output artifacts via `docker exec`), `tsh_ssh <suffix> [login]`. Node args reference the
nodename suffix after `<id>-`. Add primitives to `lib/assert.sh` as new areas need them.

Modules today: `generic_oidc` (agents join via OIDC JWTs), `tbot` (Machine ID bot joins +
identity output, token method), `bound_keypair` (bot joins via bound_keypair with a preset
registration secret). `tbot`/`bound_keypair` are near-identical except join method + bootstrap —
a good signal that extracting a shared base (roadmap) would pay off once there's a 4th.

### CLI (`bin/cluster`, `lib/*.sh`)
`doctor` · `validate [module]` · `build --repo` · `up <module> --repo [--id]` · `run-plan <module> --repo [--features a,b] [--version vNN] [--id]`
· `ls` · `logs <id> [svc]` · `admin <id>` · `tctl <id> …` · `tsh <id> …` · `web <id>` · `report <id>` · `teardown <id|--all>`.
`run-plan` gates on `requires_features`/`min_version` (SKIP with a logged reason — no silent
skips), brings the cluster up (or reuses an existing `--id`), verifies, writes `runs/<ts>-<id>/`
(results + per-service logs + rendered config + meta), and **leaves the cluster up**.

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

## Adding a module
1. `modules/<name>/` with `module.yaml` (gating + `checks:`), `render.sh` (emit `$OUT/docker-compose.yml`),
   and optionally `checks.sh` for custom asserts. Add new `assert_*` primitives to `lib/assert.sh` if needed
   (and a matching `VerbSpec` in `harness/checks.py` so `validate` knows the new verb).
2. `cluster validate <name>` — catches typo'd verbs / bad arity / schema errors before you spin anything up.
3. Follow the per-cluster rules (auth named `${id}-auth`, wildcard cert, FQDN alias, all-ports=PORT).
4. `cluster up <name> --repo <clone>` to iterate; `cluster run-plan <name> ...` to gate+verify+report.
5. Copy `modules/generic_oidc/` as the reference implementation.

## Roadmap (not yet built)

### Architecture / DX
- **Python brain — DONE (phases 1–2)**: YAML parsing, gating, and `checks:` validation moved
  from grep/sed/awk into the typed `harness/` package (pydantic + real YAML + pytest), called
  by the shell via `pybrain`. Remaining phases of the same migration:
  - **Phase 3 — port the verifier + assert library to Python**: `assert_*` become typed,
    registered functions returning structured results (list of `{verb,args,status,msg}`),
    enabling a real JSON report alongside the markdown one and unit-testable asserts. `lib/verify.sh`
    shrinks to "collect containers + hand off"; `harness/checks.py` becomes the single source of truth.
  - **Phase 4 — jinja2 templating + shared compose base**: replace the per-module `envsubst` +
    heredoc `render.sh` (which is why the three modules duplicate ~90% of their compose) with a
    jinja2 base template rendered by the brain. This subsumes "extract a shared base" below —
    a new join-method module becomes ~just its join config + `checks:`.
- **Extract a shared base** (folded into Phase 4 above): `generic_oidc`, `tbot`, and `bound_keypair`
  duplicate the same auth+proxy service, "bootstrap a bot + token" step, and negative-bot scaffold.
  We now have the 3 examples needed to factor it correctly; do this via the jinja2 base before adding a 4th module.
- **Multi-module plan files** (`plans/*.yaml`): currently a "plan" == a single module. A plan
  file would list several modules (with per-module gates) run + reported together.

### Coverage (new modules / deeper checks)
- **More join methods**: `kubernetes` (in-cluster + the OIDC path that shares the caching
  validator), `github`, `iam`/`ec2`, `azure`, etc. — each ~ a join config + `checks:` once the
  base is extracted.
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

### Pending acceptance goal
- **Drive the v18 `generic_oidc` backport** through the harness (the original motivation):
  `run-plan generic_oidc --repo <clone-on-v18-branch> --features generic_oidc --version v18`,
  then iterate on real findings. Proven across 3 join methods on the prealpha; not yet run
  against the actual v18 backport branch.
