# Design: a remote Kubernetes backend

Status: **proposed** (not built). Companion to `docs/kubernetes.md`, which covers the
*in-cluster k3s for operator tests*; this document is about running **the whole harness
cluster** in a remote Kubernetes cluster instead of local docker compose.

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
- **Aliases are all dotted `*.lab.<domain>` names** (seven owners across the modules).
  The owner resolves its own via `127.0.0.1` (inline); every other pod gets the owner's pod
  IP, so it also waits for the owner to have STARTED — unless the owner (transitively)
  waits on it, in which case that pod goes without the alias. Waits can therefore reorder
  waves (generic_oidc: the IdP starts before auth) but never deadlock.
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
| network aliases (the auth FQDN, `oidc.<lab>`) | phase 1 applies all Services; their ClusterIPs then go into **`hostAliases`** on every pod. Pods would otherwise resolve `*.lab` publicly to 127.0.0.1, so this is the k8s equivalent of the docker alias, and TLS still matches the wildcard. |
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
5. **Access + visibility.** `forward`, `ls`, `attach`, `tctl/tsh/admin/web`, TTL janitor,
   `extend`. *Exit:* a cluster created from Beams is listed, logged into (tsh + browser) and
   torn down from the laptop.
6. **Breadth.** Rows marked 6 above, including NetworkPolicy for isolated networks.
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
