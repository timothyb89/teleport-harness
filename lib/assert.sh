# Shared assertion primitives for module verification. shellcheck shell=bash
#
# Each `assert_<name>` emits a "PASS|FAIL|SKIP <msg>" line and tracks failures in
# $_assert_fail. They read the cluster id from $ASSERT_ID (set by assert_begin).
# Used by lib/verify.sh's declarative `checks:` runner AND by a module's optional
# checks.sh escape hatch (which is sourced, so it shares this state + $_assert_nodes).
#
# The vocabulary is open: any assert_<name> function defined here (or in a module's
# checks.sh) can be used as a `checks:` verb. Add primitives here as new areas need them.

_assert_fail=0
_assert_nodes='[]'

_al() { printf '  %-4s %s\n' "$1" "$2"; [ "$1" = FAIL ] && _assert_fail=1; return 0; }

# assert_begin <cluster-id> — reset state and cache the cluster's node list once.
assert_begin() {
  ASSERT_ID="$1"; _assert_fail=0
  _assert_nodes="$(docker exec "${ASSERT_ID}-auth" tctl get nodes --format json 2>/dev/null || echo '[]')"
}
# assert_result — print the combined RESULT and return non-zero on any FAIL.
assert_result() { [ "$_assert_fail" = 0 ] && echo "RESULT: PASS" || echo "RESULT: FAIL"; return "$_assert_fail"; }

_host() { echo "${ASSERT_ID}-$1"; }   # checks reference the nodename suffix after "<id>-"

# --- node join outcomes -------------------------------------------------------
assert_node_present() {
  local h; h="$(_host "$1")"
  if jq -e --arg h "$h" 'any(.[]?; .spec.hostname==$h)' >/dev/null 2>&1 <<<"$_assert_nodes"; then
    _al PASS "node $h joined"; else _al FAIL "node $h did not join"; fi
}
assert_node_absent() {
  local h; h="$(_host "$1")"
  if jq -e --arg h "$h" 'any(.[]?; .spec.hostname==$h)' >/dev/null 2>&1 <<<"$_assert_nodes"; then
    _al FAIL "node $h present but expected absent (denied)"; else _al PASS "node $h absent (denied)"; fi
}
assert_node_scope() {   # <suffix> <scope>
  local h scope got; h="$(_host "$1")"; scope="$2"
  got="$(jq -r --arg h "$h" 'first(.[]? | select(.spec.hostname==$h) | .scope) // ""' <<<"$_assert_nodes" 2>/dev/null)"
  [ "$got" = "$scope" ] && _al PASS "node $h scope=$scope" || _al FAIL "node $h scope='${got}' expected '$scope'"
}

# --- log / audit assertions ---------------------------------------------------
assert_log_contains() {   # <container-suffix> <regex...>
  local c="${ASSERT_ID}-$1"; shift; local re="$*" logs
  # Capture first: piping into `grep -q` under `set -o pipefail` returns the
  # producer's SIGPIPE (non-zero) on an early match, which would look like "no match".
  logs="$(docker logs "$c" 2>&1 || true)"
  if grep -qiE "$re" <<<"$logs"; then _al PASS "$c log matches /$re/"
  else _al SKIP "$c log has no match for /$re/ yet"; fi
}

# --- usability (opt-in; needs a valid login + RBAC on the target node) --------
assert_tsh_ssh() {   # <suffix> [login]
  local h login; h="$(_host "$1")"; login="${2:-root}"
  if cluster_tsh "$ASSERT_ID" ssh "${login}@${h}" -- echo harness-ok 2>/dev/null | grep -q harness-ok; then
    _al PASS "tsh ssh ${login}@${h} works"; else _al FAIL "tsh ssh ${login}@${h} failed"; fi
}
