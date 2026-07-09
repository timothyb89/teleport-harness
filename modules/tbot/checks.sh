# Custom check (sourced): prove the tbot-produced identity actually authenticates
# AND is authorized — run an allowed action (tokens ls) with it via tctl --identity.
if docker exec "${ASSERT_ID}-tbot" \
     tctl --identity /out/id/identity --auth-server auth:3025 tokens ls >/dev/null 2>&1; then
  _al PASS "test-bot identity authenticates + performs an authorized action"
else
  _al FAIL "test-bot identity could not perform its authorized action"
fi
