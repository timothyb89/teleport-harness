# Plan: cutting k8s-backend startup/setup cost (not yet built)

Status: **not started — rough plan for a future session.** Written after the first
`--package` run from Beams against the real host (`docs/remote-kubernetes.md`, "First
`--package` run against the real host from Beams"), which hit two avoidable costs: no
wildcard cert existed yet on the real host (worked around with a one-off in-cluster
`acme.sh` Pod), and a brand-new client needs a handful of host properties hand-copied
into its target env before anything works. Both are fixable by moving state that's
really a property of the **host**, not the **client**, into the cluster itself.

## Idea 1: cert-manager for self-contained ACME issuance

**Viable — straightforward win, same output shape as today.**

Today (`infra/k8s/import-cert.sh`): the wildcard `*.lab.<domain>` cert is issued by the
**docker** backend's `acme.sh` sidecar (`ingress/scripts/certs-entrypoint.sh`, DNS-01 via
Cloudflare) into a docker volume, and a script copies it into the k8s Secret
`harness-system/wildcard-cert` that every cluster's proxy mounts. That means a k8s-only
host needs a docker backend run *somewhere* just to mint the cert — which is why the
2026-10-27 Beams run had to improvise an ad-hoc `acme.sh` Pod instead.

**Plan:**
- Install cert-manager into `harness-system` as another vendored manifest set
  (`infra/k8s/manifests/harness-1x-cert-manager.yaml` + CRDs, same pinned/vendored
  pattern `k8s/fetch-vendored.sh` already uses for Traefik's CRDs).
- A `ClusterIssuer` using cert-manager's **built-in** Cloudflare DNS-01 solver (no
  webhook needed — `solvers: [{dns01: {cloudflare: {apiTokenSecretRef: ...}}}]`),
  pointed at the `cf-dns-token` Secret that already exists in `harness-system` (we found
  it live on 2026-10-27: `kubectl get secret -n harness-system cf-dns-token`).
- A `Certificate` for `*.lab.<domain>` whose `secretName` is literally `wildcard-cert`,
  with the SAME keys (`tls.crt`/`tls.key`) the Secret already has — so **nothing
  downstream changes**: every pod that mounts `wildcard-cert` today keeps working
  unmodified. `infra/k8s/import-cert.sh` and the whole docker-backend dependency it
  implies go away.
- Renewal is then cert-manager's job (it reissues before expiry and updates the Secret
  in place), replacing a manual rerun of `import-cert.sh`.
- Production LE rate limits: cert-manager already does the right thing here (it only
  reissues near expiry, same issue-once spirit as the existing acme.sh sidecar) — no
  special handling needed.
- Migration from the current cert: let cert-manager issue fresh once; the old Secret
  just gets overwritten with an equivalent one. No cutover dance needed since the
  Secret's name/shape is kept identical on purpose.

**Open question:** does a running teleport proxy/oidc-server pick up a renewed cert file
without a restart? If not, a renewal needs to roll the affected pods (e.g. a cert-manager
`Certificate` can drive this today via a `reloader`-style annotation, or the harness
could watch the Secret's resourceVersion itself at `up` time) — needs a quick check
against teleport's own cert-watching behavior before relying on it silently working.

## Idea 2: offload client/target config to the cluster

**Viable — moderate win, keep it additive (never replace the "no ambient context"
invariant).**

Today a new client's target env (`targets/<name>.env`) must hand-copy several values
that are really **properties of the host**, fixed once infra is bootstrapped, not of the
client reaching it: `HARNESS_DOMAIN`, `INGRESS_PORT` (must match
`harness-20-traefik.yaml`'s `exposedPort: 9443` by convention, with nothing enforcing
it), and the `REGISTRY_PUSH` default string built from both. A typo in any of these
fails quietly or confusingly rather than loudly.

**Plan:**
- Infra bring-up publishes a ConfigMap, e.g. `harness-system/harness-host-config`, holding
  `domain`, `ingress_port`, `registry_push` (and anything else that turns out to be
  host-fixed rather than client-fixed — NOT `KUBE_CONTEXT`/`KUBECONFIG`, which are
  inherently about how THIS client reaches the API server and can't be discovered before
  that connection exists).
- `load_target` (`lib/common.sh`)/`doctor` (`lib/doctor.sh`), right after
  `_k8s_resolve_connection` establishes `HARNESS_KUBE_CONTEXT`, do one
  `kubectl get cm harness-host-config` and use it to **fill in** any of
  `HARNESS_DOMAIN`/`INGRESS_PORT`/`REGISTRY_PUSH` the target env left unset — an explicit
  value in the target env still wins (needed for the lima stand-in, or a future second
  real host with a different domain, or any override).
- Net effect: a k8s target env shrinks to essentially `BACKEND=k8s` +
  `KUBECONFIG`/`KUBE_CONTEXT` (+ `FORWARD`), with the rest auto-discovered and therefore
  guaranteed to match what's actually deployed instead of a copy-pasted constant.
- Natural place to also publish whether the cert is cert-manager-backed or still the
  stopgap (idea 1), so `doctor` can say so instead of just checking the Secret exists.
- Docker backend: out of scope — there's no single shared host to query a ConfigMap
  from, so it stays env-file driven as-is.

## Rough sequencing (for whoever picks this up)

1. cert-manager manifests + `ClusterIssuer`/`Certificate`, verified against the lima
   stand-in first (cheaper iteration loop than the real host), then the real host.
   Remove `infra/k8s/import-cert.sh` and the "First real run" doc's cert workaround once
   confirmed.
2. `harness-host-config` ConfigMap, published by the SAME infra bring-up step that
   installs the manifests (so it's never stale relative to what's actually running).
3. `load_target`/`doctor` changes to read it; update `targets/default.env.example`'s k8s
   section and `docs/remote-kubernetes.md`'s "Resuming on another machine" to show the
   shrunk target env.
4. Re-verify the parity runs (`docs/remote-kubernetes.md`'s parity table) still hold —
   this only touches bring-up/client plumbing, not rendering or verification, so no
   behavior change is expected, but worth a quick confirmation run.

## Non-goals (for now)

- Making `HARNESS_DOMAIN`/`INGRESS_PORT` fully immutable/un-overridable from the client
  — overrides stay supported (see "explicit value still wins" above); this is about
  cutting required boilerplate, not enforcing policy.
- Multi-host / multi-domain support — one `harness-host-config` per cluster the client
  points `KUBE_CONTEXT` at is enough for now.
