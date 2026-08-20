#!/usr/bin/env bash
# Host-side setup for the TPM join matrix.
#
# TPM joining needs a real TPM device, which no container can have: a TPM is provided by
# the hypervisor to a guest kernel. So this module's "agent" is a lima VM with an emulated
# TPM (swtpm) rather than a compose service, and everything that must exist before the
# cluster comes up is built here.
#
# The load-bearing detail: lima's `tpm: true` starts swtpm against an EMPTY state dir and
# never runs swtpm_setup, so the guest gets a TPM with no EK certificate at all (verified:
# zero NV indices, zero persistent handles). An EKCert only exists if we manufacture one
# ourselves — which is also what makes this module possible, because manufacturing it means
# we own the issuing CA and can therefore test EKCert verification against a CA we control.
#
# Steps:
#   1. build a guest-arch tbot from the clone (CGO-free; cached by git HEAD)
#   2. mint two CAs — one that signs the device's EKCert, one that never signs anything
#   3. manufacture TPM state: EK + EKCert issued by CA "good", into NV 0x1c00002
#   4. create the VM, pre-seed that state, boot it, stage tbot, point the cluster FQDN at
#      the host gateway
#   5. ask the REAL client (`tbot tpm identify`) what the device looks like, and write it to
#      facts.json for checks.py to build allow rules from
#
# Env comes from the render context (see harness/render.py::_run_prebuild):
#   OUT CLUSTER_ID FQDN PORT REPO MODULE_DIR + this module's render.yaml vars.
set -euo pipefail

say() { printf '[tpm-prebuild] %s\n' "$*" >&2; }
die() { printf '[tpm-prebuild] ERROR: %s\n' "$*" >&2; exit 1; }

: "${OUT:?}" "${CLUSTER_ID:?}" "${FQDN:?}" "${REPO:?}"
: "${TPM_VM_CPUS:=2}" "${TPM_VM_MEMORY:=2GiB}" "${TPM_VM_DISK:=16GiB}"
: "${TPM_VM_UBUNTU:=24.04}"

VM="tpm-${CLUSTER_ID}"
TPMDIR="$OUT/tpm"
HARNESS_ROOT="$(cd "$(dirname "$MODULE_DIR")/.." && pwd)"
BINCACHE="$HARNESS_ROOT/.cache/tpm-bin"

# --- 0. preflight ------------------------------------------------------------------
# swtpm_localca shells out to gnutls-certtool (NOT macOS's /usr/bin/certtool, which is a
# different program with the same name); `brew install swtpm` pulls gnutls in, but a PATH
# without brew's bin would fail deep inside manufacturing with a confusing error.
for c in limactl swtpm swtpm_setup swtpm_localca gnutls-certtool go; do
  command -v "$c" >/dev/null 2>&1 || die "missing required command: $c (brew install lima swtpm gnutls)"
done

# lima's TPM emulation is qemu-only (vz/krunkit have no swtpm wiring), so the guest is the
# host's own arch under HVF — emulating a foreign arch would cost minutes per boot for a
# workload that is one tbot invocation.
case "$(uname -m)" in
  arm64|aarch64) GUEST_ARCH=aarch64; GOARCH=arm64; IMG_ARCH=arm64 ;;
  x86_64)        GUEST_ARCH=x86_64;  GOARCH=amd64; IMG_ARCH=amd64 ;;
  *) die "unsupported host arch $(uname -m)" ;;
esac
command -v "qemu-system-${GUEST_ARCH}" >/dev/null 2>&1 \
  || die "missing qemu-system-${GUEST_ARCH} (brew install qemu) — lima needs it for tpm: true"

mkdir -p "$TPMDIR" "$BINCACHE"

# --- 1. guest-arch tbot ------------------------------------------------------------
# NOT the harness's linux/amd64 image binaries: those run in containers under rosetta,
# while this one runs natively in the guest. CGO_ENABLED=0 keeps it to one static binary
# with no cross-toolchain (tbot needs no cgo), which is why this is ~50s and not a
# toolchain install. Cached by HEAD like lib/build.sh, so re-runs are free.
SHA="$(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo nogit)"
TBOT="$BINCACHE/$SHA-$GOARCH/tbot"
if [ ! -x "$TBOT" ]; then
  say "building tbot (linux/$GOARCH, CGO-free) from $(basename "$REPO") @ ${SHA:0:12}"
  mkdir -p "$(dirname "$TBOT")"
  ( cd "$REPO" && GOOS=linux GOARCH="$GOARCH" CGO_ENABLED=0 \
      go build -buildvcs=false -tags grpcnotrace -ldflags "-s -w" -o "$TBOT" ./tool/tbot )
else
  say "reusing cached tbot for ${SHA:0:12} ($GOARCH)"
fi

# --- 2. two CAs --------------------------------------------------------------------
# "good" signs the device's EKCert; "bad" signs nothing and exists only to be configured
# as a token's ekcert_allowed_cas, so the invalid-EKCert case varies exactly one thing (the
# trust anchor) while the device and the allow rule stay identical.
#
# swtpm_localca builds a two-level chain (root -> intermediate "swtpm-localca" -> EKCert)
# and issuercert.pem is the INTERMEDIATE. That is the cert a token must carry: Teleport
# verifies with Roots only and builds no intermediates pool (lib/tpm/validate.go
# verifyEKCert), so pinning the root instead fails with "signed by unknown authority".
# Real manufacturer EK certs chain through intermediates too, so this is the real-world
# shape, not an artifact of swtpm.
mint_ca() {
  local dir="$1" label="$2"
  [ -f "$dir/issuercert.pem" ] && return 0
  mkdir -p "$dir"
  cat > "$dir/localca.conf" <<EOF
statedir = $dir
signingkey = $dir/signkey.pem
issuercert = $dir/issuercert.pem
certserial = $dir/certserial
EOF
  cat > "$dir/localca.options" <<EOF
--platform-manufacturer HarnessLab
--platform-version 1.0
--platform-model $label
EOF
  cat > "$dir/setup.conf" <<EOF
create_certs_tool = $(command -v swtpm_localca)
create_certs_tool_config = $dir/localca.conf
create_certs_tool_options = $dir/localca.options
active_pcr_banks = sha256
rsa_keysize = 2048
EOF
}
mint_ca "$TPMDIR/ca-good" "TeleportHarnessGood"
mint_ca "$TPMDIR/ca-bad"  "TeleportHarnessBad"

# The bad CA is only ever a trust anchor in a token, so force its chain into existence by
# manufacturing a throwaway TPM against it. Cheap, and keeps mint_ca free of a second path.
if [ ! -f "$TPMDIR/ca-bad/issuercert.pem" ] || [ ! -s "$TPMDIR/ca-bad/issuercert.pem" ]; then
  rm -rf "$TPMDIR/.throwaway"; mkdir -p "$TPMDIR/.throwaway"
  swtpm_setup --tpm2 --tpmstate "dir://$TPMDIR/.throwaway" --create-ek-cert \
    --config "$TPMDIR/ca-bad/setup.conf" --overwrite \
    --logfile "$TPMDIR/ca-bad/manufacture.log" >/dev/null 2>&1 \
    || die "could not mint the decoy CA (see $TPMDIR/ca-bad/manufacture.log)"
  rm -rf "$TPMDIR/.throwaway"
fi

# --- 3. manufacture the device -----------------------------------------------------
# Each run of swtpm_setup regenerates the TPM's seeds, so the EK pub hash AND the EKCert
# serial are both fresh — one provisioning = one distinct "device".
if [ ! -f "$TPMDIR/state/tpm2-00.permall" ]; then
  say "manufacturing TPM state (EK + EKCert signed by ca-good)"
  mkdir -p "$TPMDIR/state"
  swtpm_setup --tpm2 --tpmstate "dir://$TPMDIR/state" --create-ek-cert --create-platform-cert \
    --config "$TPMDIR/ca-good/setup.conf" --overwrite \
    --logfile "$TPMDIR/manufacture.log" >/dev/null 2>&1 \
    || die "swtpm_setup failed (see $TPMDIR/manufacture.log)"
  grep -q "NVRAM area 0x1c00002" "$TPMDIR/manufacture.log" \
    || die "no EKCert written to NV 0x1c00002 (see $TPMDIR/manufacture.log)"
fi

# --- 4. the VM ---------------------------------------------------------------------
if ! limactl list --format '{{.Name}}' 2>/dev/null | grep -qx "$VM"; then
  say "creating lima VM '$VM' (qemu/$GUEST_ARCH, tpm: true)"
  cat > "$TPMDIR/lima.yaml" <<EOF
# Generated by modules/tpm/prebuild.sh. vmType MUST be qemu: lima only wires swtpm there.
vmType: qemu
arch: $GUEST_ARCH
images:
- location: "https://cloud-images.ubuntu.com/releases/${TPM_VM_UBUNTU}/release/ubuntu-${TPM_VM_UBUNTU}-server-cloudimg-${IMG_ARCH}.img"
  arch: "$GUEST_ARCH"
cpus: $TPM_VM_CPUS
memory: "$TPM_VM_MEMORY"
disk: "$TPM_VM_DISK"
tpm: true
mounts: []
containerd:
  system: false
  user: false
hostResolver:
  hosts:
    $FQDN: "192.168.5.2"
EOF
  limactl create --name="$VM" --tty=false "$TPMDIR/lima.yaml" >/dev/null 2>&1 \
    || die "limactl create failed for $VM"
fi

# Pre-seed BEFORE first boot: swtpm reads its state dir at startup, so a state file dropped
# in now is the TPM the guest sees on its very first boot — no boot/stop/reboot cycle.
LIMA_HOME="${LIMA_HOME:-$HOME/.lima}"
mkdir -p "$LIMA_HOME/$VM/swtpm"
cp "$TPMDIR/state/tpm2-00.permall" "$LIMA_HOME/$VM/swtpm/tpm2-00.permall"

# NB: --quiet cannot be combined with --format (limactl: "can only be used with
# '--format table'"), and it fails fatally rather than degrading.
if ! limactl list --format '{{.Status}}' "$VM" 2>/dev/null | grep -qi running; then
  say "starting '$VM'"
  limactl start "$VM" --tty=false >/dev/null 2>&1 || die "limactl start failed for $VM"
fi

limactl shell "$VM" -- test -c /dev/tpmrm0 \
  || die "$VM has no /dev/tpmrm0 — lima's tpm: true did not take effect"

# --- 5. stage the client + reach the cluster ---------------------------------------
# hostResolver only covers lima's own DNS; /etc/hosts is what a fresh VM actually consults
# first and survives the guest's resolver config, so pin it there too. 192.168.5.2 is
# lima's user-mode gateway = the host, where harness-ingress publishes {{ port }}.
limactl copy "$TBOT" "$VM:/tmp/tbot" >/dev/null 2>&1 || die "could not copy tbot into $VM"
limactl shell "$VM" -- sudo install -m 0755 /tmp/tbot /usr/local/bin/tbot
limactl shell "$VM" -- sudo sh -c \
  "grep -q ' $FQDN\$' /etc/hosts || echo '192.168.5.2 $FQDN' >> /etc/hosts"

# --- 6. what does the device actually look like? -----------------------------------
# Ask the real client rather than parsing the cert on the host: this is the same
# go-attestation path the join uses, so the values in facts.json are by construction the
# values auth will compare against, not a re-derivation that could drift.
IDENT="$(limactl shell "$VM" -- sudo tbot tpm identify 2>&1)" || die "tbot tpm identify failed: $IDENT"
EK_HASH="$(printf '%s\n' "$IDENT" | sed -n 's/^EK Public Hash: *//p'  | head -1)"
EK_SERIAL="$(printf '%s\n' "$IDENT" | sed -n 's/^EK Certificate Serial: *//p' | head -1)"
[ -n "$EK_HASH" ]   || die "could not read EK public hash from: $IDENT"
[ -n "$EK_SERIAL" ] || die "no EKCert serial — the TPM was not manufactured with one: $IDENT"
say "device: hash=${EK_HASH:0:16}… serial=$EK_SERIAL"

python3 - "$TPMDIR" "$VM" "$EK_HASH" "$EK_SERIAL" "$FQDN" <<'PY'
import json, sys, pathlib
tpmdir, vm, ek_hash, ek_serial, fqdn = sys.argv[1:6]
d = pathlib.Path(tpmdir)
facts = {
    "vm": vm,
    "fqdn": fqdn,
    "ek_public_hash": ek_hash,
    "ek_certificate_serial": ek_serial,
    "ca_good": (d / "ca-good" / "issuercert.pem").read_text(),
    "ca_bad": (d / "ca-bad" / "issuercert.pem").read_text(),
    # The root of the good chain: a trust anchor that is genuinely part of the device's
    # chain yet still fails, because Teleport supplies no intermediates pool. Configuring
    # it is the mistake an admin makes when told to "add the manufacturer CA".
    "ca_good_root": (d / "ca-good" / "swtpm-localca-rootca-cert.pem").read_text(),
}
(d / "facts.json").write_text(json.dumps(facts, indent=2) + "\n")
PY

say "ready — facts at $TPMDIR/facts.json"
