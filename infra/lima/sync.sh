#!/usr/bin/env bash
# Bring the lima stand-in (infra/lima/k3s.yaml) in line with the real host's config: the
# same k3s config, registry mirror and harness-system manifests the CoreOS host gets
# from infra/host/fcos.bu. Idempotent; re-run after editing anything under infra/k3s or
# infra/k8s/manifests.
#
#   limactl start --name harness-k3s infra/lima/k3s.yaml
#   infra/lima/sync.sh
#   infra/k8s/import-cert.sh        # the wildcard cert, until the ACME job exists
set -euo pipefail
VM="${VM:-harness-k3s}"
INFRA="$(cd "$(dirname "$0")/.." && pwd)"

stage="/tmp/harness-infra"
limactl shell "$VM" rm -rf "$stage"
limactl shell "$VM" mkdir -p "$stage"
limactl copy "$INFRA/k3s/config.yaml" "$INFRA/k3s/registries.yaml" "$VM:$stage/"
limactl copy -r "$INFRA/k8s/manifests" "$VM:$stage/"

# k3s reads config.yaml and registries.yaml only at startup; the manifests dir is watched
# and needs no restart. Restart only when a startup file actually changed.
limactl shell "$VM" sudo sh -euc "
  changed=0
  for f in config.yaml registries.yaml; do
    if ! cmp -s $stage/\$f /etc/rancher/k3s/\$f; then
      install -D -m 0644 $stage/\$f /etc/rancher/k3s/\$f; changed=1
    fi
  done
  install -m 0644 $stage/manifests/*.yaml /var/lib/rancher/k3s/server/manifests/
  if [ \$changed = 1 ]; then echo 'k3s config changed: restarting k3s'; systemctl restart k3s; fi
"
echo "synced infra to $VM"
