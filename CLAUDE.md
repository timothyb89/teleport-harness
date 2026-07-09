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
- Host tools: `mkcert` (fallback TLS only), `jq`, `python3`, the messense glibc cross
  toolchain `x86_64-unknown-linux-gnu-gcc` (override via `HARNESS_CC`).
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

### Module contract (`modules/<name>/`)
- `module.yaml` — gating (`provides_feature`, `requires_features`, `min_version`, grep-parsed)
  **plus** the verification spec: a `checks: |` block of `<assert-verb> <args...>` lines
  (the source of truth). `#` comment lines allowed.
- `render.sh` — invoked with `CLUSTER_ID FQDN PORT IMAGE HARNESS_DOMAIN LAB_DOMAIN OUT`;
  must emit a self-contained `$OUT/docker-compose.yml` (+ configs) per the per-cluster rules above.
- `checks.sh` *(optional escape hatch)* — SOURCED by the verifier; shares `$ASSERT_ID`,
  the cached `$_assert_nodes` JSON, `$_assert_fail`, `_al`, and every `assert_*`. For checks
  not expressible as a declarative verb. No shebang, no `exit`.
- Plus whatever the module needs (config templates, scripts, extra service images, resource generators).

### Verification (`lib/verify.sh` + `lib/assert.sh`)
`run-plan` runs the module's declarative `checks:` through shared **assert primitives**, then
sources `checks.sh` if present, then prints one `RESULT: PASS|FAIL`. The vocabulary is OPEN —
any `assert_<name>` function (in `lib/assert.sh` or a module's `checks.sh`) is a usable verb.
Current primitives: `node_present`/`node_absent`/`node_scope` (jq over `tctl get nodes`),
`log_contains <container-suffix> <regex…>`, `tsh_ssh <suffix> [login]`. Args reference the
nodename suffix after `<id>-`. Add primitives to `lib/assert.sh` as new areas need them
(e.g. bot-join / identity-output asserts for tbot modules).

### CLI (`bin/cluster`, `lib/*.sh`)
`doctor` · `build --repo` · `up <module> --repo [--id]` · `run-plan <module> --repo [--features a,b] [--version vNN] [--id]`
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
   and optionally `checks.sh` for custom asserts. Add new `assert_*` primitives to `lib/assert.sh` if needed.
2. Follow the per-cluster rules (auth named `${id}-auth`, wildcard cert, FQDN alias, all-ports=PORT).
3. `cluster up <name> --repo <clone>` to iterate; `cluster run-plan <name> ...` to gate+verify+report.
4. Copy `modules/generic_oidc/` as the reference implementation.

## Roadmap (not yet built)
- Multi-module plan files (`plans/*.yaml`); currently a "plan" == a module.
- `--target homelab` (enterprise amd64 binary + scp/systemctl swap for the real cluster).
- Worktree-based build isolation for arbitrary branches without touching the clone's checkout.
- Admin CLI via a privileged bot identity is DONE (`cluster admin/tctl/tsh`). Remaining web-UI
  polish: optional per-cluster MFA relaxation (`second_factor: off/optional`) so break-glass
  browser login is password-only, and/or a passwordless dev-login helper.
- bound_keypair join for the admin bot (currently token method) as a hardening option.
- `mkcert` offline TLS provider; cloudflare-tunnel public access provider.
