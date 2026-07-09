# Custom check (sourced): the bound_keypair-produced identity authenticates + is
# authorized (lists tokens via tctl --identity).
if docker exec "${ASSERT_ID}-bkbot" \
     tctl --identity /out/id/identity --auth-server auth:3025 tokens ls >/dev/null 2>&1; then
  _al PASS "bk-bot identity authenticates + performs an authorized action"
else
  _al FAIL "bk-bot identity could not perform its authorized action"
fi
