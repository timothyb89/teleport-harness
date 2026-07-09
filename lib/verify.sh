# Verification runner: execute a module's declarative `checks:` (from module.yaml)
# through the shared assertion library, then an optional `checks.sh` escape hatch.
# shellcheck shell=bash

. "$(dirname "${BASH_SOURCE[0]}")/assert.sh"

# run_verification <cluster-id> <module> — prints PASS/FAIL/SKIP lines + RESULT,
# returns non-zero on any FAIL.
run_verification() {
  local id="$1" module="$2"
  local mdir="$MODULES_DIR/$module" myaml="$MODULES_DIR/$module/module.yaml"

  assert_begin "$id"

  # Declarative checks: a `checks: |` block of "<assert-verb> <args...>" lines.
  local line fn
  while IFS= read -r line; do
    line="${line#"${line%%[![:space:]]*}"}"          # left-trim
    [ -z "$line" ] && continue
    case "$line" in \#*) continue ;; esac
    # shellcheck disable=SC2086
    set -- $line
    fn="assert_$1"; shift
    if declare -F "$fn" >/dev/null 2>&1; then "$fn" "$@"
    else _al FAIL "unknown check verb '$1' (no $fn)"; fi
  done < <(awk '
      /^checks:[[:space:]]*\|?[[:space:]]*$/ {b=1; next}
      b && /^[^[:space:]]/ {b=0}
      b {print}
    ' "$myaml" 2>/dev/null)

  # Escape hatch: a module may add arbitrary custom checks in checks.sh. It is
  # SOURCED (shares $ASSERT_ID, $_assert_nodes, $_assert_fail, _al, and all asserts).
  if [ -f "$mdir/checks.sh" ]; then
    # shellcheck disable=SC1090
    . "$mdir/checks.sh"
  fi

  assert_result
}
