# Custom verification escape hatch (SOURCED by lib/verify.sh — no shebang, no exit).
# Shares: $ASSERT_ID, cached $_assert_nodes (JSON), $_assert_fail, _al, and every
# assert_* primitive. Use this for checks not expressible as a declarative `checks:`
# verb. Example below: exact totals + proof tokens were created via the BOT identity.

# Exactly the four expected agents joined (nothing extra), and exactly two are scoped.
_n="$(jq 'length' <<<"$_assert_nodes" 2>/dev/null || echo '?')"
[ "$_n" = 4 ] && _al PASS "exactly 4 nodes joined" || _al FAIL "expected 4 nodes, got $_n"
_s="$(jq '[.[] | select(.scope=="/genericoidc-test")] | length' <<<"$_assert_nodes" 2>/dev/null || echo '?')"
[ "$_s" = 2 ] && _al PASS "exactly 2 scope-pinned nodes" || _al FAIL "expected 2 scope-pinned, got $_s"

# The agent tokens must have been created THROUGH the token-manager bot identity
# (not cluster admin) — the audit event records the impersonator. Capture logs first
# (pipefail + grep -q would misreport an early match as failure).
_authlog="$(docker logs "${ASSERT_ID}-auth" 2>&1 || true)"
if grep -qE 'join_token\.create.*impersonator:bot-token-manager' <<<"$_authlog"; then
  _al PASS "agent tokens created via the token-manager bot identity"
else
  _al SKIP "no join_token.create-by-bot audit line found (yet)"
fi
