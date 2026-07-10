# Shared ingress + cert sidecar lifecycle and per-cluster SNI route management.
# shellcheck shell=bash

# Ensure the shared network, volumes, and the ingress+cert stack are up, and that
# the wildcard cert has been issued.
ingress_up() {
  docker network inspect teleport-harness >/dev/null 2>&1 || docker network create teleport-harness >/dev/null
  docker volume create harness-acme >/dev/null
  docker volume create harness-certs >/dev/null
  mkdir -p "$INGRESS_DIR/dynamic"
  hlog "bringing up shared ingress + cert sidecar"
  compose teleport-harness-ingress "$INGRESS_DIR/docker-compose.yml" \
    --env-file "$HARNESS_ROOT/targets/${TARGET:-default}.env" up -d >/dev/null
  ingress_wait_cert
}

# Block until the wildcard cert lands in the shared volume (issued via DNS-01).
ingress_wait_cert() {
  hlog "waiting for wildcard cert (*.$LAB_DOMAIN) via DNS-01"
  for _ in $(seq 1 60); do
    if docker run --rm -v harness-certs:/certs alpine:3 \
         sh -c '[ -s /certs/wildcard.crt ] && [ -s /certs/wildcard.key ]' 2>/dev/null; then
      hok "wildcard cert present"; return 0
    fi
    sleep 3
  done
  die "wildcard cert never appeared; check: docker logs harness-certs"
}

# register_route <fqdn> <backend host:port>  — add/replace an SNI route and reload.
register_route() {
  local fqdn="$1" backend="$2"; local name="${fqdn%%.*}"
  mkdir -p "$INGRESS_DIR/dynamic"
  echo "${fqdn} ${backend};" > "$INGRESS_DIR/dynamic/${name}.map"
  ingress_reload
}

# unregister_route <fqdn>
unregister_route() {
  local fqdn="$1"; local name="${fqdn%%.*}"
  rm -f "$INGRESS_DIR/dynamic/${name}.map"
  ingress_reload
}

# Reload nginx to pick up map changes. `nginx -s reload` re-reads the *.map includes;
# fall back to a container restart if reload fails (rare lima mount stale-read).
ingress_reload() {
  if docker exec harness-ingress nginx -t >/dev/null 2>&1 \
     && docker exec harness-ingress nginx -s reload >/dev/null 2>&1; then
    return 0
  fi
  hwarn "nginx reload failed; restarting ingress"
  docker restart harness-ingress >/dev/null 2>&1 || true
}
