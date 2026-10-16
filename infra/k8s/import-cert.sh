#!/usr/bin/env bash
# Copy the wildcard LE cert the docker ingress already issued (volume harness-certs) into
# Secret harness-system/wildcard-cert, with the same file names the auth.yaml.j2 and
# oidc-server templates mount (/certs/wildcard.{crt,key}).
#
# Stopgap for the lima stand-in and early bring-up: the long-term host gets the Secret
# from an in-cluster ACME job instead (not written yet; docs/remote-kubernetes.md §1).
set -euo pipefail
NS=harness-system
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT

docker run --rm -v harness-certs:/c:ro alpine sh -c 'cat /c/wildcard.crt' > "$tmp/wildcard.crt"
docker run --rm -v harness-certs:/c:ro alpine sh -c 'cat /c/wildcard.key' > "$tmp/wildcard.key"
[ -s "$tmp/wildcard.crt" ] && [ -s "$tmp/wildcard.key" ] || { echo "harness-certs volume has no cert (run the docker ingress once)" >&2; exit 1; }

# A `tls` Secret, so Traefik can use it for the registry route — but with the harness's
# own file names added alongside tls.crt/tls.key for pods that mount it at /certs.
kubectl -n "$NS" create secret generic wildcard-cert --type=kubernetes.io/tls \
  --from-file=tls.crt="$tmp/wildcard.crt" --from-file=tls.key="$tmp/wildcard.key" \
  --from-file=wildcard.crt="$tmp/wildcard.crt" --from-file=wildcard.key="$tmp/wildcard.key" \
  --dry-run=client -o yaml | kubectl apply -f -
openssl x509 -in "$tmp/wildcard.crt" -noout -subject -ext subjectAltName -enddate
