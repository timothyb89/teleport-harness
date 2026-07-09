#!/usr/bin/env bash
# Assert generic_oidc join outcomes for a running cluster.
#   Usage: verify.sh <cluster-id>
# Emits "PASS|FAIL|SKIP  <check>" lines + a final "RESULT: PASS|FAIL"; exit 1 on any FAIL.
set -uo pipefail

ID="${1:?usage: verify.sh <cluster-id>}"
AUTH="${ID}-auth"
POSITIVE="discovery static scoped-discovery scoped-static"
NEGATIVE="deny scoped-deny"
SCOPE="/genericoidc-test"

fail=0
line() { printf '%-5s %s\n' "$1" "$2"; [ "$1" = FAIL ] && fail=1; return 0; }

nodes="$(docker exec "$AUTH" tctl get nodes --format json 2>/dev/null || echo '[]')"
has_node() { grep -q "\"hostname\": \"${ID}-agent-$1\"" <<<"$nodes"; }

echo "# positive joins (expected present)"
for n in $POSITIVE; do
  if has_node "$n"; then line PASS "agent-$n joined"; else line FAIL "agent-$n did not join"; fi
done

echo "# negative joins (expected denied / absent)"
for n in $NEGATIVE; do
  if has_node "$n"; then line FAIL "agent-$n joined but must be denied"; else line PASS "agent-$n absent (denied)"; fi
  logs="$(docker logs "${ID}-agent-$n" 2>&1 || true)"
  if grep -qiE "unable to (join via|validate) generic_oidc|access denied|denied" <<<"$logs"; then
    line PASS "agent-$n logged a denial"
  else
    line SKIP "agent-$n denial not yet logged (still retrying?)"
  fi
done

echo "# scope pinning (expected 2)"
sc="$(grep -c "\"scope\": \"$SCOPE\"" <<<"$nodes")"
[ "$sc" = 2 ] && line PASS "2 nodes scope-pinned to $SCOPE" || line FAIL "expected 2 scope-pinned nodes, got $sc"

echo
[ "$fail" = 0 ] && echo "RESULT: PASS" || echo "RESULT: FAIL"
exit "$fail"
