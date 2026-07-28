#!/bin/sh
# Entrypoint for a MODULE's Kubernetes runner: apply that module's custom resources into
# the disposable cluster and report, per file, exactly what the API server said.
#
# A rejected manifest is frequently the finding rather than an accident (a CRD schema that
# cannot express what the Teleport API supports), so every apply is logged with its verdict
# and NOTHING here aborts the run. The `k8s_*` and `resource_*` checks decide what the
# outcome means.
set -u
. /scripts/k8s-common.sh

: "${MANIFEST_DIR:=/work}"
: "${RECONCILE_WAIT:=15}"

k8s_wait_ready || { echo "[apply] cluster never came up" >&2; k8s_done /tmp/apply-failed; }

applied=0; rejected=0
for f in "$MANIFEST_DIR"/*.yaml; do
  [ -e "$f" ] || continue
  name="$(basename "$f")"
  echo "[apply] ---- $name ----"
  # Capture first: the error text is the evidence, and a pipeline would hide the status.
  out="$(kubectl apply -f "$f" 2>&1)"; rc=$?
  echo "$out" | sed 's/^/[apply]   /'
  if [ "$rc" -eq 0 ]; then
    applied=$((applied + 1))
    echo "[apply] RESULT $name: accepted"
  else
    rejected=$((rejected + 1))
    echo "[apply] RESULT $name: REJECTED by the API server (exit $rc)"
  fi
done
echo "[apply] summary: $applied accepted, $rejected rejected"

# Give the operator a beat to reconcile what was accepted, then show what it wrote back.
# The status conditions are what k8s_condition asserts against.
echo "[apply] waiting ${RECONCILE_WAIT}s for reconciliation ..."
sleep "$RECONCILE_WAIT"
kubectl -n teleport get teleportprovisiontokens -o wide 2>&1 | sed 's/^/[apply] /'
kubectl -n teleport get teleportprovisiontokens -o json 2>&1 \
  | grep -iE '"(name|type|status|message|reason)"' | sed 's/^/[apply]   /'

k8s_done /tmp/apply-done
