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
echo "[tf] $TF_BIN apply -auto-approve -input=false  (dev_overrides in effect -> no init)"
"$TF_BIN" apply -auto-approve -input=false 2>&1
echo "TF_APPLY_EXIT=$?"

# Signal "apply finished" so services that consume what terraform created (e.g. an agent
# joining with a TF-created token) can gate on this runner via `depends_on: service_healthy`
# + a `test -f /tmp/tf-apply-done` healthcheck. Touched on completion regardless of exit:
# a failed apply then surfaces downstream (the resource simply won't exist) rather than
# hanging dependents forever.
touch /tmp/tf-apply-done

# Keep the container alive for log inspection + exec-based checks / `cluster admin`.
tail -f /dev/null
