#!/usr/bin/env bash
#
# Render Teleport generic_oidc token resources from a running trivial OIDC
# server. Fetches the server's self-signed CA and JWKS and writes:
#
#   out/token-discovery.yaml           bot, issuer + tls_ca (discovery + caching)
#   out/token-static-jwks.yaml         bot, static_jwks     (no fetch at join time)
#   out/token-agent-discovery.yaml     agent (Node), discovery + tls_ca
#   out/token-agent-static-jwks.yaml   agent (Node), static_jwks
#   out/scoped-token-*.yaml            scoped variants of the above
#
# Usage:
#   ISSUER=https://localhost:8443 genericoidc-test/render-resources.sh
#
#   # render from your laptop without reaching the server's localhost:8443 —
#   # FETCH_URL is only used for the curl calls; ISSUER stays in the YAML:
#   ISSUER=https://localhost:8443 FETCH_URL=https://teleport.ethernet.fyi:8443 \
#     genericoidc-test/render-resources.sh
#   # or over an ssh tunnel: ssh -L 8443:localhost:8443 host, then
#   ISSUER=https://localhost:8443 FETCH_URL=https://localhost:8443 ...
#
# Env:
#   ISSUER    issuer URL the server was started with, written into the tokens
#                                          (default https://localhost:8443)
#   FETCH_URL base URL to curl /ca and /keys from, if different from ISSUER
#                                          (default $ISSUER)
#   AUDIENCE  expected audience            (default teleport.ethernet.fyi/generic-oidc-test)
#   BOT_NAME  bot the token provisions     (default generic-oidc-test)
#   OUT_DIR   output directory             (default genericoidc-test/out)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ISSUER="${ISSUER:-https://localhost:8443}"
FETCH_URL="${FETCH_URL:-$ISSUER}"
AUDIENCE="${AUDIENCE:-teleport.ethernet.fyi/generic-oidc-test}"
BOT_NAME="${BOT_NAME:-generic-oidc-test}"
OUT_DIR="${OUT_DIR:-$HERE/out}"
# Agent (non-bot, e.g. Node) token knobs. Agents join the cluster as a Teleport
# service rather than a bot, so the token grants a system role (Node) and carries
# no bot_name. The OIDC server mints whatever `sub` you ask for via ?sub=, so the
# agent's join config should curl ?sub=$AGENT_SUB to match the rule below.
AGENT_ROLE="${AGENT_ROLE:-Node}"
AGENT_NAME="${AGENT_NAME:-generic-oidc-agent}"
AGENT_SUB="${AGENT_SUB:-test-agent}"
# Scoped-token knobs (used for the additional scoped-* outputs).
SCOPE="${SCOPE:-/genericoidc-test}"
SCOPED_BOT_NAME="${SCOPED_BOT_NAME:-generic-oidc-scoped-bot}"
SCOPED_AGENT_NAME="${SCOPED_AGENT_NAME:-generic-oidc-scoped-agent}"
# Kubernetes-token knobs (for the kube static_jwks regression check). The kube
# join method requires the audience to be the Teleport cluster name.
CLUSTER_NAME="${CLUSTER_NAME:-teleport.ethernet.fyi}"
K8S_NAMESPACE="${K8S_NAMESPACE:-default}"
K8S_SERVICE_ACCOUNT="${K8S_SERVICE_ACCOUNT:-teleport-bot}"
K8S_BOT_NAME="${K8S_BOT_NAME:-k8s-test-bot}"

mkdir -p "$OUT_DIR"

# Strip any trailing slash so "$FETCH_URL/ca" is always well-formed.
FETCH_URL="${FETCH_URL%/}"

echo "==> Fetching CA and JWKS from $FETCH_URL (TLS verification skipped; just reading public material)"
if [[ "$FETCH_URL" != "$ISSUER" ]]; then
  echo "    (issuer in the rendered tokens stays: $ISSUER)"
fi
CA_PEM="$(curl -fsSk "$FETCH_URL/ca")"
JWKS="$(curl -fsSk "$FETCH_URL/keys")"

# Indent helpers for YAML block scalars.
indent() { sed "s/^/$1/"; }

# tls_ca block for the discovery tokens. When OMIT_TLS_CA=1 (the oidc-server serves a
# system-trusted cert, e.g. the wildcard LE cert), emit no tls_ca and let discovery
# validate over system trust — the same path the kube `oidc` join uses.
if [[ "${OMIT_TLS_CA:-0}" == "1" ]]; then
  TLS_CA_BLOCK=""
else
  TLS_CA_BLOCK="$(printf '    tls_ca: |\n%s' "$(printf '%s\n' "$CA_PEM" | indent '      ')")"
fi

DISCOVERY="$OUT_DIR/token-discovery.yaml"
cat > "$DISCOVERY" <<EOF
# generic_oidc token using live OIDC discovery + a custom TLS CA.
# Teleport fetches ${ISSUER}/.well-known/openid-configuration and the JWKS,
# verifying the server cert against tls_ca below.
kind: token
version: v2
metadata:
  name: ${BOT_NAME}-discovery
  expires: "2035-01-01T00:00:00Z"
spec:
  roles: [Bot]
  bot_name: ${BOT_NAME}
  join_method: generic_oidc
  generic_oidc:
    issuer: ${ISSUER}
    audience: ${AUDIENCE}
${TLS_CA_BLOCK}
    # Global AND-matched fields. Every minted token carries org=ethernet-fyi.
    must_match_fields:
      org: ethernet-fyi
    # OR-matched rules; at least one must pass. Allows sub == test-bot.
    allow_any:
      - conditions:
          - attribute: sub
            eq:
              value: test-bot
EOF
echo "    wrote $DISCOVERY"

STATIC="$OUT_DIR/token-static-jwks.yaml"
cat > "$STATIC" <<EOF
# generic_oidc token using a static JWKS (no discovery / network fetch at join
# time). issuer + audience are still verified against the JWT claims.
kind: token
version: v2
metadata:
  name: ${BOT_NAME}-static
  expires: "2035-01-01T00:00:00Z"
spec:
  roles: [Bot]
  bot_name: ${BOT_NAME}
  join_method: generic_oidc
  generic_oidc:
    issuer: ${ISSUER}
    audience: ${AUDIENCE}
    static_jwks: |
$(printf '%s\n' "$JWKS" | indent '      ')
    must_match_fields:
      org: ethernet-fyi
    allow_any:
      - conditions:
          - attribute: sub
            eq:
              value: test-bot
EOF
echo "    wrote $STATIC"

# ---------------------------------------------------------------------------
# Agent (non-bot) tokens. An agent is a Teleport service (here a Node/SSH
# service) that joins the cluster directly rather than via tbot. The token
# grants a system role (roles: [Node]) and has NO bot_name. On the agent side
# the join is configured under teleport.join_params.generic_oidc (see the
# docker test kit), not tbot onboarding.
# ---------------------------------------------------------------------------
AGENT_DISCOVERY="$OUT_DIR/token-agent-discovery.yaml"
cat > "$AGENT_DISCOVERY" <<EOF
# generic_oidc AGENT token (discovery + tls_ca). Grants the ${AGENT_ROLE} role to
# a joining Teleport service. Pair with a teleport.yaml join_params block that
# fetches a JWT with sub=${AGENT_SUB}.
kind: token
version: v2
metadata:
  name: ${AGENT_NAME}-discovery
  expires: "2035-01-01T00:00:00Z"
spec:
  roles: [${AGENT_ROLE}]
  join_method: generic_oidc
  generic_oidc:
    issuer: ${ISSUER}
    audience: ${AUDIENCE}
${TLS_CA_BLOCK}
    must_match_fields:
      org: ethernet-fyi
    allow_any:
      - conditions:
          - attribute: sub
            eq:
              value: ${AGENT_SUB}
EOF
echo "    wrote $AGENT_DISCOVERY"

AGENT_STATIC="$OUT_DIR/token-agent-static-jwks.yaml"
cat > "$AGENT_STATIC" <<EOF
# generic_oidc AGENT token (static_jwks). Same as token-agent-discovery.yaml but
# validates against an embedded JWKS instead of fetching discovery at join time.
kind: token
version: v2
metadata:
  name: ${AGENT_NAME}-static
  expires: "2035-01-01T00:00:00Z"
spec:
  roles: [${AGENT_ROLE}]
  join_method: generic_oidc
  generic_oidc:
    issuer: ${ISSUER}
    audience: ${AUDIENCE}
    static_jwks: |
$(printf '%s\n' "$JWKS" | indent '      ')
    must_match_fields:
      org: ethernet-fyi
    allow_any:
      - conditions:
          - attribute: sub
            eq:
              value: ${AGENT_SUB}
EOF
echo "    wrote $AGENT_STATIC"

# Scoped variant: a scoped_token (proto resource, v1) for a scoped bot. Uses the
# discovery + tls_ca path. Pair it with the static scoped-{role,bot,role-assignment}
# resources in genericoidc-test/scoped/.
SCOPED="$OUT_DIR/scoped-token-discovery.yaml"
cat > "$SCOPED" <<EOF
# Scoped generic_oidc bot token (discovery + tls_ca). Requires the scopes
# feature: create everything with TELEPORT_UNSTABLE_SCOPES=yes.
#   tctl create -f genericoidc-test/scoped/scoped-role.yaml
#   tctl create -f genericoidc-test/scoped/scoped-bot.yaml
#   tctl create -f genericoidc-test/scoped/scoped-role-assignment.yaml
#   tctl create -f $SCOPED
kind: scoped_token
version: v1
metadata:
  name: ${SCOPED_BOT_NAME}-token
scope: ${SCOPE}
spec:
  roles: [Bot]
  join_method: generic_oidc
  usage_mode: bot
  bot_name: ${SCOPED_BOT_NAME}
  bot_scope: ${SCOPE}
  generic_oidc:
    issuer: ${ISSUER}
    audience: ${AUDIENCE}
${TLS_CA_BLOCK}
    must_match_fields:
      org: ethernet-fyi
    allow_any:
      - conditions:
          - attribute: sub
            eq:
              value: test-bot
EOF
echo "    wrote $SCOPED"

# Scoped variant using static_jwks (no discovery / network fetch at join time).
SCOPED_STATIC="$OUT_DIR/scoped-token-static-jwks.yaml"
cat > "$SCOPED_STATIC" <<EOF
# Scoped generic_oidc bot token (static_jwks). Same as scoped-token-discovery.yaml
# but validates against an embedded JWKS instead of fetching discovery. Requires
# the scopes feature: create everything with TELEPORT_UNSTABLE_SCOPES=yes.
#   tctl create -f genericoidc-test/scoped/scoped-role.yaml
#   tctl create -f genericoidc-test/scoped/scoped-bot.yaml
#   tctl create -f genericoidc-test/scoped/scoped-role-assignment.yaml
#   tctl create -f $SCOPED_STATIC
kind: scoped_token
version: v1
metadata:
  name: ${SCOPED_BOT_NAME}-token
scope: ${SCOPE}
spec:
  roles: [Bot]
  join_method: generic_oidc
  usage_mode: bot
  bot_name: ${SCOPED_BOT_NAME}
  bot_scope: ${SCOPE}
  generic_oidc:
    issuer: ${ISSUER}
    audience: ${AUDIENCE}
    static_jwks: |
$(printf '%s\n' "$JWKS" | indent '      ')
    must_match_fields:
      org: ethernet-fyi
    allow_any:
      - conditions:
          - attribute: sub
            eq:
              value: test-bot
EOF
echo "    wrote $SCOPED_STATIC"

# Scoped AGENT tokens (roles: [Node], usage_mode: unlimited, assigned_scope).
# Node/Kube are the non-bot roles that support scoping (see rolesSupportingScopes
# in lib/scopes/joining/token.go). Scope IS consumed at agent (host) join time:
# GenerateHostCertsForJoin passes token.GetAssignedScope() as AgentScope into the
# host cert (lib/auth/join.go), so the joined instance is pinned to assigned_scope.
# Requires the scopes feature (TELEPORT_UNSTABLE_SCOPES=yes) to create + join.
SCOPED_AGENT_DISCOVERY="$OUT_DIR/scoped-token-agent-discovery.yaml"
cat > "$SCOPED_AGENT_DISCOVERY" <<EOF
# Scoped generic_oidc AGENT token (discovery + tls_ca). The joined Node is pinned
# to assigned_scope. Requires the scopes feature (TELEPORT_UNSTABLE_SCOPES=yes).
kind: scoped_token
version: v1
metadata:
  name: ${SCOPED_AGENT_NAME}-discovery
scope: ${SCOPE}
spec:
  roles: [${AGENT_ROLE}]
  join_method: generic_oidc
  usage_mode: unlimited
  assigned_scope: ${SCOPE}
  generic_oidc:
    issuer: ${ISSUER}
    audience: ${AUDIENCE}
${TLS_CA_BLOCK}
    must_match_fields:
      org: ethernet-fyi
    allow_any:
      - conditions:
          - attribute: sub
            eq:
              value: ${AGENT_SUB}
EOF
echo "    wrote $SCOPED_AGENT_DISCOVERY"

SCOPED_AGENT_STATIC="$OUT_DIR/scoped-token-agent-static-jwks.yaml"
cat > "$SCOPED_AGENT_STATIC" <<EOF
# Scoped generic_oidc AGENT token (static_jwks). The joined Node is pinned to
# assigned_scope. Requires the scopes feature (TELEPORT_UNSTABLE_SCOPES=yes).
kind: scoped_token
version: v1
metadata:
  name: ${SCOPED_AGENT_NAME}-static
scope: ${SCOPE}
spec:
  roles: [${AGENT_ROLE}]
  join_method: generic_oidc
  usage_mode: unlimited
  assigned_scope: ${SCOPE}
  generic_oidc:
    issuer: ${ISSUER}
    audience: ${AUDIENCE}
    static_jwks: |
$(printf '%s\n' "$JWKS" | indent '      ')
    must_match_fields:
      org: ethernet-fyi
    allow_any:
      - conditions:
          - attribute: sub
            eq:
              value: ${AGENT_SUB}
EOF
echo "    wrote $SCOPED_AGENT_STATIC"

# Kubernetes OIDC token — regression check for the SHARED oidc.CachingTokenValidator
# (the code the caching changes touch). Unlike static_jwks (unaffected), this
# fetches discovery + JWKS from the issuer at join time. The kube OIDC validator
# has NO custom-CA support, so the issuer MUST be reachable with real, system-
# trusted TLS — i.e. point ISSUER at your reverse-proxy URL, not localhost.
KUBE="$OUT_DIR/token-kubernetes-oidc.yaml"
cat > "$KUBE" <<EOF
# Kubernetes join (oidc). Requires ISSUER to have valid public TLS (kube OIDC
# does not support a custom tls_ca). Mint the SA token with:
#   curl -fsS "${ISSUER}/k8s/token?namespace=${K8S_NAMESPACE}&serviceaccount=${K8S_SERVICE_ACCOUNT}" > /tmp/sa-token.jwt
# The audience must be the cluster name (${CLUSTER_NAME}); /k8s/token defaults to
# that. Create the bot:  tctl bots add ${K8S_BOT_NAME} --roles=access
kind: token
version: v2
metadata:
  name: ${K8S_BOT_NAME}
  expires: "2035-01-01T00:00:00Z"
spec:
  roles: [Bot]
  bot_name: ${K8S_BOT_NAME}
  join_method: kubernetes
  kubernetes:
    type: oidc
    oidc:
      issuer: ${ISSUER}
    allow:
      - service_account: "${K8S_NAMESPACE}:${K8S_SERVICE_ACCOUNT}"
EOF
echo "    wrote $KUBE"

echo
echo "Unscoped bot:   tctl create -f $DISCOVERY   (and/or $STATIC)"
echo "Unscoped agent: tctl create -f $AGENT_DISCOVERY   (and/or $AGENT_STATIC)"
echo "Kubernetes:     tctl create -f $KUBE   (ISSUER must have public TLS)"
echo "Scoped:    TELEPORT_UNSTABLE_SCOPES=yes tctl create -f genericoidc-test/scoped/scoped-role.yaml \\"
echo "             -f genericoidc-test/scoped/scoped-bot.yaml \\"
echo "             -f genericoidc-test/scoped/scoped-role-assignment.yaml \\"
echo "             -f $SCOPED"
