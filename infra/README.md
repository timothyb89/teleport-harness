# infra/ — the remote Kubernetes host

The long-lived k3s box the k8s backend deploys test clusters into
(design: `docs/remote-kubernetes.md`). One source of truth, two consumers: the real
**Fedora CoreOS** host and the **lima stand-in** on a mac.

```
k3s/config.yaml, registries.yaml   k3s server config + the registry.harness:5000 mirror
k8s/manifests/                     harness-system: registry, Traefik (:9443), buildkitd,
                                   TTL janitor — auto-applied by k3s on BOTH hosts
k8s/host/                          real host only: system-upgrade-controller + k3s plan
k8s/fetch-vendored.sh              refresh pinned third-party manifests (SUC, Traefik CRDs)
k8s/import-cert.sh                 copy the docker ingress's wildcard cert into a Secret
host/fcos.bu, bootstrap.sh         the CoreOS host (Butane → Ignition)
host/build-ignition.sh             render host/out/<hostname>.ign
host/test-qemu-macos.sh            boot it in a throwaway aarch64 VM on this mac
host/test-libvirt.sh               boot it on an x86_64 libvirt host
lima/k3s.yaml, sync.sh             the lima stand-in (vz + Rosetta) and its sync
```

## Real host (Fedora CoreOS, amd64)

```sh
infra/host/build-ignition.sh [--ssh-key ~/.ssh/id_ed25519.pub] [--hostname harness-k3s]
# boot the CoreOS live ISO on the box, then:
sudo coreos-installer install /dev/nvme0n1 --ignition-file harness-k3s.ign
```

First boot layers `k3s-selinux` and reboots once. The second boot installs k3s, which
applies everything in `k8s/manifests` and `k8s/host`. After that, nothing needs a person:

| What | How | When (UTC) |
|---|---|---|
| OS | Zincati, atomic; `rpm-ostree rollback` to undo | Sun 09:00–11:00 |
| k3s | system-upgrade-controller, patch releases of the `v1.36` channel | Sat 09:00–11:00 |
| test clusters | janitor deletes `th-*` namespaces past `harness.teleport/expires-at` | every 15 min |

A minor k3s bump is deliberate: change the channel in `k8s/host/harness-92-upgrade-plans.yaml`
and `--k3s-version` in `build-ignition.sh`. A running host picks up manifest edits when
the file lands in `/var/lib/rancher/k3s/server/manifests/` (scp it there).

Then `KUBECONFIG` from `/etc/rancher/k3s/k3s.yaml`, and `infra/k8s/import-cert.sh` until
the in-cluster ACME job exists.

## lima stand-in (Apple Silicon)

```sh
limactl start --name harness-k3s infra/lima/k3s.yaml
infra/lima/sync.sh                      # re-run after editing k3s/ or k8s/manifests
export KUBECONFIG=~/.lima/harness-k3s/copied-from-guest/kubeconfig.yaml
infra/k8s/import-cert.sh
```

To point the harness at it, add a target (see the k8s block in
`targets/default.env.example`) with `BACKEND=k8s`, this `KUBECONFIG` and `INGRESS_PORT=9443`,
then run `bin/cluster --target <it> doctor`.

lima forwards NodePort/LoadBalancer services to the mac, so `https://<x>.lab.<domain>:9443`
and the registry (`registry.lab.<domain>:9443`, or `localhost:30500`) work with no
forwarder. amd64 harness images run under Rosetta **as long as each is a single-platform
manifest** (see the arch table in `docs/remote-kubernetes.md` §4).

## Verified (2026-10-15)

On lima (k3s v1.36.5, arm64 + Rosetta) and on a from-scratch CoreOS 44 VM via
`test-qemu-macos.sh` (SELinux enforcing, zero AVC denials):
- harness-system comes up unattended from the manifests dir, with the bundled Traefik
  disabled;
- SNI passthrough: a pod in a `th-*` namespace serving the wildcard cert is reached from
  the mac at `<name>.lab.<domain>:9443` with verified TLS;
- registry: crane push through the ingress with publicly trusted TLS, then pull by the
  canonical `registry.harness:5000/…` name;
- buildkitd via `buildctl --addr kube-pod://…`: an amd64 image built, pushed, and run;
  the output is a single manifest by default;
- janitor: deletes an expired `th-*` namespace and leaves an unexpired one alone, along
  with an expired namespace without the `th-` prefix;
- CoreOS only: k3s v1.36.4 → v1.36.5 by system-upgrade-controller with the window
  removed (22 s, node cordoned then uncordoned); with the window present the plan resolves
  and waits. `sch_netem` and `ifb` are in the base kernel.

NOT yet verified: `test-libvirt.sh` (written from the CoreOS libvirt guide; no x86_64
host here), and anything on real hardware.

## Gotchas found bringing this up (do not relearn)

- **Traefik's CRDs can't be a Helm release.** Rendered, they exceed the 1 MiB release
  Secret (`data: Too long`), so they're vendored (`harness-18-…`), and the controller chart
  installs with `serverSide: "false"` so helm SKIPS its own `crds/` instead of fighting
  k3s's `deploy` field manager over them.
- **Disabling the bundled Traefik deletes its CRDs**, along with every
  IngressRouteTCP. k3s re-applies a manifest when its checksum changes, and on every
  k3s start, but not on drift; a k3s restart (or the weekly upgrade) heals it.
- **A TLS-terminating TCP route offers ALPN `h2` by default**, so Go clients speak HTTP/2
  at an HTTP/1.1 backend (`http2: frame too large`); hence the registry's `http/1.1`
  TLSOption. Not an issue for passthrough routes, where Teleport does its own ALPN.
- **CoreOS + SELinux:** with `INSTALL_K3S_SKIP_SELINUX_RPM` the k3s installer also skips
  its relabel, leaving the binary `tmp_t` (systemd: `Permission denied`) — bootstrap.sh
  `restorecon`s. And system-upgrade-controller can't read the host's `cert_t` CA files
  as `container_t`, so it's patched to `spc_t` at vendor time (fetch-vendored.sh).
- `openssl s_server -cert fullchain.pem` sends only the LEAF; use `-cert_chain`, or a
  verifying client fails with `unable to get local issuer certificate`.
