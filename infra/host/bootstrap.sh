#!/bin/bash
# First-boot k3s install on Fedora CoreOS, run by harness-k3s-install.service until
# /usr/local/bin/k3s exists. Two passes, one reboot:
#   1. layer k3s-selinux (k3s's SELinux policy) PLUS the Cockpit/libvirt stack in one
#      rpm-ostree transaction, then reboot into the new deployment;
#   2. install k3s, which starts it, which applies every manifest Ignition already wrote
#      to /var/lib/rancher/k3s/server/manifests.
# After that, Zincati owns OS updates and system-upgrade-controller owns k3s updates.
# (A third leg, teleport-update, is staged separately by harness-teleport-bootstrap and
# needs no rpm-ostree layer at all — see teleport-bootstrap.sh.)
set -euo pipefail
# shellcheck source=/dev/null
. /etc/harness/k3s.env   # K3S_VERSION

if ! rpm -q k3s-selinux cockpit-machines libvirt-daemon-kvm >/dev/null 2>&1; then
  echo "harness: layering k3s-selinux + cockpit/libvirt, then rebooting into it"
  rpm-ostree install --idempotent --allow-inactive \
    k3s-selinux \
    cockpit cockpit-machines \
    libvirt-daemon-kvm qemu-kvm virt-install edk2-ovmf
  systemctl reboot
  exit 0
fi

# The libvirt group is created by libvirt-daemon-kvm's install, so it doesn't exist
# until after the reboot above — Ignition's own passwd.users.groups runs too early
# (in the initramfs, before any package is layered) and fails the whole boot with
# "useradd: cgroup 'libvirt' does not exist". Add core to it here instead, now that
# it's real.
usermod -aG libvirt core

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
