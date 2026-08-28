#!/bin/sh
# Shared entrypoint for a Terraform-provider test runner (mounted at /scripts/tf-entrypoint.sh
# and run as the container's entrypoint). Drives a DEV build of the Teleport provider
# (bind-mounted at /plugins) against the live cluster via `dev_overrides` — which makes
# Terraform/OpenTofu use the local binary directly and SKIP `init`/lockfiles entirely
# (no fs-mirror, no `terraform providers lock` hashing — the gist's biggest pain).
#
# Auth comes from env the module sets: TF_TELEPORT_ADDR + TF_TELEPORT_IDENTITY_FILE_PATH
# (the provider REJECTS the token join method, so identity_file_path is the path). Engine is
# $TF_BIN (terraform | tofu). Sources are copied out of the read-only /work mount into a
# writable dir because $TF_BIN writes terraform.tfstate into its cwd.
#
# NOTE: no `set -e` around the apply. A failing apply (e.g. the known must_match_fields
# provider bug) must still leave the container up so `docker logs`/checks can inspect it.
set -u
: "${TF_BIN:=terraform}"

export TF_CLI_CONFIG_FILE=/tmp/tf-cli.tfrc
cat > "$TF_CLI_CONFIG_FILE" <<EOF
provider_installation {
  dev_overrides {
    "terraform.releases.teleport.dev/gravitational/teleport" = "/plugins"
  }
  direct {}
}
EOF

mkdir -p /tmp/work
cp /work/*.tf /tmp/work/ 2>/dev/null || echo "[tf] warning: no .tf files in /work" >&2
cd /tmp/work || { echo "[tf] cannot enter workdir" >&2; tail -f /dev/null; }

echo "[tf] engine: $("$TF_BIN" version 2>&1 | head -1) | provider: /plugins | addr: ${TF_TELEPORT_ADDR:-?}"
echo "[tf] $TF_BIN apply -auto-approve -input=false -no-color  (dev_overrides in effect -> no init)"
# The braces put the exit code line INSIDE the pipeline, so `$?` is still the apply's (this
# is POSIX sh — no pipefail, no PIPESTATUS) while tee both streams the output live and
# keeps a copy to post-process below. -no-color keeps ANSI escapes out of that copy.
{ "$TF_BIN" apply -auto-approve -input=false -no-color 2>&1; echo "TF_APPLY_EXIT=$?"; } \
  | tee /tmp/tf-apply.log

# Terraform renders diagnostics in a box, WORD-WRAPPED at 78 columns, so a provider error
# message arrives split mid-sentence across several lines. Every log check here is
# line-oriented, so a check for a phrase the provider actually emitted fails purely on
# where terraform chose to break the line — which reads as "the error didn't happen".
#
# Emit one flattened, gutter-stripped copy so the message can be matched as written. Only
# on FAILURE: on success the log stays byte-identical to before, so no existing module's
# log_count tally moves.
if ! grep -q '^TF_APPLY_EXIT=0$' /tmp/tf-apply.log; then
  echo "TF_DIAG_FLAT: $(sed -e 's/^╷$//' -e 's/^╵$//' -e 's/^│ \{0,1\}//' /tmp/tf-apply.log \
    | tr '\n\t' '  ' | tr -s ' ')"
fi

# Signal "apply finished" so services that consume what terraform created (e.g. an agent
# joining with a TF-created token) can gate on this runner via `depends_on: service_healthy`
# + a `test -f /tmp/tf-apply-done` healthcheck. Touched on completion regardless of exit:
# a failed apply then surfaces downstream (the resource simply won't exist) rather than
# hanging dependents forever.
touch /tmp/tf-apply-done

# Keep the container alive for log inspection + exec-based checks / `cluster admin`.
tail -f /dev/null
