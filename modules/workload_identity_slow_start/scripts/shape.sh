#!/bin/sh
# Install a netem qdisc on this container's interfaces, then hold the network namespace open
# for the tbot that joins it (services.yml.j2 puts tbot in `network_mode: service:` this one).
#
# ORDER MATTERS TWICE:
#   * iproute2 is fetched BEFORE any shaping, so the apk download is never itself throttled;
#   * /tmp/shaped — the healthcheck tbot waits on — is touched only AFTER tc reports the qdisc
#     installed, so tbot's first packet cannot predate the degraded link.
#
# NETEM="" means the control profile: no qdisc at all. It still touches /tmp/shaped (the
# container is still the namespace owner), and it still prints what the interface looks like,
# so "unshaped" is an observed fact in the log rather than an assumption.
set -eu

NETEM="${NETEM:-}"

echo "[shape] installing iproute2 (before any shaping, so this fetch is not throttled)"
apk add --no-cache iproute2 >/dev/null 2>&1 || {
  echo "[shape] FATAL: could not install iproute2" >&2
  exit 1
}

DEVS="$(ls /sys/class/net | grep -v '^lo$' || true)"
echo "[shape] interfaces: ${DEVS:-<none>}"

for dev in $DEVS; do
  if [ -n "$NETEM" ]; then
    # `replace` rather than `add`: idempotent if the container is ever restarted in place.
    # Unquoted on purpose — NETEM is an argument LIST ("delay 900ms rate 64kbit limit 40").
    tc qdisc replace dev "$dev" root netem $NETEM
  fi
  echo "[shape] $dev: $(tc qdisc show dev "$dev" | tr '\n' ' ')"
done

if [ -n "$NETEM" ]; then
  echo "[shape] RESULT shaped=yes netem=$NETEM"
else
  echo "[shape] RESULT shaped=no netem="
fi

touch /tmp/shaped
echo "[shape] holding the network namespace open"
exec sleep infinity
