#!/usr/bin/env bash
# Refresh the third-party manifests vendored under infra/k8s. Each is pinned here; bump a
# version, re-run, review the diff, commit. Vendored rather than fetched at boot so a host
# install is reproducible and never depends on a URL that might move.
#
#   system-upgrade-controller  → host/harness-90-suc-crd.yaml, host/harness-91-suc.yaml
#   Traefik CRDs               → manifests/harness-18-traefik-crds.yaml
#
# Traefik's CRDs are vendored instead of installed by a chart: rendered they exceed the
# 1 MiB limit on a Helm release Secret, so no release can own them (the traefik-crds chart
# fails with `Secret "sh.helm.release.v1…" is invalid: data: Too long`). Keep the CRD
# version in step with the controller chart in manifests/harness-20-traefik.yaml.
set -euo pipefail
SUC_VERSION=v0.20.2
TRAEFIK_CRDS_CHART_VERSION=1.18.0

K8S="$(cd "$(dirname "$0")" && pwd)"

base="https://github.com/rancher/system-upgrade-controller/releases/download/$SUC_VERSION"
curl -sfL -o "$K8S/host/harness-90-suc-crd.yaml" "$base/crd.yaml"
# One local change, made here so a refresh can't lose it: the controller pod runs as
# SELinux type spc_t. It mounts the host's /etc/ssl, /etc/pki and /etc/ca-certificates
# for its CA bundle, and on an enforcing host (CoreOS) a container_t process may not read
# their cert_t files — it then can't resolve the release channel at all ("x509: failed to
# load system roots … ca-certificates.crt: permission denied"). The upgrade JOBS it
# spawns are privileged, which already runs them as spc_t.
curl -sfL "$base/system-upgrade-controller.yaml" |
  uv run --quiet --project "$K8S/../.." python -c '
import sys, yaml
docs = [d for d in yaml.safe_load_all(sys.stdin) if d]
for d in docs:
    if d["kind"] == "Deployment" and d["metadata"]["name"] == "system-upgrade-controller":
        pod = d["spec"]["template"]["spec"]
        pod.setdefault("securityContext", {})["seLinuxOptions"] = {"type": "spc_t"}
        break
else:
    sys.exit("system-upgrade-controller Deployment not found: re-check the spc_t patch")
print("# Vendored by infra/k8s/fetch-vendored.sh (+ spc_t patch). Do not edit.")
yaml.safe_dump_all(docs, sys.stdout, sort_keys=False)
' > "$K8S/host/harness-91-suc.yaml"

{
  echo "# Vendored by infra/k8s/fetch-vendored.sh: traefik-crds chart $TRAEFIK_CRDS_CHART_VERSION. Do not edit."
  helm template harness-traefik-crds oci://ghcr.io/traefik/helm/traefik-crds \
    --version "$TRAEFIK_CRDS_CHART_VERSION" 2>/dev/null
} > "$K8S/manifests/harness-18-traefik-crds.yaml"

wc -c "$K8S"/host/harness-9[01]-*.yaml "$K8S/manifests/harness-18-traefik-crds.yaml"
