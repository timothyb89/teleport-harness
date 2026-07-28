# In-cluster Kubernetes testing (`components/k8s-runner`)

How the harness runs a real Kubernetes cluster so it can test the Teleport **Kubernetes
operator**, what that requires of your Docker host, and what to do when it breaks.

If you only want to run the tests, you need nothing beyond a working Docker daemon and
the harness's normal setup:

```bash
./bin/cluster run-plan operator --repo ~/projects/teleport \
    --features generic_oidc --version v19
```

No `kind`, no `k3d`, no `minikube`, no `kubectl` or `helm` on your host.

---

## The shape

```
             docker network: <cluster>_internal
  ┌───────────────┐   gRPC/TLS via the proxy    ┌────────────────────────────┐
  │  <id>-auth    │◀────────────────────────────│  <id>-k3s   (a container)  │
  │  teleport     │                             │   └─ pod: teleport-operator│
  │  amd64        │                             │        (native arch)       │
  └───────────────┘                             └────────────────────────────┘
         ▲                                                    ▲
         │ generic_oidc join with the operator-created token  │ kubectl apply
  ┌──────┴────────┐                             ┌─────────────┴──────────────┐
  │ <id>-op-agent-*│                            │ <id>-k8s-operator, <id>-op-crs│
  └───────────────┘                             └────────────────────────────┘
```

Teleport itself runs **outside** Kubernetes, as an ordinary harness cluster. The operator
runs **inside** k3s and reaches Teleport over the shared docker network. That split is
deliberate: putting Teleport in Kubernetes too would mean testing the helm chart, and what
we want to test is the operator's resource sync.

### Why k3s-as-a-container, and not kind / k3d / minikube

The requirement that decided it is **lifecycle**: `cluster teardown <id>` must remove the
Kubernetes cluster too. When the node is just another compose service, teardown is already
correct — `docker compose down -v` takes the node, its data volume and its network with
it, and nothing is registered anywhere on the host that could leak. `kind` and `k3d` both
keep a host-level cluster registry that would need a matching teardown hook, and VM-based
options (minikube) add a second virtualisation layer under an already-virtualised Docker.

The trade is that we own the runtime tuning that `k3d` would otherwise own for us — hence
the rest of this document.

### Concurrency

Everything is namespaced by cluster id, so concurrent runs do not collide:

| Resource | Name |
|---|---|
| node container | `<id>-k3s` |
| runners | `<id>-k8s-operator`, `<id>-op-crs` |
| volumes | `teleport-harness-<id>_k8s-data`, `…_k8s-kubeconfig` |
| operator image | `teleport-harness-operator:<id>` (removed by `teardown`) |
| staged build | `state/<id>/k8s/` |

The node publishes **no host ports**. The real limit is machine resources: budget roughly
**1 GB of RAM per concurrent k3s**, and note that the `native` snapshotter (see below)
does not share image layers between pods, so disk use per cluster is higher than usual.
Both are reclaimed on teardown.

---

## Host requirements

Verified on the harness's normal environment: **rootless** Docker inside a lima VM
(aarch64, cgroup v2, kernel 6.x).

| Requirement | Why | Rootful Docker |
|---|---|---|
| cgroup **v2** | the entrypoint's cgroup nesting fix is v2-specific | same |
| `privileged: true` allowed | kubelet + containerd inside a container | same |
| Kernel ≥ 4.18 | k3s baseline | same |
| Go toolchain on the host | cross-builds the operator (CGO-free, no cross compiler) | same |

**Rootless is the harder case, and it is what this is tuned for.** Every workaround below
is either required by rootless or harmless everywhere, so the same configuration is
expected to work on a rootful daemon — with the caveat that it has not been exercised
there. If you run this on rootful Docker and something misbehaves, the two settings most
likely to be worth revisiting are `--snapshotter=native` (rootful hosts can usually use
the faster `overlayfs`) and the `KubeletInUserNamespace` feature gate (unnecessary but
inert when not in a user namespace).

Things that are **not** required: a specific architecture (the k8s side builds and runs
native — see below), any host Kubernetes tooling, or any registry.

---

## The four things that make k3s work here

Each of these was found by watching k3s die. Do not remove one because it looks
unnecessary. They live in `harness/templates/scripts/k3s-entrypoint.sh` and the `k3s`
service in `components/k8s-runner/services.yml.j2`.

### 1. cgroup v2 nesting (the big one)

cgroup v2 forbids a cgroup from both holding processes and delegating controllers to
children. The container's root cgroup starts out holding the entrypoint shell, so the
moment kubelet creates `/kubepods` it dies with:

```
cannot enter cgroupv2 "/sys/fs/cgroup/kubepods" with domain controllers -- it is in an invalid state
```

The fix (the same one `kind` and `k3d` apply) is to move every process into a leaf cgroup
and then delegate the controllers downward, before k3s starts.

> **Trap:** `--kubelet-arg=cgroups-per-qos=false --kubelet-arg=enforce-node-allocatable=`
> makes the node go **Ready** and looks like a fix. It is not — every pod then fails to
> start with the identical error raised by *runc* creating its sandbox
> (`cannot enter cgroupv2 "/sys/fs/cgroup/k8s.io" …`). Fix the cgroup tree, don't disable
> QoS.

### 2. `--snapshotter=native`

`overlayfs` cannot stack on the container's own overlay filesystem, and the `rancher/k3s`
image ships no `mount.fuse3`, so `fuse-overlayfs` fails its startup probe too:

```
"fuse-overlayfs" snapshotter cannot be enabled … exec: "mount.fuse3": executable file not found
```

`native` works everywhere at the cost of copying instead of sharing image layers.

### 3. `/dev/kmsg` and `KubeletInUserNamespace`

Rootless Docker cannot give a container the real `/dev/kmsg`, and kubelet's oomWatcher
opens it unconditionally:

```
Failed to create an oomWatcher (running in UserNS …) err="open /dev/kmsg: operation not permitted"
```

Compose binds `/dev/null` over it to satisfy the open, and
`--kubelet-arg=feature-gates=KubeletInUserNamespace=true` makes kubelet tolerate the rest
of what it cannot see in a user namespace.

### 4. `--tls-san=<id>-k3s`

k3s writes a kubeconfig pointing at `127.0.0.1`, which is meaningless to a sibling
container. The runners rewrite the server address to the container name, so the API
certificate has to be valid for that name.

---

## Networking: pods cannot resolve docker names

A pod reaches other containers on the docker network **by IP** perfectly well, but cannot
resolve them **by name** — CoreDNS forwards to an upstream resolver that has never heard
of `<id>-auth`. Using `hostNetwork: true` does not help, because kubelet still hands the
pod a generated `resolv.conf`.

So `k8s-operator-entrypoint.sh` resolves the Teleport cluster's IP where docker DNS *is*
available (in the runner container) and pins it into the pod with `hostAliases`. Both
names are pinned:

- `<fqdn>` — what the wildcard Let's Encrypt certificate matches, so the operator
  validates real TLS through the proxy with no `--insecure`;
- `<id>-auth` — what the rest of the harness uses.

## Getting the locally built operator in

There is no registry. `components/k8s-runner/prebuild.sh`:

1. cross-builds `integrations/operator` with `CGO_ENABLED=0` for the **docker daemon's
   native architecture** (the rest of the harness is amd64; the k8s side is not, because
   emulating a control plane is slow and a cross-arch image would not resolve on the node
   — and the operator only speaks gRPC, so its architecture is irrelevant to the test);
2. packages it into `teleport-harness-operator:<id>` on an alpine base;
3. `docker save`s it into `state/<id>/k8s/images/`, which is bind-mounted at
   `/var/lib/rancher/k3s/agent/images` — k3s imports every tarball it finds there at agent
   startup (its documented air-gap path). The Deployment can then use
   `imagePullPolicy: Never`.

The build is **not** SHA-cached: like `terraform-runner`, the point is the
edit → rebuild → retest loop on an uncommitted fix, so it always rebuilds (a no-op
rebuild takes seconds).

> The upstream `Dockerfile` sets `CGO_ENABLED=1`, claiming `lib/system` needs it. Nothing
> the operator exercises does, and a static binary removes the need for a cross toolchain
> entirely.

### CRDs come from the checked-in generated files

`prebuild.sh` copies `integrations/operator/config/crd/bases/*.yaml` — the manifests that
actually ship. **If you change `crdgen`, regenerate before re-running**, or you will test
the old schema:

```bash
make -C integrations/operator crd-manifests    # needs protoc
```

`prebuild.sh` prints a reminder on every render.

---

## How the operator authenticates (and its one sharp edge)

The operator's embedded tbot joins as a privileged bot with the **`token`** join method
and a preset secret, matching how the harness's other admin bots work.

> **The `token` join method is single-use.** Auth deletes the token once it is redeemed
> (`lib/auth/join.go`), and `embeddedtbot` keeps its state in memory
> (`destination.NewMemory()`) — so **a restarted operator pod can never re-join.**

Two consequences are baked into the manifest, and you should keep them:

- **No liveness probe.** Self-healing would turn one transient stall into a permanently
  dead operator and a baffling test failure. Readiness only.
- The Deployment is applied only after `auth` is healthy (`depends_on`), so the single
  join it gets is the one that counts.

If a future module needs an operator that survives restarts, the fix is a non-consumable
join method — `kubernetes` with `static_jwks`, which would also exercise the operator's
real production join path. That needs the k3s service-account JWKS in a Teleport token at
bootstrap time, and anonymous access to the API server's discovery endpoints; it was not
worth the ordering complexity for something that is not under test.

RBAC is `cluster-admin`, deliberately: the real chart ships a least-privilege role, but
reproducing it here would only test our copy of it, and it would become a confounding
variable whenever a reconcile fails.

---

## Writing another operator module

The component gives you the cluster, the CRDs and a running operator. A module adds its
own resource runner, exactly as the `terraform_*` modules add their own terraform runner.
See `modules/operator_generic_oidc/` — the whole module is two CR templates, two agent
configs, a ~40-line fragment and a `checks:` block.

```yaml
# render.yaml
components: [k8s-runner]
k3s_version: v1.31.5-k3s1     # re-declared: fragments can't see a component's vars
```

```yaml
# services.yml.j2
  my-crs:
    image: rancher/k3s:{{ k3s_version }}
    entrypoint: ["/bin/sh", "/scripts/k8s-apply-entrypoint.sh"]
    environment: {K3S_HOST: "{{ cluster_id }}-k3s"}
    volumes:
      - {{ shared_scripts }}/k8s-common.sh:/scripts/k8s-common.sh:ro
      - {{ shared_scripts }}/k8s-apply-entrypoint.sh:/scripts/k8s-apply-entrypoint.sh:ro
      - {{ out }}/config/my-cr.yaml:/work/10-my-cr.yaml:ro    # applied in filename order
      - k8s-kubeconfig:/kubeconfig:ro
    networks: [internal]
    depends_on: {k8s-operator: {condition: service_healthy}}
    healthcheck: {test: ["CMD-SHELL", "test -f /tmp/apply-done"], …}
```

The apply runner never aborts on a rejected manifest — a rejection is frequently the
finding, not an accident — and logs a `RESULT <file>: accepted|REJECTED` line per file
that `log_contains` can assert on.

### Check verbs

`<kind/name>` resolves in the `teleport` namespace (`harness/cluster.py:KUBE_NAMESPACE`).

| Verb | Meaning |
|---|---|
| `k8s_resource_present <kind/name>` | the object exists (a CR refused at admission does not) |
| `k8s_resource_field <kind/name> <dotted.path> [expected]` | a field the API server actually stored |
| `k8s_condition <kind/name> <type> [status]` | a `status.conditions[]` entry; defaults to `True`, surfaces the condition's message on failure |

Pair them with the Teleport-side `resource_present` / `resource_field`. Together the two
sides localise a failure precisely: the CRD schema rejected it, *or* the operator never
reconciled it, *or* it never reached Teleport.

---

## Troubleshooting

```bash
./bin/cluster logs <id> k3s              # node: cgroup / snapshotter / kubelet failures
./bin/cluster logs <id> k8s-operator     # CRD install + rollout + the operator's own logs
./bin/cluster logs <id> op-crs           # per-manifest accepted/REJECTED verdicts
docker exec <id>-k3s kubectl get pods -A
docker exec <id>-k3s kubectl -n teleport logs deploy/teleport-operator
```

| Symptom | Cause |
|---|---|
| node never Ready, `cannot enter cgroupv2` | cgroup nesting fix didn't apply — cgroup v1 host, or `/sys/fs/cgroup` not writable |
| node Ready, every pod `ContainerCreating` | same cause, seen from runc; do **not** paper over it with `cgroups-per-qos=false` |
| `ErrImageNeverPull` | the tarball wasn't imported — check `state/<id>/k8s/images/` exists and `prebuild.sh` succeeded |
| operator `CrashLoopBackOff` after one good start | it consumed its single-use join token and cannot re-join; tear down and re-run |
| CR rejected with `unknown field …` | often a real finding — a generated CRD schema that can't express what the Teleport API supports |
