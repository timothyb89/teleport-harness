#!/usr/bin/env sh
# Cert sidecar: obtain + maintain ONE wildcard LE cert for *.lab.$HARNESS_DOMAIN
# via Cloudflare DNS-01, and keep an installed copy at /certs/wildcard.{crt,key}
# for the cluster proxies to mount. Issue-once (persisted in the /acme.sh volume) +
# renew loop, so LE production rate limits stay safe across many cluster runs.
#
# Uses the neilpang/acme.sh image: acme.sh state lives in /acme.sh (persistent
# volume); /entry.sh proxies to acme.sh. Cloudflare token comes from CF_Token.
set -eu

: "${HARNESS_DOMAIN:?HARNESS_DOMAIN required}"
: "${CF_Token:?CF_Token (Cloudflare API token) required}"
ACME_EMAIL="${ACME_EMAIL:-admin@${HARNESS_DOMAIN}}"
DOMAIN="*.lab.${HARNESS_DOMAIN}"
CRT=/certs/wildcard.crt
KEY=/certs/wildcard.key

acme() { /entry.sh "$@"; }

install_cert() {
  acme --install-cert -d "$DOMAIN" \
    --key-file "$KEY" \
    --fullchain-file "$CRT" \
    --reloadcmd "echo '[certs] installed cert for $DOMAIN'"
}

acme --register-account -m "$ACME_EMAIL" --server letsencrypt || true

if [ ! -s "$CRT" ] || [ ! -s "$KEY" ]; then
  echo "[certs] issuing $DOMAIN via Cloudflare DNS-01 (Let's Encrypt production)..."
  acme --issue --dns dns_cf -d "$DOMAIN" --server letsencrypt --keylength 2048
  install_cert
else
  echo "[certs] existing cert found at $CRT, skipping issuance"
fi

echo "[certs] entering renew loop (checks every 12h)"
while true; do
  sleep 43200
  # --cron renews only if near expiry; reinstall to refresh /certs on renewal.
  acme --cron || true
  install_cert || true
done
