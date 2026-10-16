# Design: a remote Kubernetes backend

Status: **phases 1–5 built; Phase 6 in progress; Phase 0's "from Beams" half waiting on
outer-Teleport onboarding.** See **Current state and handoff** right below. Companion to
`docs/kubernetes.md`, which covers the *in-cluster k3s for operator tests*; this document
is about running **the whole harness cluster** in a remote Kubernetes cluster instead of
local docker compose.

## Current state and handoff (2026-10-16)

Everything below the line "Motivation" is the design, annotated with what was built
(**Status:** paragraphs in §1–§6, **Done** notes in §8). This section is the summary a new
session needs to resume.

### Where each phase stands

| Phase | State | Commit |
|---|---|---|
| 0 host infra + spike | infra built and VM-tested (lima + CoreOS); **"from Beams" half NOT done** | eadefc7 |
| 1 backend seam | done | 2f5b335 |
| 2 translator | done | 23eff88 |
| 3 image publishing | done (second client = a second checkout on the mac, not Beams) | 4e88fe8 |
| 4 end to end | done: `bots` 10/10, `oidc-caching` 52/52 identical to docker | c906dc2 |
| 5 access + visibility | done between two mac clients | 44d5271 |
| 6 breadth | **in progress** (below) | e2862d5 |
| 7 operator modules | not started (`requires_backend: docker`) | |
| 8 TPM via KubeVirt | not started; spike on the real host | |

### Phase 6 so far

Built:
- `KubeCluster.restart_auth`: kill the `teleport` child, not PID 1 (the kernel shields a
  namespace's init from signals it doesn't handle). The kubelet restarts the container
  in place, and the PVCs keep auth's data and the apply-on-startup files.
- **Per-namespace resolver replacing hostAliases** (§3): it fixed a real k8s/docker
  divergence in `terraform_native_join_lb`.
- **Unhealthy budget:** a dependency that never passes its healthcheck fails the apply
  after compose's own budget (`start_period + interval × retries + timeout`), counted from
  when the containers start running. It no longer waits the full 600s.
- **Agent result through the seam:** `Cluster.clear_state_file`, and `run_agent` checks
  the agent's result through `state_file`, so a k8s workbench's result, which stays in
  the pod's volume, is found. Before this, the agent always "wrote nothing" on k8s.

Verdict parity, check by check, same source on both backends (lima k3s vs local docker):

| Module | Source | Docker | k8s | Same? |
|---|---|---|---|---|
| bound_keypair_status | package v18.11.0-rc.2 | 11 P / 6 F | 11 / 6 | yes |
| scoped_usage_mode | package | 45 / 23 | 45 / 23 | yes |
| workload_identity_sds_override | package | 6 / 2 | 6 / 2 | yes |
| workload_identity_slow_start | package | 7 / 6 | 7 / 6 | yes (netem sidecar shapes: `shaped yes`) |
| terraform_bot | repo `~/projects/teleport` @ 0e4aa11cf8c2 (v19 master) | 0 / 3 | 0 / 3 | yes |
| terraform_generic_oidc | repo | 1 / 3 | 1 / 3 | yes |
| terraform_native_join_lb | repo | 18 / 16 | 18 / 16 | yes, after the resolver fix (was 14 / 20) |
| bound_keypair_apply_on_startup | repo | 5 / 8 | 5 / 8 | yes (`restart_auth` exercised) |
| docs_bound_keypair (AI agent) | repo | 4 / 0 | 4 / 0 | yes, rerun after the agent-result change |
| scoped_app_access | repo | never comes up | never comes up | same failure; k8s now fails fast (244s, was 620s) |

With the resolver in place, the Phase 4 plans were rerun on k8s: `oidc-caching` passes
(75s, down from 90s now that aliases add no waits) and `bots` passes. The admin pod
(`admin`, in-cluster `tsh`/`tctl`, native `tsh` through the ingress) was checked again
too.

The failures are not backend problems. On the RC they are the fixes these modules guard;
on master, three modules have drifted (the next section). Parity of FAILING verdicts is
still evidence: the same assertions fail for the same reasons.

### Findings about the modules/Teleport, not the backend (need a human decision)

- `terraform_bot`/`terraform_*` on master: the provider's `teleport_bot` resource changed
  shape (top-level `name`/`roles`; `version`/`metadata`/`spec` rejected), so the module's
  `main.tf` no longer applies. Module drift; update the HCL.
- `bound_keypair_apply_on_startup` on master: the pre-seeded bot is refused with `either
  an initial public key or registration secret is required`, so the registration secret
  seeded through `.status` doesn't take effect. Could be a regression or an intended
  change.
- `scoped_app_access` on master: the scoped App agent's inline join string
  `/harness-apps::app-agent-token:<secret>` is treated as a FILE PATH because it starts with
  `/` (`open /harness-apps::…: no such file or directory`), so the agent never joins.
  Probably a real bug in how the agent reads `token_name`.

### Next steps, in order

1. `docs_generic_oidc` on both backends (same machinery as docs_bound_keypair). Then
   Phase 6 is complete apart from the master-drift findings above.
2. Phase 0 "from Beams": onboard the host to the outer Teleport (kube agent + a TCP app
   `harness-ingress` → the node's :9443), set `FORWARD=tsh-app:harness-ingress`, repeat
   the bots run from Beams, and measure `kubectl exec` latency through `tsh kube` (risk
   list at the end of this doc). `tsh-app` forward mode is the only untested mode.
3. Real hardware: `infra/host/` (CoreOS) on the box; `test-libvirt.sh` is untested.
4. Phase 7 (operator: vcluster or privileged k3s-in-pod) and Phase 8 (KubeVirt TPM spike).
5. Smaller open items: registry GC (per-cluster artifact tags pile up); in-cluster ACME
   CronJob (today `infra/k8s/import-cert.sh` copies the cert from the docker backend's
   `harness-certs` volume); five leaked docker admin tbots and volumes from before Phase 1
   plus three exited docker clusters (`c303c4a`, `c96337b`, `ce54506`) await the user's
   cleanup.

### Resuming on another machine

Not in git, so recreate them:
- **The k3s stand-in:** `limactl start --name harness-k3s infra/lima/k3s.yaml`, then
  `infra/lima/sync.sh`, then `infra/k8s/import-cert.sh` (needs a wildcard cert in the
  docker `harness-certs` volume: run any docker-backend `cluster up` once, or adapt
  the script to read a cert file). Details: `infra/README.md`.
- **A target, e.g. `targets/lima-k3s.env`:** your `default.env` (domain, DNS token,
  ACME email), plus:
  ```
  BACKEND=k8s
  KUBECONFIG=$HOME/.lima/harness-k3s/copied-from-guest/kubeconfig.yaml   # write it out literally
  KUBE_CONTEXT=default          # REQUIRED; the harness never uses an ambient context
  INGRESS_PORT=9443
  # FORWARD=none                # lima forwards 9443 itself; tsh-app:<app> from Beams
  ```
- **Tools:** `uv`, `kubectl`, `jq`, `python3`; `crane`/`buildctl` download themselves
  (pinned, checksummed: `lib/tools.sh`). `--repo` builds also need the cross toolchain.
- **Throwaway or rebuildable:** `.cache/` (binaries and the tool downloads), the
  registry's contents (the cache rebuilds on first push), and `state/`. A live k8s cluster
  made elsewhere can be picked up with `cluster attach <id>`.

Smoke test after setup: `bin/cluster --target lima-k3s doctor`, then
`bin/cluster --target lima-k3s run-plan bots --package <teleport-*-linux-amd64-bin.tar.gz>`
(expect 10/10), and `teardown`.

How the parity table was made: run the same module with the same source on both targets
(`run-plan <m> <source> --id <id>`), then diff
`jq -r '.results[] | "\(.status) \(.verb) \(.args|join(" "))"' state/<id>/results-<m>.json`
between the two, after replacing each cluster id with a placeholder (`sed s/<id>/ID/g`).

### Rules that must not be relearned

- Every k8s kubectl call is pinned to the cluster's recorded KUBECONFIG + KUBE_CONTEXT
  (`use_cluster`). An unpinned call once reached a WORK cluster through the default
  kubeconfig. Never add a k8s code path that falls back to the ambient context.
- Harness images are single-platform amd64 MANIFESTS, never an index (§4).
- A Dockerfile that compiles must cross-compile (`--platform=$BUILDPLATFORM`): buildkitd
  on an arm64 node runs amd64 `RUN` under qemu-user, where Go crashes.
- Both ingresses route the FQDN AND `<hex(fqdn)>.teleport.cluster.local` (native tsh);
  nginx needs `map_hash_bucket_size 256`.
- Aliases resolve through the namespace resolver at lookup time; don't reintroduce
  hostAliases.

## Motivation

Teleport Beams microVMs cannot run docker or rootless podman (no writable cgroupfs, no
tun/fuse, root lacks the capabilities). They *can* reach resources through Teleport. So:
a k3s box on the home network, onboarded to the outer Teleport cluster, reachable with
`tsh kube`, hosts the disposable clusters; the harness drives it with `kubectl` from
Beams **or** from the laptop, and both can reach the test clusters as users.

## Goals / non-goals

Goals
- Every existing source (`--repo` / `--package` / `--binary`) works from a docker-less
  client, keeping the content-keyed build cache.
- **Modules are not rewritten.** Compose stays the authoring format; the k8s backend
  *translates* the rendered `docker-compose.yml`.
- The verifier, reports, `share`, gating and plans are backend-agnostic.
- Test clusters are reachable as a user (tsh, tctl, browser) from Beams AND the laptop,
  with the real LE wildcard cert and no `--insecure`.
- One test cluster == one namespace: `teardown` is `kubectl delete ns`, and leaks expire.

Non-goals
- Replacing the docker backend. It stays the default and the fast local loop.
- `tpm` before Phase 8 (it needs a VM with a TPM; see Phase 8 for the KubeVirt route).
- Operator modules in phase 1 (CRDs are cluster-scoped; see Phase 7).
- Exposing test clusters to the internet.

## Architecture

```
 client (laptop OR Beams VM)                       k3s host (home LAN, linux/amd64)
 ┌──────────────────────────────────┐            ┌───────────────────────────────────────────┐
 │ bin/cluster  (BACKEND=k8s)       │  kubectl   │ ns harness-system                         │
 │   build: go cross-build / unpack │──(tsh kube)│   traefik      :9443 (hostPort, LAN)      │
 │   publish: crane append ─────────┼──┐        │   registry     :5000 svc / :30500 node    │
 │   render → kube-render → apply   │  │        │   buildkitd    (kube-pod:// via exec)     │
 │   verify: KubeCluster (exec/logs)│  │        │   acme CronJob → Secret wildcard-cert     │
 │                                  │  │        │   janitor CronJob (TTL namespaces)        │
 │ tsh / tctl / browser             │  │        │                                           │
 │   <id>.lab.<domain>:9443         │  │        │ ns th-<id>   (one per test cluster)       │
 │   → 127.0.0.1:9443 ──forwarder───┼──┴───────▶│   IngressRouteTCP  SNI <id>.lab… → auth   │
 │     (tsh proxy app harness-ingress)          │   pods: <id>-auth, <id>-tbot, …           │
 └──────────────────────────────────┘            └───────────────────────────────────────────┘
```

Two independent paths from the client:
- **Control plane:** `kubectl` through `tsh kube` (Teleport kube access) for apply / exec /
  logs / wait / delete.
- **Data plane:** one local TCP forwarder on `127.0.0.1:9443` leading to the Traefik
  ingress. It serves every test cluster (SNI routes at the far end) and the registry.

## 1. One-time host setup (`infra/k8s/`, applied once by hand)

| Piece | What | Why |
|---|---|---|
| k3s, **amd64** host | stock k3s; Traefik/servicelb defaults may stay | amd64 matches every harness image; no emulation |
| `harness-system/traefik` | a **dedicated** Traefik (kubernetesCRD provider only), entrypoint `harness` on `:9443`, LoadBalancer Service (klipper binds the host port) | CRD routes live IN the test namespace, so deleting the namespace deletes the route — no reload step, nothing to unregister |
| `harness-system/registry` | `registry:2` + PVC; ClusterIP `:5000` + NodePort `30500` | holds teleport images, side images, artifact images |
| `registries.yaml` on the node | mirror `registry.harness:5000` → `http://localhost:30500` | gives every pod ONE canonical pull name (see §4) |
| `harness-system/buildkitd` | buildkitd Deployment, its registry config pointing `registry.harness:5000` → `http://registry.harness-system.svc:5000` | builds the few side images that come from Dockerfiles |
| acme CronJob | acme.sh DNS-01 (CF token Secret) → Secret `wildcard-cert` with SANs `*.lab.<domain>` | the same issue-once LE cert as today, now as a Secret |
| janitor CronJob | deletes namespaces labelled `harness.teleport/expires-at` < now | Beams VMs die without tearing down |
| Traefik route for the registry | `HostSNI(registry.lab.<domain>)`, **TLS terminated** with `wildcard-cert` → `registry:5000` | pushes go through the same forwarder with real TLS |
| Outer Teleport | kube access to this k3s (the kube agent); a **TCP app** `harness-ingress` → `tcp://traefik.harness-system.svc:9443` (the kube agent chart can also serve it: `roles: kube,app`) | the data-plane forwarder for anywhere that is not the home LAN |

**Status: built, in `infra/`** (bring-up, what was verified, and the gotchas:
`infra/README.md`). The host is Fedora CoreOS + k3s from one Butane file; the lima
stand-in (`infra/lima/`) applies the same manifests. Differences from the table above:
Traefik's CRDs are a vendored manifest, not a chart (too big for a Helm release); the
registry Service has a fixed ClusterIP (`10.43.250.250`) so buildkitd can pin
`registry.harness` to it; and the registry route carries an `http/1.1`-only TLSOption.
**Not built yet:** the ACME CronJob (`infra/k8s/import-cert.sh` copies the docker
ingress's cert in the meantime), onboarding to the outer Teleport (kube agent + the
`harness-ingress` TCP app), and the RBAC hardening below.

RBAC: the harness identity needs namespace create/delete plus full rights inside them —
effectively admin on this box, so **the box is dedicated to the harness**. Hardening
(later): a Teleport role whose `kubernetes_resources` limits namespaces to `th-*` and
`harness-system`.

Target config (`targets/remote.env`):
```
BACKEND=k8s
KUBE_CONTEXT=<teleport-cluster>-<kube-cluster>   # from `tsh kube login`
INGRESS_PORT=9443                                # ≠ the local docker ingress (8443)
FORWARD=tsh-app:harness-ingress                  # or lan:<k3s-host> | kubectl
REGISTRY_PUSH=registry.lab.<domain>:9443         # through the forwarder
REGISTRY_PULL=registry.harness:5000              # what pods reference
CLUSTER_TTL=24h
```
`HARNESS_DOMAIN`/DNS are unchanged: `*.lab.<domain> → 127.0.0.1` keeps working because
every client puts its forwarder there. Using a different port per target keeps local
docker clusters and remote ones from colliding on the laptop.

## 2. The backend seam

**Status: built (Phase 1).** The authoritative op list and contract is the header of
`lib/backend.sh`; the table below was the sketch it grew from. Differences: `exec` and
`exec_i` are separate (stdin attached only when asked, so an exec inside a `while read`
loop can't eat the loop's input); the admin plumbing is five ops (`admin_up`,
`admin_ready`, `admin_identity_present`, `admin_copy_identity`, `admin_run`); the
image ops gained `image_version`/`binary_version` for the version probes; and
`backend_use` refuses a backend that is missing any op, so `k8s.sh` can't ship half-done.
On the Python side: `make_cluster` + `Cluster.exec_argv`, and the agent's MCP tool gets
`WORKBENCH_EXEC` (a JSON argv prefix) instead of a container name.

Before Phase 1, `lib/cluster.sh`, `lib/report.sh`, `lib/admin.sh`, `lib/agent.sh`, `lib/ingress.sh`
and `harness/cluster.py` called docker directly. The seam, one per side:

**Shell** — `lib/backend/docker.sh` (today's code, moved unchanged) and
`lib/backend/k8s.sh`, selected by `BACKEND`:

| Function | docker | k8s |
|---|---|---|
| `backend_publish_image` | `docker build` (today's `build_image` tail) | `crane append` onto the base (§4) |
| `backend_build_side_image <tag> <ctx>` | `docker build` | `buildctl --addr kube-pod://…` push |
| `backend_up <id> <out>` | `compose up -d` + `register_route` | `harness kube-render` → apply in waves (§3) |
| `backend_wait_healthy <id>` | `docker inspect …Health` | `kubectl wait pod/<id>-auth --for=condition=Ready` |
| `backend_teardown <id>` | `compose down -v`, rm images, lima | `kubectl delete ns th-<id> --wait=false` |
| `backend_logs <id> [svc]` | `compose logs` | `kubectl logs` (+ `--previous` when restarted) |
| `backend_exec <id> <svc> …` | `docker exec` | `kubectl exec <id>-<svc> -c main --` |
| `backend_ls` | state dirs + `docker inspect` | namespaces by label (§6) |

`prebuild.sh` scripts stop calling `docker build` directly and call
`backend_build_side_image` (today only `components/oidc-server`; `k8s-runner` is §7).
Its `docker save` is k8s-runner-specific and stays docker-only.

**Python** — `harness/cluster.py` grows `KubeCluster(Cluster)`, picked by a factory that
reads `BACKEND` from `meta.env`. It is mechanical: every `DockerCluster` method is one
`docker …` call, and each maps to a `kubectl …` call against `pod/<id>-<suffix>`. Specifics:
- `audit_events` stays `find … -exec cat` via exec, but its result is **cached for one
  verify pass**. Each exec now crosses Teleport, so repeated identical calls get expensive.
- `tsh_ssh` runs `tsh` via `kubectl exec` in the admin pod rather than `docker run`.
- `restart_auth` → `kubectl exec <id>-auth -- kill 1`; the auth pod is
  `restartPolicy: Always` so kubelet restarts the container *in place* (volumes survive,
  like `docker restart`). The container's writable layer does NOT survive, so `/tmp`
  markers and anything else must live on a volume (see the apply-on-startup rule in §3).
- `state_file` falls back to pulling from the pod, using the mount map that `kube-render`
  writes (§3, writable mounts).

`harness/agent_mcp.py` keeps its single fixed target. The exec prefix (`docker exec` vs
`kubectl --context … exec`) comes from the environment set by `lib/agent.sh`, so the
agent still cannot pick a target.

## 3. The translator: `harness kube-render --state-dir state/<id>`

**Status: built (Phase 2)** — `harness/kube.py`, with a focused test per rule and a golden
snapshot of every shipped module (`tests/test_kube.py`, `tests/golden/kube/`). 17 modules
translate; `tpm` and `operator_generic_oidc` are refused by `requires_backend: docker`.
What implementation changed from the sketch below:
- **Names resolve through two portless headless Services per compose service** (`<svc>`
  and `<id>-<svc>`, `publishNotReadyAddresses`) — compose services declare no ports, and a
  ClusterIP Service needs them. Verified on k3s: both names resolve to a NOT-ready pod, as
  docker DNS does. The ingress gets its own ClusterIP Service on the proxy port.
- **Aliases are all dotted `*.lab.<domain>` names** (seven owners across the modules), and
  each namespace has its own resolver for them (Phase 6; the first design used
  `hostAliases`, see below): a CoreDNS pod `harness-dns` rewrites each alias to its owner's
  headless Service and forwards everything else to the cluster DNS, and every pod gets it
  through `dnsPolicy: None` + `dnsConfig`, with the namespace's search path, so short
  names still work. That is docker's semantics: resolved at LOOKUP time, so aliases impose
  no start order. It can't live in the cluster's own DNS, because every test cluster has its
  own `oidc.lab.<domain>`.
  **Why not hostAliases (the Phase 2 design):** a hostAlias needs the owner's pod IP
  before a pod is created, so every alias user had to wait for the owner, and a cycle
  could only be broken by dropping the alias. `terraform_native_join_lb` has exactly that
  cycle (auth needs `oidc.lab`, oidc waits on lb, lb waits on auth), so auth could not
  reach its IdP on k8s while docker passed. `oidc-caching` only worked because its IdPs
  happened to start before auth.
- **No NetworkPolicy needed:** the only networks are `internal` and the external
  `teleport-harness`, with no isolation topology. A network with any other key is refused.
- **`platform` is dropped**, not refused: modules pin `linux/arm64` on public multi-arch
  images to run natively on the docker host, which a node does anyway.
- **A `$` anywhere is refused** (compose and k8s interpolate differently; there are none).
- **Object names derive from the mount path RELATIVE to the state dir**, so a cluster's
  manifests don't change with where its state dir lives.
- **ConfigMap volumes:** a single-file mount uses `subPath` = the item's path (not its
  sanitized key); seeding copies from `<cm>/..data/`, since the volume root also holds the
  kubelet's `..data`/`..<timestamp>` bookkeeping.
- **The auth pod is always `restartPolicy: Always`**, so `restart_auth` can kill its process
  and have the kubelet restart the container in place.
- **Artifact images need `sh` + `cp`** (the init container runs `cp -R /artifact/. /dst/`),
  so Phase 3 builds them on busybox.

Input: the rendered `docker-compose.yml` plus the render metadata already in the state
dir. Output: `state/<id>/kube/manifests.yaml` and `kube/plan.json` (apply waves + mount
map). **Unknown compose keys are a hard error naming the key and service.** This is the
same fail-fast rule as `validate`, so a new module using an untranslated feature fails at
render time instead of halfway through a run.

| Compose | Kubernetes |
|---|---|
| project `teleport-harness-<id>` | Namespace `th-<id>`, labels `harness.teleport/cluster-id`, `…/expires-at`, `…/source`; PSA `privileged` (netem/privileged modules need it) |
| service + `container_name: <id>-<svc>` | **bare Pod** `<id>-<svc>` (not a Deployment: compose semantics are one named container) + a ClusterIP Service of the same name, so the name resolves as it does today. Container is named `main`. |
| `image` | as-is, except harness-built images, which are rewritten to `REGISTRY_PULL/<tag>` (the set is recorded by `backend_publish_image` / `backend_build_side_image`) |
| `command` / `entrypoint` | `args` / `command` |
| `environment` | `env` |
| `restart` (absent / `no`) | `restartPolicy: Never`; `always`/`unless-stopped` → `Always`; the auth pod is always `Always` (restart_auth) |
| `healthcheck` | `readinessProbe` exec (`CMD-SHELL` → `sh -c`); interval/timeout/retries map directly, `start_period` → `initialDelaySeconds`. Readiness never kills a pod, the same as a compose healthcheck. |
| `depends_on` | **host-side apply waves**: topologically sort, apply a wave, `kubectl wait` on the conditions the next wave needs (`service_healthy` → Ready, `service_started` → Running/Initialized, `service_completed_successfully` → `jsonpath={.status.phase}=Succeeded`). No in-cluster RBAC or waiter image, and it matches how compose orders startup. |
| network aliases (the auth FQDN, `oidc.<lab>`) | *(as built: a per-namespace CoreDNS rewriting each alias to its owner's Service, see above)* phase 1 applies all Services; their ClusterIPs then go into **`hostAliases`** on every pod. Pods would otherwise resolve `*.lab` publicly to 127.0.0.1, so this is the k8s equivalent of the docker alias, and TLS still matches the wildcard. |
| `networks:` beyond aliasing | phase 1: one flat namespace network. A module that relies on *isolation* between networks declares it and gets NetworkPolicies (k3s ships a netpol controller) in Phase 6. |
| `hostname` | Pod `hostname` (nodename defaults to it — `scoped_usage_mode` depends on this) |
| `cap_add` / `privileged` / `user` / `tmpfs` | `securityContext` / `runAsUser` / `emptyDir{medium: Memory}` |
| `network_mode: service:X` | the dependent becomes a **second container in X's pod** (a pod shares one netns). Its Service name becomes an alias of X's. Covers the netem shaper. |
| `platform` | dropped (amd64 node) |
| named volume | PVC (`local-path`, RWO; single node, so multi-pod RWO is fine) |
| external `harness-certs` | Secret `wildcard-cert` copied from `harness-system`, mounted at `/certs` with today's filenames |
| license bind | Secret `license` |

**Bind mounts** are classified by host path and mode:

| Class | Rule | Becomes |
|---|---|---|
| small read-only (`{{ out }}/config`, `/bootstrap`, `{{ scripts }}`, `{{ shared_scripts }}`, `{{ module_dir }}/…`) | total ≤ ~900 KiB | ConfigMap (files keyed by flattened path, mounted with `items`, mode 0755 for scripts) |
| large read-only (`{{ out }}/tf-plugins`, `{{ repo }}/docs`) | over the ConfigMap budget | an **artifact image**: `crane append` the dir as one layer onto busybox, content-keyed tag; an init container copies it into an `emptyDir`. Use an `image:` volume (ImageVolume) instead where the k3s version supports it. |
| read-write, seeded (`{{ out }}/apply-on-startup`) | `rw` mount whose source has content | init container copies a ConfigMap into an `emptyDir` → writable, survives a container restart |
| read-write, host reads back (`{{ out }}/agent/out`, observations) | `rw` mount the host reads later | `emptyDir`, recorded in `plan.json` as `relpath → (pod, path)`; `KubeCluster.state_file` / the report step **pull** it with `kubectl cp` |

Golden tests: render every module's compose, translate it, and snapshot the manifests.
Modules that can't translate yet are asserted to fail **with their named reason**
(`tpm` → "requires a docker host").

Gating: add `requires_backend: docker` (module/component level, inherited like
`requires_repo`). `plan-resolve` SKIPs such a module with the unit's name as the reason,
and `kube-render` refuses outright.

## 4. Images without a docker daemon

**One canonical pull name.** A registry stores content by repository path, not host
name. Pushers use whatever address reaches it (`REGISTRY_PUSH`, through the forwarder);
pods always reference `registry.harness:5000/…`, which the node's `registries.yaml` and
buildkitd's config both map to the real endpoint. That keeps the image name in a
rendered manifest the same no matter where it was pushed from.

**Status: built (Phase 3), in `lib/backend/k8s.sh`.** What was built, and where it
differs from the sketch:

- **Tools.** `crane` and `buildctl` are fetched on first use into `.cache/tools/` at a
  pinned version, checked against sha256 sums recorded in `lib/tools.sh` (Beams has no
  package manager to rely on). A copy on PATH is used only if it is the pinned version.
- **Base image** (`debian:bookworm-slim` + ca-certificates/curl/openssl/bash/jq): built by
  buildkitd from `lib/backend/k8s/base.Dockerfile` and tagged with that file's hash, rather
  than hand-pinned by digest. Any client builds it on first use; editing the file rebuilds it.
- **Teleport image**: `crane append` of **one layer per binary** onto the base. The
  layers come from `harness layer-tar` (`harness/images.py`), which writes them
  deterministically (sorted entries, mtime 0, root:root, fixed modes). That determinism is
  what makes the registry a cache shared by every client: the same binary staged on two
  machines is the same blob, so it is uploaded once. Verified: a forced republish from a
  second client with an empty `.cache` produced the identical manifest digest and uploaded
  nothing, and `--binary` on the unpacked files of a `--package` deduplicated to the same
  image.
- **Smoke test kept.** The docker backend's `RUN teleport version && …` survives as a pod
  run against a `<tag>-unverified` push, and only a passing image gets the real tag, so a
  broken image can never become a cache hit. The output is cached as the image's version
  (`.cache/k8s/probe/`).
- **Version of a `--binary` from a non-linux/amd64 client** (the mac): it can't run the
  binary locally, so it pushes base + the teleport layer as `teleport-harness-probe:<sha>`
  and runs `teleport version` in a pod. That layer is byte-identical to the real image's, so
  the later push skips it. A linux/amd64 client (Beams) just runs the binary.
- **Side images**: a new backend op, `build_side_image <context> <tag>`, which component
  prebuilds reach through `cluster side-image` (on docker: the old `docker build`).
  **Gotcha:** buildkitd on an arm64 node runs amd64 `RUN` steps under its OWN bundled
  qemu-user, not the node's Rosetta, and the Go toolchain crashes there (`fatal error:
  lfstack.push`) while `apt-get` survives. So a Dockerfile that compiles must
  cross-compile instead (`FROM --platform=$BUILDPLATFORM`, `GOARCH=$TARGETARCH`, as
  `components/oidc-server/build/Dockerfile` now does). Two traps: an `ARG BUILDPLATFORM`
  with a DEFAULT overrides BuildKit's own value, and the docker backend's legacy builder
  doesn't set these args at all, so `be_docker_build_side_image` passes them.
- **Artifact images**: `k8s_publish_artifacts <state-dir>` (called by the Phase 4
  executor) layers each source under `/artifact` onto an amd64 busybox mirrored into the
  registry. It pushes once under a content tag `harness-artifact:c<sha12>`, then
  `crane tag`s the per-cluster name the manifests use. Verified with the 140 MB teleport
  `docs/` tree: 3.2s the first time, then 0.6s and no upload. The image's own
  `cp -R /artifact/. /dst/` copies all 1561 files on the node.
- Public images (`curlimages/curl`, httpbin, terraform, nginx, envoy, k3s) are pulled by
  the node straight from upstream, unchanged.

Timings on the lima stand-in (local registry, so they measure work, not bandwidth): the
first `cluster build --package` took 65s, 44s of which was the one-time base image build;
a cache hit from a fresh client took 3.6s.

**Every harness-published image is a single-platform MANIFEST, never an index.** This is
what lets an arm64 node (the lima stand-in, `infra/lima/k3s.yaml`, or arm hardware) run
the amd64 harness under Rosetta/qemu binfmt. Verified on k3s v1.36.5 / containerd 2.3.4,
arm64 + Rosetta, all pulled through the `registry.harness:5000` mirror:

| Image | Result |
|---|---|
| busybox, multi-arch index (control) | pulls the arm64 entry, `uname -m` = aarch64 |
| busybox, amd64 single manifest (`crane copy --platform linux/amd64`) | **runs**, `uname -m` = x86_64 |
| busybox, index containing only amd64 (`crane index filter`) | **`ErrImagePull`: `no match for platform in manifest`** |
| `teleport-harness:<key>` (`docker save` → `crane push`) | **runs**: teleport/tctl/tbot/tsh v18.11.0 |

containerd applies its platform matcher only when choosing an entry from an INDEX. A bare
manifest is pulled as-is, and the kernel's binfmt handler runs it. So: `crane append`
onto the base pinned by its **amd64 manifest digest** (not the index digest), push buildkit
side images with `--platform linux/amd64` and no attestations (attestations turn the
output into an index), and mirror any amd64-only public image with `crane copy --platform
linux/amd64` before referencing it. On an amd64 node none of this matters, but keeping
the rule makes the backend architecture-agnostic for free.

The Go cross-build, package unpack and binary checks in `lib/build.sh` are untouched.
Only the tail of `build_image` (the `docker build`) moves behind `backend_publish_image`.

## 5. Access: the forwarder

**Status: built (Phase 5)** as `cluster forward [start|stop|status]` (be_k8s_forward), which
`up`/`run-plan` and any push through the default registry address start implicitly. The
`kubectl` and `lan:` modes were verified on a spare port, with the registry reached
through each using verified TLS. `lan:` is a small Python relay (`harness relay`), not
socat, so it needs nothing beyond the harness's own Python. `tsh-app:` waits on the Phase 0
onboarding. A new mode, `none` (the default), only checks that something already listens
on the port, as lima's own port forward does.

**TLS-routed clients need a second SNI route.** `tsh ls` with an identity reaches the
proxy with SNI `<hex(cluster name)>.teleport.cluster.local` (`api/utils/cluster.go`
EncodeClusterName), not the FQDN. The route now matches it too. So did the DOCKER ingress,
where native `tsh` through nginx had never worked (verified: with only the FQDN route,
`handshake failed: EOF`). The longer key needs `map_hash_bucket_size 256` in
`ingress/config/nginx.conf`, or nginx refuses to start. That is worse than it sounds: the
reload fails, `ingress_reload` falls back to a restart, and the shared ingress then
crash-loops for every docker cluster.

`cluster forward` (new) makes sure `127.0.0.1:$INGRESS_PORT` is listening, based on `FORWARD`:
- `tsh-app:harness-ingress` → `tsh proxy app harness-ingress --port $INGRESS_PORT`
  (background, pidfile under `state/`). Works from Beams and from the laptop anywhere.
  TCP app access tunnels raw bytes, so the test proxy's TLS/SNI/ALPN pass through
  untouched.
- `lan:<host>` → `socat` to `<host>:9443` (laptop at home, avoids the outer-Teleport hop).
- `kubectl` → `kubectl port-forward -n harness-system svc/traefik 9443`. Fallback only:
  long-lived port-forwards drop idle and streaming connections.

`run-plan`/`up` call it implicitly when `BACKEND=k8s`. The verifier itself does not need
the forwarder: checks run in-cluster through `kubectl exec`. Only the human-facing
commands and external agents use it.

User access then works exactly as it does locally: `tsh login --proxy <id>.lab.<domain>:9443`,
the web UI in a browser, and an agent started on the laptop or in Beams can join the test
cluster (reverse tunnels share the web port).

**Not covered:** scoped-app derived addresses (one label deeper than the wildcard). This
limitation already exists today.

## 6. State and cross-environment visibility

**Status: built (Phase 5).** As built, compared with the sketch below:
- `harness-meta` holds one gzipped tar (`harness/kube_state.py`) of meta.env, setup.json,
  the render and `kube/plan.json`. It does not hold results, logs, or the admin identity,
  which is a credential, so another client re-runs `admin`. meta.env's
  KUBECONFIG/KUBE_CONTEXT are stripped on pack and replaced by the ATTACHING client's own
  on unpack. It is written even when `up` fails part-way, so a half-up cluster can still
  be attached and torn down.
- `cluster ls` lists clusters other clients made as "not attached", with their expiry.
  When a cluster has been torn down elsewhere, the stale local entry says "gone".
- The admin tbot is a pod in the namespace (`<id>-admin-tbot`, the cluster's own image,
  its FQDN pinned to the auth pod). `tctl`/`tsh` exec into it, and `admin` also copies the
  identity to `state/<id>/identity` for a native tsh through the forwarder.
- `extend <id> [hours]` relabels the namespace (default 24h from now; `CLUSTER_TTL_HOURS`
  sets the initial TTL).

`state/<id>/` stays the client-local working dir (render output, results, bundles). In
addition, `kube-render` writes `meta.env`, `setup.json` and `rendered/` into a ConfigMap
`harness-meta` in the namespace. That makes a cluster created from Beams manageable from
the laptop:
- `cluster ls` (k8s) lists namespaces by label, so it shows clusters regardless of which
  client created them.
- `cluster attach <id>` hydrates a local `state/<id>/` from `harness-meta`, after which
  `report`, `tctl`, `verify`, `teardown` work as usual.
- `cluster tctl|tsh <id>` → `kubectl exec` into the `harness-admin` tbot pod (same image,
  so version-matched; no local linux binary needed on a mac). `cluster web` mints the
  invite the same way.
- `cluster extend <id> [dur]` bumps `expires-at`. Every `up` stamps `CLUSTER_TTL`.

## 7. Module compatibility (from the current tree)

| Module / component | Needs | Phase |
|---|---|---|
| `tbot`, `bound_keypair` | nothing beyond the core translator | 4 |
| `generic_oidc`, `kubernetes`, `oidc_caching`, `oidc_response_limit`, `scoped_usage_mode`, `scoped_app_access`, `bound_keypair_status` | `oidc-server` side image, aliases, `module_dir` ConfigMaps, `service_completed_successfully` waves | 4 |
| `bound_keypair_apply_on_startup` | `restart_auth` + seeded-writable apply-on-startup | 6 |
| `workload_identity_sds_override` | checks.py (uses the Cluster seam) — verify it never shells docker | 6 |
| `workload_identity_slow_start` | netns-share → sidecar, `NET_ADMIN` (its checks.py reads the rendered `docker-compose.yml`, which stays the render output on both backends, so that's fine) | 6 |
| `terraform_*` (+ `terraform-runner`, privileged) | artifact image for `tf-plugins`, privileged PSA | 6 |
| `terraform_native_join_lb` | the above + network isolation (NetworkPolicy) for the tarpit/LB topology | 6 |
| `docs_*` (+ `agent-runner`) | artifact image for `{{ repo }}/docs`, pull-back of `agent/out`, agent_mcp exec prefix | 6 |
| `operator_generic_oidc` (+ `k8s-runner`) | `requires_backend: docker` until Phase 7 | 7 |
| `tpm` | `requires_backend: docker` until Phase 8; then a KubeVirt VM with a vTPM | 8 |

## 8. Phases and exit criteria

0. **Host infra + spike.** `infra/k8s/` manifests + a setup section in this doc. *Exit:*
   from Beams, via the forwarder, `crane push` an image and a pod pulls it by its
   canonical name; `openssl s_client -servername x.lab.<domain>` reaches a dummy SNI route
   with the LE cert; measure a `kubectl exec` round trip through `tsh kube`.
   **Partly done (2026-10-15):** the infra exists, and push/pull, SNI passthrough with
   verified TLS, buildkitd and the janitor all work from the MAC against lima and a
   CoreOS VM. What remains is the "from Beams" half: onboard a host to the outer
   Teleport, then repeat these checks through `tsh kube` + the TCP app and take the
   latency number. None of that blocks phases 1–3.
1. **Backend seam, docker-only.** Move docker calls behind `lib/backend/docker.sh` and the
   `Cluster` factory with no behavior change. *Exit:* pytest green, `bots` plan green live.
   **Done (2026-10-16):** 253 tests pass; `bots` is 10/10 live on the
   v18.11.0-rc.2 package; `ls`/`logs`/`admin`/`tctl`/`tsh`/`web`/`doctor`/`teardown` checked
   by hand, and the agent MCP tool exec'd into a live container through a
   `make_cluster`-built prefix. Two intended behaviour changes: `teardown` now also removes
   the admin tbot + its volume (`compose down` never knew about them, and the tbot held the
   project network, which leaked too), and `cluster tctl/tsh` pass `--platform linux/amd64`.
2. **Translator.** `harness/kube.py` + `kube-render` + golden tests over every module.
   *Exit:* every module translates or is refused with its named reason.
   **Done (2026-10-16):** 17 modules translate with stable golden snapshots; `tpm` and
   `operator_generic_oidc` are refused by `requires_backend: docker`, with the unit named.
   The test suite is 296. Not yet exercised against a cluster — that is Phase 4.
3. **Image publishing.** crane base/teleport/artifact images, buildkitd side images, a
   registry-backed cache. *Exit:* the same `--package` key published from the laptop is a
   cache hit from Beams.
   **Done (2026-10-16), except the "from Beams" half**, which waits on the Phase 0
   onboarding. The second client was a fresh copy of the harness on the mac with an empty
   `.cache`: `cluster build --package` was a cache hit, and a forced republish uploaded
   nothing (same manifest digest). See §4 for what was built and the buildkitd/qemu gotcha.
   The other cluster ops on `BACKEND=k8s` refuse with the phase they land in.
4. **End to end.** `KubeCluster`, waves, teardown, log collection into the bundle.
   *Exit:* `bots` and `oidc-caching` plans run from Beams with the **same verdicts** as
   the docker backend on the same source, and `share` works on the bundle.
   **Done (2026-10-16) from the mac against the lima stand-in; "from Beams" waits on
   Phase 0.** On v18.11.0-rc.2: `bots` 10/10, and `oidc-caching` (all four modules, 26
   pods in 8 waves) gives the same verdict as docker on every one of its 52 checks; 90s
   end to end against docker's 53s. The bundle stages for `share` (checked with
   `harness gist-stage`; no gist was published) and gains `rendered/kube/`. What was built:
   - `harness/kube_apply.py` (`harness kube-up`): refuses an existing namespace (state
     dirs are per-client, so another client may own the id), applies the namespace, the
     Secrets (license read from the host, wildcard cert copied from `harness-system`; the
     values travel on stdin and are never written to disk), then the other objects, then
     pods wave by wave, each waiting on its `needs` (Phase 6 replaced the alias pinning
     with the namespace resolver, §3). Server-side apply, because a ConfigMap can exceed client-side
     apply's 256 KiB annotation limit. A pod stuck on an unrecoverable reason
     (`InvalidImageName`, …) or `Failed` stops the apply with that pod named.
   - `be_k8s_up` also checks that every harness image the pods name is in the registry
     before applying. That check found a latent bug that affects docker too:
     `oidc_caching` and `oidc_response_limit` pinned `teleport-harness-oidc:v2`, which
     nothing builds (the component builds `v4`). On docker they had worked only off a stale
     local image. Fixed, and `tests/test_render.py` now enforces that the pins match.
   - `KubeCluster` (harness/cluster.py) execs into the service's pod and container from
     `plan.json`. It reads `--previous` logs first (docker's logs span restarts), and reads
     files under a pull-back mount inside the pod, so `agent_result` needs no copy step.
     DockerCluster's tctl/audit helpers now go through `exec_argv`, so both backends share
     them. The translator now refuses a `container_name` other than `<id>-<service>`,
     because checks address services by that suffix.
   - **Every kubectl call is pinned** to an explicit kubeconfig and context. The target
     must name `KUBE_CONTEXT` (`KUBECONFIG` is optional), a new cluster records both in
     `meta.env`, and a new backend op, `use_cluster`, restores them for any command on an
     existing cluster. Found the hard way: `cluster logs` loads no target, so `kubectl`
     reached for the user's default kubeconfig, a WORK cluster. It failed only on a stale
     login, and `teardown`, which loads the default docker target, would have aimed
     `kubectl delete namespace` the same way. A k8s cluster whose `meta.env` records no
     context is now refused outright.
   - Not yet: `restart_auth` (Phase 6, `bound_keypair_apply_on_startup`) and admin
     access (Phase 5) refuse. Per-cluster artifact TAGS are left in the registry by
     teardown (content blobs are shared, so the cost is small; registry GC is open).
5. **Access + visibility.** `forward`, `ls`, `attach`, `tctl/tsh/admin/web`, TTL janitor,
   `extend`. *Exit:* a cluster created from Beams is listed, logged into (tsh + browser) and
   torn down from the laptop.
   **Done (2026-10-16) between two clients on the mac** (this checkout, and a separate
   copy with its own empty `state/`); Beams waits on Phase 0. Client B listed client A's
   cluster as "not attached", attached it, and ran `admin`, `tctl bots ls`, in-cluster
   `tsh status`, and a native mac `tsh ls` through the ingress. It also minted a `web`
   invite, whose page loads (200), ran `extend` and `logs`, then `teardown`. Client A then
   showed the cluster as gone. See §5 and §6 for what was built. Browser login itself
   (password + MFA on the invite page) was not driven.
6. **Breadth.** Rows marked 6 above, including NetworkPolicy for isolated networks.
   **In progress (2026-10-16):** `restart_auth`, the per-namespace resolver, compose's
   unhealthy budget, and the agent result read through the seam are built. Ten modules
   have per-check verdict parity with docker. NetworkPolicy turned out to be unneeded (§3).
   The table and what remains: "Current state and handoff" at the top.
7. **Operator modules (optional).** A privileged k3s-in-pod (the existing k3s entrypoint,
   minus most rootless workarounds) or vcluster per run, so CRDs don't collide across
   concurrent runs or branches.
8. **TPM via KubeVirt (spike first, on the real host).** The `tpm` module's client is a
   VM because a TPM is handed to a guest kernel by its hypervisor. KubeVirt makes that VM
   a namespaced resource — a `VirtualMachineInstance` running in a `virt-launcher` pod with
   `spec.domain.devices.tpm: {}` (swtpm, via libvirt) — so the module can lose
   `requires_backend: docker`, and teardown is the namespace delete like everything else.
   On the amd64 host the guest is amd64 too, so it reuses the harness's own `tbot`, and the
   lima/qemu/swtpm/gnutls host requirements disappear.

   **The question the spike answers: who manufactures the EK certificate?** Today's module
   exists because lima starts swtpm against empty state and never runs `swtpm_setup`, so it
   mints its own CAs and pre-seeds manufactured state. libvirt, as far as we know, runs
   `swtpm_setup --create-ek-cert` itself, signing with `swtpm_localca` inside the
   `virt-launcher` pod — and that CA is a root plus a SEPARATE issuer, which is exactly the
   module's root-not-issuer trap. So, in order of preference:
   1. **Discover the CA:** let libvirt manufacture the EKCert, read the issuer and root
      out of `virt-launcher` (`kubectl exec`), and build `ekcert_allowed_cas` from them;
      "unrelated CA denied" needs only some other CA. The claims and checks barely change —
      the device facts already come from `tbot tpm identify`.
   2. **Pre-seed state:** `tpm: {persistent: true}` (KubeVirt ≥ 1.0) keeps state on a
      backend PVC; write the manufactured state into it before first boot. Works, but it
      couples the module to KubeVirt's internal PVC layout, which an upgrade can change
      silently.

   Design notes for the build: drive the guest from an in-namespace runner pod that SSHes
   to the VMI's pod IP, not with `virtctl ssh` (that uses KubeVirt API subresources that
   Teleport's kube proxy may not pass through); `checks.py act()` then `kubectl exec`s the
   runner, which works from Beams. cloud-init writes the auth FQDN into the guest's
   `/etc/hosts` (pod `hostAliases` don't reach a guest). KubeVirt goes in `infra/k8s/host/`
   as a vendored operator + CR (containerDisk only, no CDI); expect SELinux friction on
   CoreOS, as with system-upgrade-controller. Where it can run: the real host (KVM); the
   lima stand-in only after `nestedVirtualization: true` in `infra/lima/k3s.yaml` (vz
   nested virt needs M3+ and macOS 15+ — the dev mac qualifies); a libvirt guest only with
   nested virt enabled.
   *Spike exit:* a VMI with `tpm: {}` boots; `tbot tpm identify` in the guest reports an
   EKCert; its issuer is found in `virt-launcher` and verifies the guest's certificate.
   *Phase exit:* the `tpm` module's claims hold on the k8s backend with the same verdicts
   as its last docker run.

## Risks to settle in the Phase 0 spike

- **exec latency through Teleport.** Verify retries + many execs. Mitigations: cache
  `audit_events`/`get_nodes` per verify pass; if still slow, batch checks into one
  in-pod helper call. Target: `tbot` verify ≤ 2× the docker time.
- **TCP app streaming.** Confirm long-lived reverse tunnels and gRPC streams through
  `tsh proxy app` stay up (idle timeouts on the outer cluster).
- **Push throughput.** A first-time teleport layer is ~150–250 MB compressed through
  the outer proxy, once per source key.
- **`tsh` in Beams.** Confirm `tsh proxy app` can run (and bind localhost) with the
  identity Beams provides, not only `tsh kube`.
- **ImageVolume availability** on the chosen k3s version (the init-copy fallback works
  everywhere, so this is an optimization).
- **Compose surface drift.** The translator's hard error on unknown keys is the guard;
  the authoring guide gets a line that new compose keys need a translator rule.
