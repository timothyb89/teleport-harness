#!/bin/bash
# Stage the host-level Teleport agent, run by harness-teleport-install.service until
# /opt/teleport exists. No-op (service doesn't even start) if --teleport-proxy wasn't
# passed to build-ignition.sh, since /etc/harness/teleport.env then doesn't exist.
#
# teleport-update manages its own static binary + systemd unit entirely outside
# rpm-ostree (unlike k3s, which needs the k3s-selinux layer): no reboot per Teleport
# version bump, and /opt is /var/opt under ostree so it survives upgrades untouched.
# /etc/teleport_host.yaml (join_method: tpm) is already in place from Ignition, but the
# provisioning token it names can't exist yet — creating it on the proxy side needs the
# EKCert hash/serial read from THIS box's live TPM. So teleport_host.service is left to
# fail-and-retry until a human runs `sudo /opt/teleport/host/bin/teleport tpm identify`
# and creates the matching token on the proxy (see infra/README.md). That's expected
# steady state, not a bug: the StartLimitIntervalSec=0 drop-in below keeps it retrying
# forever instead of giving up after systemd's default burst limit.
set -euo pipefail
# shellcheck source=/dev/null
. /etc/harness/teleport.env   # PROXY_ADDR, TOKEN_NAME

if [ -z "$PROXY_ADDR" ]; then
  echo "harness: no --teleport-proxy was given at build time; host agent disabled"
  exit 0
fi

echo "harness: installing teleport-update against $PROXY_ADDR"
# Three things install.sh (lib/web/scripts/oneoff/oneoff.sh server-side) assumes that
# don't hold on an ostree root, found the hard way:
#   1. `mktemp -d -p ${HOME:-}/` for its own scratch dir — systemd units don't set $HOME
#      by default, so this collapses to `mktemp -d -p /`, CoreOS's read-only root.
#      /root is a symlink to /var/roothome, which IS writable — set HOME explicitly.
#      TMPDIR too, in case a future revision of the script uses that instead.
#   2. teleport-update's DEFAULT (unnamed) install hardcodes its systemd unit at
#      /lib/systemd/system/teleport.service ("for backwards-compat", straight from
#      upstream's own setup.go) — i.e. /usr/lib/systemd/system, part of the read-only
#      ostree /usr tree: "open /lib/systemd/system/.teleport.service...: read-only file
#      system". A NAMED install (TELEPORT_INSTALL_SUFFIX, upstream's escape hatch for
#      running multiple instances) uses /etc/systemd/system instead, which is always
#      writable — not a hack, just a different, equally-supported upstream code path.
#      This also renames the config to /etc/teleport_host.yaml, the data dir to
#      /var/lib/teleport_host, the service to teleport_host.service, and moves the
#      linked binaries from /usr/local/bin to /opt/teleport/host/bin (teleport-update
#      itself stays at /usr/local/bin/teleport-update either way).
export HOME=/root TMPDIR=/var/tmp TELEPORT_INSTALL_SUFFIX=host
curl -sfL "https://$PROXY_ADDR/scripts/install.sh" | bash

mkdir -p /etc/systemd/system/teleport_host.service.d
cat > /etc/systemd/system/teleport_host.service.d/10-harness.conf <<'EOF'
[Service]
# Keep retrying indefinitely: the join token this box needs doesn't exist until a
# human creates it on the proxy after reading this box's TPM identity.
StartLimitIntervalSec=0
EOF

restorecon -R -v /opt/teleport /usr/local/bin/teleport-update 2>/dev/null || true

systemctl daemon-reload
systemctl enable --now teleport_host.service
