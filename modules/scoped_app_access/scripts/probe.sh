#!/usr/bin/env bash
#
# probe.sh — the CLIENT in this module. It does what a human would do with a scoped app
# (curl it) and RECORDS what it saw; the module's checks do the judging (see
# skills/teleport-cluster/references/authoring.md, "act and record, don't assert").
#
# Four cases land in /out/observations.json:
#
#   scoped-tunnel     GET through the SCOPED bot's application-tunnel. `hostname` is served
#                     by the backend itself (go-httpbin -use-real-hostname), so it says WHICH
#                     of the two same-named `httpbin` apps the tunnel resolved — a 200 alone
#                     could not, because an unscoped `httpbin` also exists.
#   unscoped-tunnel   the same GET through the UNSCOPED bot's tunnel. Must reach the OTHER
#                     backend: getApp skips scoped apps for an unscoped request.
#   output-cert       what the `application` output actually WROTE: the app name, target scope
#                     and derived public address carried by the certificate's subject
#                     (Teleport encodes route-to-app as subject RDNs with private OIDs, see
#                     lib/tlsca/ca.go — so plain `openssl x509 -subject` reads them).
#   output-cert-access  the written credentials used as a client cert straight against the
#                     proxy, at the scoped app's derived public address. --connect-to reaches
#                     the proxy container directly and -k skips hostname verification,
#                     because that address is `<32-char-base32-hash>.<proxy-host>` and the
#                     harness wildcard cert/DNS only cover one level of `*.lab.<domain>`.
#
# NOT `set -e`: a failing case must still be recorded. A missing record is indistinguishable
# from a crashed probe, while a recorded bad value is a finding.
set -uo pipefail

OBS=/out/observations.json
ACTOR=scripts/probe.sh            # module-relative: the proof links back to this file
CERT=/creds/tlscert
KEY=/creds/key

SCOPED_URL="http://${SCOPED_TUNNEL}"
UNSCOPED_URL="http://${UNSCOPED_TUNNEL}"

log() { echo "[probe] $*"; }

# --- recording -------------------------------------------------------------------------
# One {case, at, actor, before, after} record per case, flushed after every case so a hang
# or a timeout still leaves everything observed up to that point.
: > /tmp/records.jsonl
record() {  # record <case> <after-json-object>
  jq -cn --arg case "$1" --arg at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --arg actor "$ACTOR" \
     --argjson after "$2" '{case:$case, at:$at, actor:$actor, before:{}, after:$after}' \
     >> /tmp/records.jsonl
  jq -s '.' /tmp/records.jsonl > "$OBS"
  log "recorded $1: $2"
}

# --- helpers ---------------------------------------------------------------------------
# A tunnel listener exists as soon as tbot starts, but it only serves once the app is
# discoverable and a cert has been issued (the tunnel retries initialization), so poll for a
# real answer rather than for the port.
wait_http() {  # wait_http <url> <label>
  local url="$1" label="$2" i status
  for i in $(seq 1 "${WAIT_ATTEMPTS:-60}"); do
    status="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$url" 2>/dev/null)"
    if [ "$status" = "200" ]; then log "$label ready after ${i} attempt(s)"; return 0; fi
    sleep 2
  done
  log "$label never answered 200 (last status '${status:-none}')"
  return 1
}

wait_file() {  # wait_file <path> <label>
  local i
  for i in $(seq 1 "${WAIT_ATTEMPTS:-60}"); do
    [ -s "$1" ] && { log "$2 present after ${i} attempt(s)"; return 0; }
    sleep 2
  done
  log "$2 never appeared at $1"
  return 1
}

# GET <url> [curl-args...] -> records `http_status` + the backend's own `hostname`.
probe_case() {  # probe_case <case> <url> [extra curl args...]
  local case="$1" url="$2"; shift 2
  local body=/tmp/${case}.body status hostname
  status="$(curl -s -o "$body" -w '%{http_code}' --max-time 15 "$@" "$url" 2>/dev/null)"
  hostname="$(jq -r '.hostname // ""' "$body" 2>/dev/null || true)"
  record "$case" "$(jq -cn --arg s "${status:-none}" --arg h "$hostname" \
                      '{http_status:$s, hostname:$h}')"
}

# Read one subject RDN out of the written app certificate. Teleport puts route-to-app in the
# SUBJECT (pkix ExtraNames), not in an X509v3 extension, so `-nameopt sep_multiline` prints
# one `<oid>=<value>` line per attribute (indented, unknown OIDs shown numerically). Matched
# with an exact string comparison on the attribute name, because an OID is a prefix of its
# own siblings (…2.3 vs …2.34) and its dots would be regex wildcards.
oid_value() {  # oid_value <subject-text> <oid>
  # NB: the key is trimmed into a LOCAL copy. Assigning to $1 would make awk rebuild $0 with
  # OFS, turning `1.3.9999.1.10=httpbin` into `1.3.9999.1.10 httpbin` and taking the
  # separator this function then strips with it.
  printf '%s\n' "$1" | awk -F= -v oid="$2" '
    { key = $1; gsub(/^[ \t]+/, "", key) }
    key == oid { sub(/^[^=]*=/, ""); print; exit }'
}

# Recorded presence as a string, not a JSON boolean: observation_equals compares
# str(value) to the expected literal, so a bool would arrive as Python's "True".
yesno() { [ -n "$1" ] && echo yes || echo no; }

# --- scoped tunnel: the headline case --------------------------------------------------
wait_http "${SCOPED_URL}/status/200" "scoped tunnel"
probe_case scoped-tunnel "${SCOPED_URL}/hostname"

# --- unscoped tunnel: same app NAME, must be the other app -----------------------------
wait_http "${UNSCOPED_URL}/status/200" "unscoped tunnel"
probe_case unscoped-tunnel "${UNSCOPED_URL}/hostname"

# --- the application output's credentials ----------------------------------------------
if wait_file "$CERT" "app output certificate"; then
  subject="$(openssl x509 -in "$CERT" -noout -subject -nameopt sep_multiline,utf8 2>&1)"
  app_name="$(oid_value "$subject" 1.3.9999.1.10)"     # RouteToApp.Name
  target_scope="$(oid_value "$subject" 1.3.9999.2.34)" # RouteToApp.Scope
  public_addr="$(oid_value "$subject" 1.3.9999.1.6)"   # RouteToApp.PublicAddr
  session_id="$(oid_value "$subject" 1.3.9999.1.4)"    # RouteToApp.SessionID
  # The scope PIN is a separate RDN from the target scope: the pin is what the identity IS
  # (an encoded scopesv1.Pin), the target scope is what it is routed TO. Only presence is
  # recorded — the value is an encoded proto, not a string worth asserting on.
  scope_pin="$(oid_value "$subject" 1.3.9999.2.24)"
  log "certificate subject:"; printf '%s\n' "$subject" | sed 's/^/[probe]   /'
  record output-cert "$(jq -cn --arg n "$app_name" --arg s "$target_scope" \
      --arg a "$public_addr" --arg sid "$(yesno "$session_id")" \
      --arg pin "$(yesno "$scope_pin")" \
      '{app_name:$n, target_scope:$s, public_addr:$a, has_session_id:$sid, has_scope_pin:$pin}')"

  if [ -n "$public_addr" ]; then
    probe_case output-cert-access "https://${public_addr}/hostname" \
      -k --cert "$CERT" --key "$KEY" --connect-to "${public_addr}:443:${PROXY_ADDR}"
  else
    # No address to dial: say so as data rather than skipping the case silently, so the
    # check fails with "public_addr was empty" instead of "no observation".
    record output-cert-access '{"http_status":"no-public-addr","hostname":""}'
  fi
else
  record output-cert '{"app_name":"","target_scope":"","public_addr":"","has_session_id":"no","has_scope_pin":"no"}'
  record output-cert-access '{"http_status":"no-certificate","hostname":""}'
fi

log "observations:"; jq . "$OBS"
touch /tmp/probe-done   # the healthcheck (and the module's precondition) key off this
exec sleep infinity     # stay alive: the verifier reads $OBS with `docker exec`
