#!/bin/sh
# Shared helpers for containers that drive the in-cluster Kubernetes API (the k8s-runner
# component's k3s). Sourced by k8s-operator-entrypoint.sh and k8s-apply-entrypoint.sh,
# both of which run the rancher/k3s image itself as their runner: it already ships
# kubectl + a busybox shell, so there is no second image to pull and no version skew.
#
# Expects: K3S_HOST (the k3s container name), KUBECONFIG_SRC (the shared-volume path k3s
# writes its admin kubeconfig to).

: "${K3S_HOST:?K3S_HOST required}"
: "${KUBECONFIG_SRC:=/kubeconfig/k3s.yaml}"
export KUBECONFIG=/tmp/kubeconfig

# k8s_wait_ready — block until the API answers AND the node is schedulable.
# k3s writes its kubeconfig pointing at 127.0.0.1, which is meaningless from a sibling
# container, so rewrite the server to the container name (valid because the entrypoint
# passes --tls-san with that name).
k8s_wait_ready() {
  echo "[k8s] waiting for $KUBECONFIG_SRC ..."
  i=0
  while [ ! -s "$KUBECONFIG_SRC" ]; do
    i=$((i + 1))
    [ "$i" -gt 120 ] && { echo "[k8s] kubeconfig never appeared" >&2; return 1; }
    sleep 2
  done
  sed "s#https://127.0.0.1:6443#https://${K3S_HOST}:6443#; s#https://localhost:6443#https://${K3S_HOST}:6443#" \
    "$KUBECONFIG_SRC" > "$KUBECONFIG"

  echo "[k8s] waiting for the API server ..."
  i=0
  while ! kubectl get --raw /readyz >/dev/null 2>&1; do
    i=$((i + 1))
    [ "$i" -gt 120 ] && { echo "[k8s] API server never became ready" >&2; return 1; }
    sleep 2
  done

  # Node Ready also means the k3s AGENT finished starting — which is when it imports the
  # image tarballs under /var/lib/rancher/k3s/agent/images. Waiting here is what makes
  # `imagePullPolicy: Never` safe for the locally-built operator image.
  echo "[k8s] waiting for the node to be Ready ..."
  kubectl wait --for=condition=Ready node --all --timeout=180s || return 1
  kubectl get nodes -o wide
}

# resolve_ip <hostname> — first IPv4 A record, via busybox nslookup (the k3s image has no
# getent). Drops the "Address: 127.0.0.11:53" server line and any loopback answer.
resolve_ip() {
  nslookup "$1" 2>/dev/null \
    | awk '/^Address/ {print $NF}' \
    | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' \
    | grep -v '^127\.' \
    | head -1
}

# k8s_done <marker> — signal completion to a compose healthcheck, then idle so the
# container stays inspectable (its logs are the deploy log the report captures) instead
# of exiting and taking them with it.
k8s_done() {
  touch "$1"
  echo "[k8s] done -> $1"
  tail -f /dev/null
}
