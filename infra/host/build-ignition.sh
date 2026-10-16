#!/usr/bin/env bash
# Render infra/host/fcos.bu to an Ignition config: infra/host/out/<hostname>.ign.
#
#   infra/host/build-ignition.sh [--ssh-key FILE] [--hostname NAME] [--k3s-version VER]
#     [--teleport-proxy ADDR] [--teleport-token-name NAME]
#
# Then install it: bare metal from the CoreOS live ISO with
#   sudo coreos-installer install /dev/<disk> --ignition-file <hostname>.ign
# or boot a test VM with infra/host/test-libvirt.sh / test-qemu-macos.sh.
#
# --teleport-proxy enables a host-level Teleport agent (ssh_service + app_service for
# Cockpit, TPM join — see teleport.yaml and teleport-bootstrap.sh) alongside k3s. Omit it
# to leave the host k3s-only, as before.
#
# Uses a local `butane` if installed, else the official container image.
set -euo pipefail
INFRA="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$INFRA/host/out"

ssh_key="$HOME/.ssh/id_ed25519.pub"
hostname="harness-k3s"
# The version FIRST installed. Afterwards system-upgrade-controller follows the minor
# channel in k8s/host/harness-92-upgrade-plans.yaml — keep the two on the same minor.
k3s_version="v1.36.5+k3s1"
teleport_proxy=""
teleport_token_name=""

while [ $# -gt 0 ]; do
  case "$1" in
    --ssh-key) ssh_key="$2"; shift 2 ;;
    --hostname) hostname="$2"; shift 2 ;;
    --k3s-version) k3s_version="$2"; shift 2 ;;
    --teleport-proxy) teleport_proxy="$2"; shift 2 ;;
    --teleport-token-name) teleport_token_name="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -s "$ssh_key" ] || { echo "no ssh public key at $ssh_key (pass --ssh-key)" >&2; exit 1; }
[ -n "$teleport_token_name" ] || teleport_token_name="$hostname-host"

mkdir -p "$OUT"
cp "$ssh_key" "$OUT/ssh.pub"
printf '%s\n' "$hostname" > "$OUT/hostname"
printf 'K3S_VERSION=%s\n' "$k3s_version" > "$OUT/k3s.env"

# Always write both files — butane's local: references need them to exist regardless
# of whether --teleport-proxy was passed. teleport-bootstrap.sh checks PROXY_ADDR itself
# and no-ops immediately when it's empty, so an unconfigured host stays k3s-only.
printf 'PROXY_ADDR=%s\nTOKEN_NAME=%s\n' "$teleport_proxy" "$teleport_token_name" > "$OUT/teleport.env"
sed -e "s|__PROXY_ADDR__|$teleport_proxy|" -e "s|__TOKEN_NAME__|$teleport_token_name|" \
  "$INFRA/host/teleport.yaml" > "$OUT/teleport.yaml"

ign="$OUT/$hostname.ign"
if command -v butane >/dev/null 2>&1; then
  butane --strict --pretty --files-dir "$INFRA" "$INFRA/host/fcos.bu" > "$ign"
else
  docker run --rm -i -v "$INFRA:/infra:ro" quay.io/coreos/butane:release \
    --strict --pretty --files-dir /infra < "$INFRA/host/fcos.bu" > "$ign"
fi
echo "$ign  (k3s $k3s_version, host $hostname, key $(awk '{print $NF}' "$ssh_key"))"
