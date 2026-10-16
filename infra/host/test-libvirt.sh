#!/usr/bin/env bash
# Boot the real host config in a Fedora CoreOS VM on an x86_64 libvirt host — the closest
# thing to the real box (same arch, so the amd64 harness images run natively).
#
#   infra/host/test-libvirt.sh <ignition> [name]
#   virsh destroy <name> && virsh undefine --remove-all-storage <name>   # tear down
#
# Run ON the libvirt host (copy the .ign over). Follows the Fedora CoreOS libvirt guide:
# Ignition is handed over with qemu's fw_cfg. Needs virt-install, and coreos-installer
# or curl+jq+xz to fetch the image.
set -euo pipefail
ign="$(realpath "${1:?usage: test-libvirt.sh <ignition> [name]}")"
name="${2:-harness-k3s-test}"
images=/var/lib/libvirt/images

img="$(ls "$images"/fedora-coreos-*-qemu.x86_64.qcow2 2>/dev/null | tail -1 || true)"
if [ -z "$img" ]; then
  url="$(curl -sf https://builds.coreos.fedoraproject.org/streams/stable.json |
    jq -r '.architectures.x86_64.artifacts.qemu.formats["qcow2.xz"].disk.location')"
  (cd "$images" && sudo curl -sfLO "$url" && sudo xz -d "$(basename "$url")")
  img="$images/$(basename "${url%.xz}")"
fi

# libvirt's qemu must be able to read the Ignition file: put it next to the images and,
# on an SELinux host, label it like the docs say.
sudo cp "$ign" "$images/$name.ign"
if command -v chcon >/dev/null 2>&1 && [ "$(getenforce 2>/dev/null)" = Enforcing ]; then
  sudo chcon --verbose --type svirt_home_t "$images/$name.ign"
fi

sudo virt-install --connect qemu:///system --name "$name" \
  --vcpus 4 --memory 8192 --os-variant fedora-coreos-stable \
  --import --graphics none --noautoconsole \
  --disk "size=60,backing_store=$img" \
  --network network=default \
  --qemu-commandline="-fw_cfg name=opt/com.coreos/config,file=$images/$name.ign"

echo "started $name; address: sudo virsh domifaddr $name ; then ssh core@<ip>"
