#!/bin/sh
# Shared entrypoint for the disposable in-cluster Kubernetes node (the k8s-runner
# component's `k3s` service). Mounted at /scripts/k3s-entrypoint.sh and run instead of
# the image's own entrypoint so we can fix up cgroups BEFORE k3s starts.
#
# Every flag and fixup here exists because of a specific failure under ROOTLESS docker
# (the harness's lima VM); each is harmless on a rootful daemon. See
# docs/kubernetes.md for the full derivation — do NOT drop one because it "looks
# unnecessary", they were each found by watching k3s die.
set -u

# --- cgroup v2 nesting fix (kind/k3d do the same thing) -----------------------
# cgroup v2 forbids a cgroup from BOTH holding processes and delegating controllers to
# children. Our container's root cgroup starts out holding this shell, so the moment
# kubelet creates /kubepods (or runc creates /k8s.io for a pod sandbox) it dies with
#   cannot enter cgroupv2 "..." with domain controllers -- it is in an invalid state
# Move every process into a leaf ("init"), then delegate the controllers downward.
# NOTE: disabling cgroups-per-qos instead is a TRAP — the node goes Ready but every pod
# still fails to start, because runc hits the identical error creating its sandbox.
if [ -w /sys/fs/cgroup/cgroup.controllers ] || [ -e /sys/fs/cgroup/cgroup.controllers ]; then
  mkdir -p /sys/fs/cgroup/init 2>/dev/null
  for pid in $(cat /sys/fs/cgroup/cgroup.procs 2>/dev/null); do
    # A pid can exit between the read and the write; that's fine, keep going.
    (echo "$pid" > /sys/fs/cgroup/init/cgroup.procs) 2>/dev/null || true
  done
  for ctl in $(cat /sys/fs/cgroup/cgroup.controllers 2>/dev/null); do
    (echo "+$ctl" > /sys/fs/cgroup/cgroup.subtree_control) 2>/dev/null || true
  done
fi

echo "[k3s] cgroup.subtree_control: $(cat /sys/fs/cgroup/cgroup.subtree_control 2>/dev/null)"
echo "[k3s] starting server (snapshotter=native, tls-san=${K3S_TLS_SAN:-none})"

# --- k3s server ---------------------------------------------------------------
# --snapshotter=native      : the image ships no mount.fuse3, and overlayfs-on-overlayfs
#                             is not permitted, so both other snapshotters fail.
# KubeletInUserNamespace    : rootless docker cannot open /dev/kmsg (we bind /dev/null
#                             over it in compose); this gate makes kubelet tolerate that.
# --disable=*               : nothing here needs an ingress, LB, metrics or helm CRDs.
#                             Less to wait for, less to go wrong.
# --tls-san                 : so the API cert is valid for the container NAME, which is
#                             how sibling containers on the docker network reach it.
exec /bin/k3s server \
  --snapshotter=native \
  --kubelet-arg=feature-gates=KubeletInUserNamespace=true \
  --disable=traefik \
  --disable=servicelb \
  --disable=metrics-server \
  --disable-network-policy \
  --disable-helm-controller \
  --tls-san="${K3S_TLS_SAN:-localhost}"
