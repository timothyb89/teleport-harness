# shellcheck shell=bash
# Preflight checks for the harness.

doctor() {
  load_target
  local fail=0
  pass() { hok "$*"; }
  chk_warn() { hwarn "$*"; }
  chk_fail() { herr "$*"; fail=1; }

  # cross toolchain — only --repo (source-build) runs need it; --package/--binary don't
  if command -v "${HARNESS_CC:-x86_64-unknown-linux-gnu-gcc}" >/dev/null 2>&1; then pass "cross toolchain present (${HARNESS_CC:-x86_64-unknown-linux-gnu-gcc})"
  else chk_warn "cross toolchain missing (needed only for --repo builds; --package/--binary runs are fine): brew install messense/macos-cross-toolchains/x86_64-unknown-linux-gnu"; fi

  # target env — DNS-01 token issues the wildcard cert. On the k8s backend the cert is a
  # Secret provisioned on the host separately (infra/k8s/import-cert.sh or an in-cluster
  # acme.sh Pod), not by this client, so an empty token here is a warning, not a hard fail.
  if [ -n "${CF_DNS_API_TOKEN:-}" ]; then pass "target '${TARGET:-default}': HARNESS_DOMAIN=$HARNESS_DOMAIN, DNS token set"
  elif [ "$HARNESS_BACKEND" = k8s ]; then chk_warn "CF_DNS_API_TOKEN empty in targets/${TARGET:-default}.env (fine on the k8s backend if the host already has a wildcard-cert Secret)"
  else chk_fail "CF_DNS_API_TOKEN empty in targets/${TARGET:-default}.env"; fi
  [ -n "${ACME_EMAIL:-}" ] && pass "ACME_EMAIL=$ACME_EMAIL" || chk_warn "ACME_EMAIL unset (LE registration may warn)"

  # wildcard DNS -> loopback
  local ip; ip="$(dig +short "doctor.${LAB_DOMAIN}" 2>/dev/null | head -1)"
  if [ "$ip" = "127.0.0.1" ]; then pass "wildcard *.$LAB_DOMAIN resolves to 127.0.0.1"
  else chk_fail "*.$LAB_DOMAIN must resolve to 127.0.0.1 (got '${ip:-nothing}') — add the wildcard A record"; fi

  # python3 (authoritative TLS verifier; macOS curl/LibreSSL is unreliable)
  command -v python3 >/dev/null 2>&1 && pass "python3 present (TLS verification)" || chk_warn "python3 missing (used for web-UI verification)"

  # claude CLI (only agent-driven modules like docs_bound_keypair need it; subscription login, no API key)
  if command -v claude >/dev/null 2>&1; then pass "claude CLI present ($(claude --version 2>/dev/null | head -1)) — agent-driven modules can run"
  else chk_warn "claude CLI missing (only needed for agent-driven modules; must be logged in with a subscription)"; fi

  # TPM emulation (only the tpm module needs it: a lima qemu VM + a manufactured vTPM).
  # Warn rather than fail — every other module runs fine without any of this.
  local tpm_missing=""
  for c in swtpm swtpm_setup swtpm_localca gnutls-certtool; do
    command -v "$c" >/dev/null 2>&1 || tpm_missing="$tpm_missing $c"
  done
  # lima wires swtpm only for vmType: qemu, so the qemu binary for the HOST arch is what
  # the tpm module's VM actually boots on (a same-arch guest gets HVF acceleration).
  local qemu_bin; case "$(uname -m)" in
    arm64|aarch64) qemu_bin=qemu-system-aarch64 ;; x86_64) qemu_bin=qemu-system-x86_64 ;;
    *) qemu_bin="" ;;
  esac
  [ -n "$qemu_bin" ] && ! command -v "$qemu_bin" >/dev/null 2>&1 && tpm_missing="$tpm_missing $qemu_bin"
  if [ -z "$tpm_missing" ]; then
    # gnutls-certtool is NOT macOS's /usr/bin/certtool (a different program, same name);
    # swtpm_localca looks for the gnutls one by that exact name.
    pass "TPM emulation ok (swtpm $(swtpm --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1), $qemu_bin) — the tpm module can run"
  else
    chk_warn "TPM emulation unavailable (missing:$tpm_missing) — only the tpm module needs it: brew install swtpm qemu gnutls"
  fi

  # uv + the Python brain (YAML parsing / gating / checks validation)
  if command -v uv >/dev/null 2>&1; then
    if pybrain validate >/dev/null 2>&1; then pass "harness brain ok (uv); all modules validate"
    else chk_fail "module validation failed — run: $(basename "$0") validate"; fi
  else chk_fail "uv missing (harness brain) — install: https://docs.astral.sh/uv/"; fi

  # where clusters run: the selected backend's own checks (docker reachable, amd64
  # emulation, its ingress + cert, …)
  hlog "backend: $HARNESS_BACKEND"
  be doctor

  echo
  [ "$fail" = 0 ] && hok "doctor: all required checks passed" || die "doctor: some required checks failed (see above)"
}
