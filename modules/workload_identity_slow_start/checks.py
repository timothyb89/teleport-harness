"""Host-side ACTOR for workload_identity_slow_start.

It records; the module's declarative verbs judge (see references/authoring.md, "Scripts: act and
record"). One record per profile, holding what the three containers of that profile observed:

  * the shaper  — whether a qdisc was actually installed, and which one;
  * tbot itself — how long the initial service-identity wait took, measured between the two DEBUG
    lines that bracket it in workload_api.go setup(), plus whether the pre-fix timeout error
    appeared;
  * the probe   — whether a real SPIFFE client got an SVID out of the socket, and how long it took.

Why host-side rather than an in-container actor: the measurement that decides this module is a
duration read out of ANOTHER container's log (tbot's), and only the host has `docker logs` for
every container at once. Nothing here mutates the cluster — it is pure observation.

The wait is reported as a number AND as `identity_wait_over_10s`, because the claim is not "it was
slow" but "it was slower than the timer that was removed would have allowed". Anything the actor
could not measure is recorded as "unknown", which no expected value matches, so a missing
measurement FAILs loudly instead of quietly reading as a pass.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

# The two DEBUG lines the fix added around the wait, and the error the removed timer produced.
WAIT_START = "Waiting for initial service identity"
WAIT_END = "Received initial service identity"
TIMEOUT_ERR = "timeout waiting for identity to be ready"

# The budget the deleted `time.After(10 * time.Second)` allowed. A wait longer than this is one
# the pre-fix build could not have survived — that is the whole discriminator of this module.
IDENTITY_WAIT_BUDGET_SECONDS = 10.0

# tbot log lines start with an RFC3339 timestamp: `2026-08-28T06:09:07.590Z INFO [TBOT] ...`
_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)")
# `[probe] RESULT case=slow served=yes elapsed=31 attempts=12 spiffe_id=spiffe://...`
# `[shape] RESULT shaped=yes netem=delay 900ms rate 64kbit limit 40`
_RESULT = re.compile(r"RESULT\s+(.*)$")

# Fallback if the rendered compose can't be read; the real list is discovered from it so that
# adding a profile to render.yaml needs no edit here.
DEFAULT_PROFILES = ["fast", "slow"]


def _profiles(cluster) -> list[str]:
    raw = cluster.state_file("docker-compose.yml") or ""
    found = sorted(set(re.findall(r"wi-tbot-([a-z0-9-]+):", raw)))
    return found or DEFAULT_PROFILES


def _parse_ts(line: str):
    m = _TS.match(line.strip())
    if not m:
        return None
    try:
        return datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
    except ValueError:
        return None


def _result_fields(log: str, keys: list[str]) -> dict:
    """Parse the LAST `RESULT k=v ...` line a script printed.

    A value may contain spaces (netem is an argument list: `delay 900ms rate 64kbit limit 40`),
    so this splits on the KNOWN keys rather than on whitespace: each value runs from just after
    its `key=` to the start of the next key's token, or to the end of the line.
    """
    line = None
    for ln in log.splitlines():
        m = _RESULT.search(ln)
        if m:
            line = m.group(1).strip()
    if line is None:
        return {}

    starts = []
    for key in keys:
        m = re.search(rf"(?:^|\s){re.escape(key)}=", line)
        if m:
            starts.append((m.end(), key))
    starts.sort()

    out = {}
    for i, (value_at, key) in enumerate(starts):
        if i + 1 < len(starts):
            next_at, next_key = starts[i + 1]
            end = next_at - len(next_key) - 1  # back up over `<next_key>=`
        else:
            end = len(line)
        out[key] = line[value_at:end].strip()
    return out


def _identity_wait(log: str) -> dict:
    """What tbot's own log says about the wait the removed timer used to bound."""
    started = ended = None
    for ln in log.splitlines():
        if started is None and WAIT_START in ln:
            started = _parse_ts(ln)
        elif ended is None and WAIT_END in ln:
            ended = _parse_ts(ln)

    fields = {
        "identity_wait_started": "yes" if started else "no",
        "identity_wait_completed": "yes" if ended else "no",
        # Asserted as a count in module.yaml too; recorded here so the record alone tells the
        # story of a pre-fix run without cross-referencing the log.
        "timeout_error_seen": "yes" if TIMEOUT_ERR in log else "no",
    }
    if started and ended:
        waited = (ended - started).total_seconds()
        fields["identity_wait_seconds"] = f"{waited:.2f}"
        fields["identity_wait_over_10s"] = "yes" if waited > IDENTITY_WAIT_BUDGET_SECONDS else "no"
    else:
        # Unmeasurable, e.g. tbot died in the wait (the pre-fix failure) or debug logging is off.
        # "unknown" matches no expected value, so this FAILs rather than passing quietly.
        fields["identity_wait_seconds"] = "unknown"
        fields["identity_wait_over_10s"] = "unknown"
    return fields


def act(cluster, nodes) -> list[dict]:
    records = []
    for case in _profiles(cluster):
        shaper = _result_fields(cluster.logs(f"wi-shaper-{case}"), ["shaped", "netem"])
        probe = _result_fields(cluster.logs(f"wi-probe-{case}"),
                               ["case", "served", "elapsed", "attempts", "spiffe_id"])
        after = {
            # Did the link really differ from the control's? (precondition, not evidence)
            "shaped": shaper.get("shaped", "unknown"),
            "netem": shaper.get("netem", "") or "(none)",
            # Did the probe run at all, and did it get an SVID?
            "probe_ran": "yes" if probe else "no",
            "svid_served": probe.get("served", "unknown"),
            "spiffe_id": probe.get("spiffe_id", "none"),
            "probe_elapsed_seconds": probe.get("elapsed", "unknown"),
            "probe_attempts": probe.get("attempts", "unknown"),
        }
        after.update(_identity_wait(cluster.logs(f"wi-tbot-{case}")))
        records.append({
            "case": case,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "actor": "checks.py",
            "before": {},
            "after": after,
        })
    return records
