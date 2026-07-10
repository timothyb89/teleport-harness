# Verification runner: execute a module's declarative `checks:` (from module.yaml)
# through the shared assertion library, then an optional `checks.sh` escape hatch.
# shellcheck shell=bash

. "$(dirname "${BASH_SOURCE[0]}")/assert.sh"

# run_verification <cluster-id> <module> — prints PASS/FAIL/SKIP lines + RESULT,
# returns non-zero on any FAIL.
run_verification() {
  local id="$1" module="$2"
  local mdir="$MODULES_DIR/$module"

  assert_begin "$id"

  # Declarative checks come from the Python brain, which parses module.yaml with a
  # real YAML parser and validates every verb + arity BEFORE we run anything (so a
  # typo fails fast with a clear message instead of mid-loop). Each emitted line is
  # a normalized "<assert-verb> <args...>" that we dispatch to assert_<verb>.
  local checks line fn
  if ! checks="$(pybrain checks "$module" 2>&1)"; then
    _al FAIL "module '$module' checks invalid:"; printf '%s\n' "$checks" | sed 's/^/    /'
    assert_result; return
  fi
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    # shellcheck disable=SC2086
    set -- $line
    fn="assert_$1"; shift
    if declare -F "$fn" >/dev/null 2>&1; then "$fn" "$@"
    else _al FAIL "unknown check verb (no $fn)"; fi
  done <<<"$checks"

  # Escape hatch: a module may add arbitrary custom checks in checks.sh. It is
  # SOURCED (shares $ASSERT_ID, $_assert_nodes, $_assert_fail, _al, and all asserts).
  if [ -f "$mdir/checks.sh" ]; then
    # shellcheck disable=SC1090
    . "$mdir/checks.sh"
  fi

  assert_result
}
