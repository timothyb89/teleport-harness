# shellcheck shell=bash
# Verification runner — delegates to the Python brain (harness/verify.py), which is
# the single source of truth for what each `checks:` verb means (it replaced the old
# lib/assert.sh). The brain parses+validates the module, runs every check against the
# live cluster via docker, prints the PASS/FAIL/SKIP + `RESULT:` lines this function's
# callers expect, writes a JSON report to state/<id>/results.json, and exits non-zero
# on any FAIL (so plan.sh's retry loop still works).

# run_verification <cluster-id> <module>  — writes state/<id>/results-<module>.json
# (per-module so a multi-module plan's results don't clobber each other).
run_verification() {
  local id="$1" module="$2" sdir
  sdir="$(state_dir_for "$id")"
  pybrain verify "$module" --cluster-id "$id" --state-dir "$sdir" \
    --json-out "$sdir/results-${module}.json"
}
