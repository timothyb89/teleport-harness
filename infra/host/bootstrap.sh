#!/bin/bash
# First-boot k3s install on Fedora CoreOS, run by harness-k3s-install.service until
# /usr/local/bin/k3s exists. Two passes, one reboot:
#   1. layer k3s-selinux (k3s's SELinux policy) and reboot into the new deployment;
#   2. install k3s, which starts it, which applies every manifest Ignition already wrote
#      to /var/lib/rancher/k3s/server/manifests.
# After that, Zincati owns OS updates and system-upgrade-controller owns k3s updates.
set -euo pipefail
# shellcheck source=/dev/null
. /etc/harness/k3s.env   # K3S_VERSION

if ! rpm -q k3s-selinux >/dev/null 2>&1; then
  echo "harness: layering k3s-selinux, then rebooting into it"
  rpm-ostree install --idempotent --allow-inactive k3s-selinux
  systemctl reboot
  exit 0
fi

echo "harness: installing k3s $K3S_VERSION"
# The installer would try to layer k3s-selinux itself (another reboot); pass 1 did it.
# Skipping that ALSO skips its relabel, so the binary keeps the tmp_t label it was
# downloaded with and systemd can't exec it ("Permission denied"). Ignition likewise wrote
# /var/lib/rancher and /etc/rancher before the k3s policy module existed. So: install
# without starting, relabel against the now-loaded policy, then start.
curl -sfL https://get.k3s.io |
  INSTALL_K3S_VERSION="$K3S_VERSION" INSTALL_K3S_SKIP_SELINUX_RPM=true INSTALL_K3S_SKIP_START=true sh -
restorecon -R -v /usr/local/bin/k3s /var/lib/rancher /etc/rancher
systemctl start k3s
