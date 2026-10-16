#!/usr/bin/env bash
# Boot the real host config (an Ignition from build-ignition.sh) in a throwaway Fedora
# CoreOS VM on an Apple Silicon mac: qemu + Hypervisor.framework, aarch64 FCOS.
#
#   infra/host/test-qemu-macos.sh up <ignition> [workdir]   # boots, daemonized
#   infra/host/test-qemu-macos.sh down [workdir]
#
# What it can prove: the Butane config, the k3s-selinux reboot dance, k3s install,
# harness-system auto-deploy, Zincati + system-upgrade-controller config. What it can't:
# running the amd64 harness images — plain qemu has no Rosetta, so amd64 pods fail with
# `exec format error` here. Use the lima VM or an x64 host (test-libvirt.sh) for that.
#
# Forwards (127.0.0.1 only): ssh 2222, k8s API 26443, harness ingress 29443, registry 30501.
set -euo pipefail
cmd="${1:?usage: test-qemu-macos.sh up <ignition> [workdir] | down [workdir]}"

pidfile() { echo "$1/qemu.pid"; }
case "$cmd" in
  down)
    work="${2:-$PWD/fcos-test}"
    if [ -f "$(pidfile "$work")" ]; then kill "$(cat "$(pidfile "$work")")" 2>/dev/null || true; rm -f "$(pidfile "$work")"; fi
    echo "stopped (disk kept in $work)"; exit 0 ;;
  up) ;;
  *) echo "unknown command: $cmd" >&2; exit 2 ;;
esac

ign="$(cd "$(dirname "${2:?ignition file}")" && pwd)/$(basename "$2")"
work="${3:-$PWD/fcos-test}"
mkdir -p "$work"; cd "$work"

fw=/opt/homebrew/share/qemu
[ -f "$fw/edk2-aarch64-code.fd" ] || { echo "qemu UEFI firmware not found under $fw (brew install qemu)" >&2; exit 1; }

if ! ls fedora-coreos-*-qemu.aarch64.qcow2 >/dev/null 2>&1; then
  url="$(curl -sf https://builds.coreos.fedoraproject.org/streams/stable.json |
    jq -r '.architectures.aarch64.artifacts.qemu.formats["qcow2.xz"].disk.location')"
  echo "downloading $url"
  curl -sfLO "$url" && xz -d "$(basename "$url")"
fi
base="$(ls fedora-coreos-*-qemu.aarch64.qcow2 | tail -1)"

# A fresh overlay each `up`: Ignition only runs on first boot.
rm -f disk.qcow2 vars.fd console.log
qemu-img create -q -f qcow2 -F qcow2 -b "$base" disk.qcow2 40G
cp "$fw/edk2-arm-vars.fd" vars.fd

qemu-system-aarch64 \
  -machine virt,accel=hvf -cpu host -smp 4 -m 6144 \
  -drive if=pflash,format=raw,readonly=on,file="$fw/edk2-aarch64-code.fd" \
  -drive if=pflash,format=raw,file=vars.fd \
  -drive if=virtio,file=disk.qcow2 \
  -fw_cfg name=opt/com.coreos/config,file="$ign" \
  -nic user,model=virtio-net-pci,hostfwd=tcp:127.0.0.1:2222-:22,hostfwd=tcp:127.0.0.1:26443-:6443,hostfwd=tcp:127.0.0.1:29443-:9443,hostfwd=tcp:127.0.0.1:30501-:30500 \
  -display none -serial file:console.log \
  -daemonize -pidfile "$(pidfile "$work")"

echo "booting; console: $work/console.log"
echo "  ssh -p 2222 core@127.0.0.1"
echo "  kubeconfig: ssh -p 2222 core@127.0.0.1 sudo cat /etc/rancher/k3s/k3s.yaml | sed 's/:6443/:26443/'"
