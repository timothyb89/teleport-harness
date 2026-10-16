# shellcheck shell=bash
# Backend dispatch. A backend is WHERE a cluster's containers run: "docker" (compose on the
# local daemon) today; "k8s" (a namespace on a remote k3s, docs/remote-kubernetes.md) next.
# Callers say `be <op> …` and never name docker; each lib/backend/<name>.sh implements every
# op as be_<name>_<op>.
#
# Which backend: a NEW cluster uses the target's (BACKEND= in targets/<t>.env, default
# docker) and records it in meta.env; anything that acts on an EXISTING cluster switches to
# the one recorded there (backend_for_cluster). So a cluster is always managed by the
# backend that created it, whatever target the current shell happens to be pointed at.
#
# The ops, and what each must do (the docker implementation is the reference):
#   preflight                      fail fast if the backend's tools are missing
#   doctor                         backend-specific doctor checks (pass/chk_warn/chk_fail)
#   infra_up                       shared, long-lived infra (ingress, cert) is up
#   image_exists <tag>             the teleport image for <tag> is already published
#   publish_image <bindir> <tag>   publish the binaries in <bindir> as image <tag>
#   image_version <tag>            print the image's raw `teleport version` line
#   binary_version <dir>           print the raw `teleport version` line of <dir>/teleport
#   up <id> <state-dir>            start every service of a rendered cluster, route ingress
#   auth_healthy <id>              exit 0 once auth is healthy (bootstrap included)
#   status <id>                    print the auth service's state, or "-"
#   logs <id> [args…]              stream/print service logs (args as for compose logs)
#   collect_logs <id> <dir>        write one <service>.log per service into <dir>
#   exec <id> <svc> <cmd…>         run a command in a service, stdin NOT attached
#   exec_i <id> <svc> <cmd…>       the same with stdin attached
#   running <id> <svc>             exit 0 if the service is running
#   teardown <id> <state-dir>      remove everything the cluster created
#   admin_up <id> <state-dir> <image> <tbot-config> <token>
#                                  run the long-lived admin tbot writing an identity
#   admin_ready <id>               exit 0 if admin access was ever set up
#   admin_identity_present <id>    exit 0 once the admin identity has been written
#   admin_copy_identity <id> <dest>  copy the identity file to the host
#   admin_run <id> <tty:0|1> <cmd…>  run a command in the cluster's image, on its network,
#                                  with the admin identity at /id/identity
BACKEND_OPS="preflight doctor infra_up image_exists publish_image image_version binary_version
up auth_healthy status logs collect_logs exec exec_i running teardown
admin_up admin_ready admin_identity_present admin_copy_identity admin_run"

# backend_use <name> — select a backend for the rest of this process. Refuses an unknown
# name or an incomplete implementation up front, rather than failing at the first missing
# op somewhere in the middle of a run.
backend_use() {
  local name="${1:?backend_use <name>}" op missing=""
  [ -f "$HARNESS_ROOT/lib/backend/$name.sh" ] || die "unknown backend '$name' (have: $(cd "$HARNESS_ROOT/lib/backend" && ls ./*.sh | sed 's#^\./##; s#\.sh$##' | tr '\n' ' '))"
  for op in $BACKEND_OPS; do
    declare -F "be_${name}_${op}" >/dev/null || missing="$missing $op"
  done
  [ -z "$missing" ] || die "backend '$name' is incomplete; missing ops:$missing"
  export HARNESS_BACKEND="$name"
}

# backend_for_cluster <id> — switch to the backend that created <id>. Clusters from before
# backends existed have no BACKEND in meta.env; they are docker by construction.
backend_for_cluster() {
  local b; b="$(cluster_meta "${1:?backend_for_cluster <id>}" BACKEND)"
  backend_use "${b:-docker}"
}

# be <op> [args…] — call the selected backend's implementation of <op>.
be() {
  local op="${1:?be <op> [args…]}"; shift
  "be_${HARNESS_BACKEND:?no backend selected (load_target selects one)}_${op}" "$@"
}

for _b in "$HARNESS_ROOT"/lib/backend/*.sh; do
  # shellcheck source=/dev/null
  . "$_b"
done
unset _b
