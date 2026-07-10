# Shared helpers for the cluster CLI. Sourced by bin/cluster and lib/*.sh.
# shellcheck shell=bash

set -euo pipefail

HARNESS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export HARNESS_ROOT
STATE_DIR="$HARNESS_ROOT/state"          # per-cluster generated artifacts (gitignored)
CACHE_DIR="$HARNESS_ROOT/.cache"         # SHA-keyed binaries (gitignored)
RUNS_DIR="$HARNESS_ROOT/runs"            # report bundles (gitignored)
INGRESS_DIR="$HARNESS_ROOT/ingress"
MODULES_DIR="$HARNESS_ROOT/modules"

# ---- logging -----------------------------------------------------------------
_c() { printf '\033[%sm' "$1"; }
hlog()  { printf '%s==>%s %s\n' "$(_c '1;34')" "$(_c 0)" "$*" >&2; }
hok()   { printf '%s ok %s %s\n' "$(_c '1;32')" "$(_c 0)" "$*" >&2; }
hwarn() { printf '%swarn%s %s\n' "$(_c '1;33')" "$(_c 0)" "$*" >&2; }
herr()  { printf '%serr %s %s\n' "$(_c '1;31')" "$(_c 0)" "$*" >&2; }
die()   { herr "$*"; exit 1; }

# ---- target config -----------------------------------------------------------
# Loads targets/<TARGET>.env (default: "default"). Exports the vars within.
load_target() {
  local target="${TARGET:-default}"
  local f="$HARNESS_ROOT/targets/${target}.env"
  [ -f "$f" ] || die "target env not found: $f (copy targets/default.env.example)"
  set -a; . "$f"; set +a
  : "${HARNESS_DOMAIN:?HARNESS_DOMAIN missing in $f}"
  export INGRESS_PORT="${INGRESS_PORT:-8443}"
  export LAB_DOMAIN="lab.${HARNESS_DOMAIN}"    # clusters live at <id>.$LAB_DOMAIN
  export TLS_PROVIDER="${TLS_PROVIDER:-le-dns01}"
}

# fqdn <id>  ->  <id>.lab.<domain>
fqdn() { echo "${1}.${LAB_DOMAIN}"; }

# compose <project> <compose-file> [args...]  — docker compose wrapper
compose() {
  local project="$1" file="$2"; shift 2
  docker compose -p "$project" -f "$file" "$@"
}

# gen_id — short cluster id (6 hex). Deterministic ids should be passed via --id.
gen_id() { echo "c$(openssl rand -hex 3)"; }

# require_cmd <cmd...>
require_cmd() { for c in "$@"; do command -v "$c" >/dev/null 2>&1 || die "missing required command: $c"; done; }

# pybrain <subcommand> [args...] — the typed Python brain (harness/ package) that
# owns YAML parsing, gating, and checks validation (replaces grep/sed/awk). Runs
# via uv against the harness pyproject; --modules-dir keeps it pointed at ours.
pybrain() {
  command -v uv >/dev/null 2>&1 || die "uv not found (install: https://docs.astral.sh/uv/) — needed for the harness brain"
  uv run --quiet --project "$HARNESS_ROOT" harness --modules-dir "$MODULES_DIR" "$@"
}

# state_dir_for <id>
state_dir_for() { echo "$STATE_DIR/$1"; }

# list_cluster_ids — ids that have state dirs
list_cluster_ids() { [ -d "$STATE_DIR" ] && ls -1 "$STATE_DIR" 2>/dev/null || true; }

# cluster_meta <id> <key>  — read a value from the cluster's meta.env
cluster_meta() { local d; d="$(state_dir_for "$1")"; [ -f "$d/meta.env" ] && (grep -E "^$2=" "$d/meta.env" | cut -d= -f2-) || true; }
