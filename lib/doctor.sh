# shellcheck shell=bash
# Preflight checks for the harness.

doctor() {
  load_target
  local fail=0
  pass() { hok "$*"; }
  chk_warn() { hwarn "$*"; }
  chk_fail() { herr "$*"; fail=1; }

  # docker + lima
  if docker info >/dev/null 2>&1; then pass "docker reachable (context: $(docker context show 2>/dev/null))"
  else chk_fail "docker not reachable — is the lima VM running? (limactl start docker)"; fi

  # amd64 emulation (binaries are amd64)
  if [ "$(docker run --rm --platform linux/amd64 alpine:3 uname -m 2>/dev/null)" = "x86_64" ]; then
    pass "linux/amd64 emulation works"
  else chk_fail "cannot run linux/amd64 containers (need Rosetta/qemu emulation in the docker VM)"; fi

  # cross toolchain
  if command -v "${HARNESS_CC:-x86_64-unknown-linux-gnu-gcc}" >/dev/null 2>&1; then pass "cross toolchain present (${HARNESS_CC:-x86_64-unknown-linux-gnu-gcc})"
  else chk_fail "cross toolchain missing: brew install messense/macos-cross-toolchains/x86_64-unknown-linux-gnu"; fi

  # target env
  if [ -n "${CF_DNS_API_TOKEN:-}" ]; then pass "target '${TARGET:-default}': HARNESS_DOMAIN=$HARNESS_DOMAIN, DNS token set"
  else chk_fail "CF_DNS_API_TOKEN empty in targets/${TARGET:-default}.env"; fi
  [ -n "${ACME_EMAIL:-}" ] && pass "ACME_EMAIL=$ACME_EMAIL" || chk_warn "ACME_EMAIL unset (LE registration may warn)"

  # wildcard DNS -> loopback
  local ip; ip="$(dig +short "doctor.${LAB_DOMAIN}" 2>/dev/null | head -1)"
  if [ "$ip" = "127.0.0.1" ]; then pass "wildcard *.$LAB_DOMAIN resolves to 127.0.0.1"
  else chk_fail "*.$LAB_DOMAIN must resolve to 127.0.0.1 (got '${ip:-nothing}') — add the wildcard A record"; fi

  # python3 (authoritative TLS verifier; macOS curl/LibreSSL is unreliable)
  command -v python3 >/dev/null 2>&1 && pass "python3 present (TLS verification)" || chk_warn "python3 missing (used for web-UI verification)"

  # uv + the Python brain (YAML parsing / gating / checks validation)
  if command -v uv >/dev/null 2>&1; then
    if pybrain validate >/dev/null 2>&1; then pass "harness brain ok (uv); all modules validate"
    else chk_fail "module validation failed — run: $(basename "$0") validate"; fi
  else chk_fail "uv missing (harness brain) — install: https://docs.astral.sh/uv/"; fi

  # ingress + cert
  if docker ps --format '{{.Names}}' | grep -q '^harness-ingress$'; then
    pass "ingress running"
    if docker run --rm -v harness-certs:/certs alpine:3 sh -c '[ -s /certs/wildcard.crt ]' 2>/dev/null; then
      pass "wildcard cert issued"
    else chk_warn "ingress up but cert not issued yet (docker logs harness-certs)"; fi
  else chk_warn "ingress not running yet (will be started on first 'cluster up')"; fi

  # ingress host port
  if lsof -nP -iTCP:"${INGRESS_PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
    docker ps --format '{{.Ports}}' | grep -q ":${INGRESS_PORT}->" && pass "ingress port ${INGRESS_PORT} in use by ingress" || chk_warn "port ${INGRESS_PORT} in use by something else"
  else pass "ingress port ${INGRESS_PORT} free"; fi

  echo
  [ "$fail" = 0 ] && hok "doctor: all required checks passed" || die "doctor: some required checks failed (see above)"
}
