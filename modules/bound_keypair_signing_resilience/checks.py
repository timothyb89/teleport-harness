"""Host-side ACTOR for bound_keypair_signing_resilience.

Verifies (and quantifies the limits of) timothyb89/bound-keypair-resilient-signing
(core commit dad8f4a4934, "Fix bound keypair lockouts when signing fails") end to end,
against a REAL auth server and a REAL bot, not the unit tests' mocks.

Drives bk-bot's joins itself (`tbot start -c /etc/tbot.yaml`, `oneshot: true`, directory
storage) rather than letting tbot run as its own daemon, so each invocation is a
deterministic, individually-inspectable join/recovery attempt. Toggles two TEST-ONLY
fault hooks built into a disposable worktree (never merged - see the hooks' own comments
in lib/auth/bot.go and lib/join/boundkeypair/boundkeypair.go) via sentinel files on the
auth container:

  /tmp/inject-cert-sign-failure   - GenerateUserCerts() fails before ANY backend write.
                                    The fix's own claim: should never lock.
  /tmp/inject-post-commit-fault   - the token patch (write B) commits successfully, but
                                    the response carrying the new certs/join-state never
                                    reaches the client - e.g. a crash/dropped connection
                                    in that window. NOT covered by the fix (signing order
                                    doesn't help once the write has already landed): the
                                    client's retry presents a join-state document that is
                                    now genuinely stale, which `verifyBoundKeypairJoinState`
                                    (lib/join/boundkeypair/boundkeypair.go) is SUPPOSED to
                                    catch - by design, since a stale join state might also
                                    mean a stolen keypair. So this case is expected to
                                    lock the token even on the fixed commit, and does -
                                    this module exists to make that residual risk visible
                                    and measured, not to disprove the fix.

A 3rd case reruns the write-B-race case across several trials, each on its OWN freshly
created token, with the fault active on half of them - giving a real LOCK RATE rather than
one data point, and separating "the fault fired and it locked" from "no fault, no lock".
The per-trial token is load-bearing: a bound_keypair lockout targets the JOIN TOKEN, so
reusing a single token means the first lock silently blocks every later trial (see the
loop's own comment for how that invalidated an earlier version of this case).
"""

from __future__ import annotations

import base64
import json
import time

TOKEN = "bk-token"
BOT_NAME = "bk-bot"
REG_SECRET = "harness-bksr-regsecret"  # must match render.yaml

CERT_SIGN_FAULT = "/tmp/inject-cert-sign-failure"
POST_COMMIT_FAULT = "/tmp/inject-post-commit-fault"

# The hooks' own error strings (see the TEST-ONLY commit's comments in lib/auth/bot.go and
# lib/join/boundkeypair/boundkeypair.go). Matched against the captured `tbot start` output
# so "the fault fired" is evidence from the client, not an assumption that touching the
# sentinel file had an effect.
CERT_SIGN_FAULT_TEXT = "injected: simulated GenerateUserCerts signing failure"
POST_COMMIT_FAULT_TEXT = "injected: simulated response-delivery failure after a successful commit"

# Each trial is a real token plus three real joins, so keep it modest. The fault is
# alternated deterministically rather than sampled: with one fresh token per trial each
# outcome is independent, so alternating measures the same two things (P(lock | fault
# fired), and that no fault means no lock) while guaranteeing BOTH arms are populated -
# a random schedule can leave one empty and make the verdict flake on the RNG.
THROTTLE_TRIALS = 10

ACTOR = "checks.py"


def _dig(doc, path):
    cur = doc
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return False, None
        cur = cur[key]
    return True, cur


FIELDS = (
    "status.bound_keypair.recovery_count",
    "status.bound_keypair.bound_public_key",
    "status.bound_keypair.bound_bot_instance_id",
)


def _lock_count(cluster) -> int:
    rc, out = cluster.exec_out("auth", ["tctl", "get", "lock", "--format", "json"])
    if rc != 0 or not out.strip():
        return 0
    try:
        data = json.loads(out)
    except ValueError:
        return 0
    return len(data) if isinstance(data, list) else (1 if data else 0)


def _bot_instance_generation(cluster, instance_id: str) -> int:
    """The highest generation recorded for a specific bot_instance - the counter
    lib/auth/bot.go's updateBotInstance()/commitFn bump, independent of (and not
    observable through) the token's status.bound_keypair.recovery_count, which only
    write B (patchToken) touches. -1 if the instance can't be read at all."""
    if not instance_id:
        return -1
    rc, out = cluster.exec_out(
        "auth", ["tctl", "get", f"bot_instance/{BOT_NAME}/{instance_id}", "--format", "json"])
    if rc != 0 or not out.strip():
        return -1
    try:
        data = json.loads(out)
    except ValueError:
        return -1
    doc = data[0] if isinstance(data, list) and data else data
    gens = [a.get("generation", -1) for a in
            (doc.get("status", {}) or {}).get("latest_authentications", []) or []]
    return max(gens) if gens else -1


def _snapshot(cluster) -> dict:
    """Token fields plus the live lock count, all as strings - one snapshot shape so
    declarative `observation_equals`/`observation_unchanged` checks can assert on either,
    the same contract bound_keypair_apply_on_startup's checks.py uses."""
    doc = cluster.get_resource("token", TOKEN) or {}
    out = {}
    for f in FIELDS:
        found, v = _dig(doc, f)
        out[f] = "" if not found or v is None else str(v)
    out["lock_count"] = str(_lock_count(cluster))
    out["bot_instance_generation"] = str(
        _bot_instance_generation(cluster, out["status.bound_keypair.bound_bot_instance_id"]))
    return out


def _set_fault(cluster, path: str, on: bool) -> None:
    argv = ["touch", path] if on else ["rm", "-f", path]
    cluster.exec_out("auth", argv)


def _write_file(cluster, suffix: str, path: str, content: str) -> None:
    """base64 round-trip so YAML (newlines, braces, quotes) survives the exec argv intact -
    the same trick act() uses to persist its records."""
    b64 = base64.b64encode(content.encode()).decode()
    cluster.exec_out(suffix, ["sh", "-c", f"printf %s '{b64}' | base64 -d > {path}"])


# An explicit recovery limit, unlike the bootstrapped bk-token which leaves it defaulting
# to 1: a trial spends one recovery on its retry, and a limit-exhausted refusal would be
# indistinguishable from the lockout this case is trying to count.
_TRIAL_TOKEN_YAML = """kind: token
version: v2
metadata: {{name: {name}}}
spec:
  roles: [Bot]
  bot_name: {bot}
  join_method: bound_keypair
  bound_keypair:
    onboarding:
      registration_secret: {secret}
    recovery:
      mode: relaxed
      limit: 5
"""


def _create_trial_token(cluster, name: str, secret: str) -> bool:
    path = f"/tmp/{name}.yaml"
    _write_file(cluster, "auth", path,
                _TRIAL_TOKEN_YAML.format(name=name, bot=BOT_NAME, secret=secret))
    rc, _ = cluster.exec_out("auth", ["tctl", "create", "-f", path])
    return rc == 0


def _trial_tbot_config(cluster, idx: int, token: str, secret: str) -> str | None:
    """Write a per-trial tbot config derived from the bootstrapped one, so proxy_server/port
    stay defined in exactly one place (config/tbot.yaml.j2). Fresh storage and output dirs
    mean the trial starts with NO bound keypair, no join state and no working identity -
    a genuine first join against its own token.

    Returns the in-container path, or None if the config could not be derived - never a
    silently-unmodified config, which would quietly run the trial against the SHARED
    bk-token and reintroduce exactly the cross-trial interference this design removes.
    """
    rc, base = cluster.exec_out("bkbot", ["cat", "/etc/tbot.yaml"])
    if rc != 0 or not base.strip():
        return None
    cfg = (base
           .replace(f"token: {TOKEN}", f"token: {token}")
           .replace(f"registration_secret: {REG_SECRET}", f"registration_secret: {secret}")
           .replace("path: /var/lib/tbot", f"path: /var/lib/tbot-t{idx}")
           .replace("path: /out/id", f"path: /out/id-t{idx}"))
    if f"token: {token}" not in cfg or f"registration_secret: {secret}" not in cfg:
        return None
    if f"path: /var/lib/tbot-t{idx}" not in cfg:
        return None
    path = f"/tmp/tbot-t{idx}.yaml"
    _write_file(cluster, "bkbot", path, cfg)
    return path


# tbot's directory storage mixes two independent things in /var/lib/tbot: the bound
# keypair + prior join state (PrivateKeyPath/JoinStatePath/KeyHistoryPath in
# lib/auth/join/boundkeypair/boundkeypair.go: id_bkp, id_bkp.pub, bkp_state,
# bkp_key_history.json), and the bot's own short-lived working identity (key, key.pub,
# key-cert.pub, tlscert, tlscacerts, sshcacerts, tokenhash). If the latter is still
# present and valid, HandleBoundKeypairJoin takes the CHEAP "standard rejoin" branch
# (lib/join/boundkeypair/boundkeypair.go: hasBoundPublicKey && hasBoundUniqueID &&
# hasIncomingUniqueID) - no recovery consumed, no mutators, no patchToken() call at all,
# so neither fault hook downstream of a write can ever fire. Only the "hard rejoin"
# branch (no incoming unique ID) calls patchToken() and exercises the write B cases this
# module cares about. So: wipe the working identity (keeping the bound keypair files)
# before every invocation, forcing hard rejoin every time.
_WORKING_IDENTITY_FILES = (
    "key", "key.pub", "key-cert.pub", "tlscert", "tlscacerts", "sshcacerts", "tokenhash",
)


def _settle(cluster, result):
    # `tctl get`/our resource reads go through the auth cache - a read immediately after a
    # write can still return the pre-write value (the same pitfall bound_keypair_status's
    # module notes). Give it a moment to catch up before any snapshot is taken.
    time.sleep(1.5)
    return result


def _run_tbot_standard(cluster) -> tuple[int, str]:
    """One `tbot start` invocation leaving any cached working identity in place, so a
    bot that already has a valid cert takes the CHEAP "standard rejoin" branch (reuses
    its existing bot_instance via lib/auth/bot.go's updateBotInstance - write A). This is
    the realistic shape of a periodic renewal (tbot renews every ~20 min while its cert
    stays valid), and the only way to exercise write A's own write-then-sign history
    (bug item 1 in dad8f4a4934) rather than the brand-new-instance path a hard rejoin
    always takes (expectNewUniqueID in boundkeypair.go)."""
    return _settle(cluster, cluster.exec_out("bkbot", ["tbot", "start", "-c", "/etc/tbot.yaml"]))


# tbot's directory storage mixes two independent things in /var/lib/tbot: the bound
# keypair + prior join state (PrivateKeyPath/JoinStatePath/KeyHistoryPath in
# lib/auth/join/boundkeypair/boundkeypair.go: id_bkp, id_bkp.pub, bkp_state,
# bkp_key_history.json), and the bot's own short-lived working identity (key, key.pub,
# key-cert.pub, tlscert, tlscacerts, sshcacerts, tokenhash). If the latter is still
# present and valid, HandleBoundKeypairJoin takes the CHEAP "standard rejoin" branch
# (lib/join/boundkeypair/boundkeypair.go: hasBoundPublicKey && hasBoundUniqueID &&
# hasIncomingUniqueID) - no recovery consumed, no mutators, no patchToken() call at all,
# so the write-B fault hook downstream of patchToken() can never fire. Only the "hard
# rejoin" branch (no incoming unique ID) calls patchToken() and exercises write B.
_WORKING_IDENTITY_FILES = (
    "key", "key.pub", "key-cert.pub", "tlscert", "tlscacerts", "sshcacerts", "tokenhash",
)


def _run_tbot_hard(cluster) -> tuple[int, str]:
    """One `tbot start` invocation via a forced hard rejoin (wipes the cached working
    identity first, keeping the bound keypair files) so this call exercises the full
    ceremony including patchToken() - write B."""
    rm_argv = ["rm", "-f"] + [f"/var/lib/tbot/{f}" for f in _WORKING_IDENTITY_FILES]
    cluster.exec_out("bkbot", rm_argv)
    return _settle(cluster, cluster.exec_out("bkbot", ["tbot", "start", "-c", "/etc/tbot.yaml"]))


def _run_tbot_hard_in(cluster, storage: str, cfg: str) -> tuple[int, str]:
    """`_run_tbot_hard` against an arbitrary storage dir + config - the per-trial equivalent
    used by the throttle loop. Wipes only the working identity, keeping the bound keypair
    and prior join state, so this is a hard rejoin (patchToken/write B) rather than a fresh
    registration."""
    rm_argv = ["rm", "-f"] + [f"{storage}/{f}" for f in _WORKING_IDENTITY_FILES]
    cluster.exec_out("bkbot", rm_argv)
    return _settle(cluster, cluster.exec_out("bkbot", ["tbot", "start", "-c", cfg]))


def _record(case, before, after, rc, note="", fault_text=None, expect_fault_text=None):
    # `log_contains` greps the CONTAINER's own log stream (`kubectl logs`/`docker logs`),
    # which only ever holds the entrypoint process's output - here that's `sleep infinity`,
    # which prints nothing. `tbot start` runs via `exec`, so its stdout/stderr goes straight
    # back to THIS process, never into that stream. So "the fault really fired" has to be
    # asserted from the captured exec output directly, not via a log_contains check (which
    # would stay a silent, non-failing SKIP forever - log_contains is deliberately soft).
    # join_exit_code and newly_locked ride in `after` (not just as top-level/derived
    # fields) so the declarative observation_equals verb can assert on them too -
    # otherwise "state unchanged" would be indistinguishable from "the actor never
    # actually attempted the join", and "it locked" would have no generic verb to assert
    # with (there's no observation_changed/observation_greater, only equals/unchanged).
    newly_locked = before.get("lock_count") != after.get("lock_count")
    # Write B (patchToken) only ever touches the TOKEN's status - recovery_count.
    newly_committed_write_b = (
        before.get("status.bound_keypair.recovery_count")
        != after.get("status.bound_keypair.recovery_count")
    )
    # Write A (updateBotInstance/commitFn) only ever touches the BOT INSTANCE's own
    # generation counter - a completely separate resource recovery_count says nothing
    # about. Only meaningful when before/after share the same bound_bot_instance_id
    # (true for a standard rejoin; a hard rejoin mints a new instance every time, so
    # this is intentionally not asserted on in those cases).
    newly_committed_write_a = (
        before.get("status.bound_keypair.bound_bot_instance_id")
        == after.get("status.bound_keypair.bound_bot_instance_id")
        and before.get("bot_instance_generation") != after.get("bot_instance_generation")
    )
    after_full = {
        **after,
        "join_exit_code": str(rc),
        "newly_locked": str(newly_locked).lower(),
        "newly_committed_write_b": str(newly_committed_write_b).lower(),
        "newly_committed_write_a": str(newly_committed_write_a).lower(),
    }
    if expect_fault_text is not None:
        after_full["fault_confirmed"] = str(
            bool(fault_text) and expect_fault_text in fault_text).lower()
    rec = {
        "case": case,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "token": TOKEN,
        "actor": ACTOR,
        "before": before,
        "after": after_full,
        "join_exit_code": rc,
    }
    if note:
        rec["note"] = note
    return rec


DONE_MARKER = "/tmp/bksr-records.json"


def act(cluster, nodes):
    """Idempotency guard: the harness's verify-retry loop may call `act()` more than once
    against the SAME cluster. Everything this module does (joins, fault toggles) mutates
    real server state and is NOT safe to repeat, unlike bound_keypair_apply_on_startup's
    actor. So: run the real sequence only once per cluster, persist its result on the auth
    container (survives for the cluster's lifetime, unlike this Python process), and every
    later call just replays that file instead of re-driving anything."""
    rc, out = cluster.exec_out("auth", ["cat", DONE_MARKER])
    if rc == 0 and out.strip():
        try:
            return json.loads(out)
        except ValueError:
            pass  # fall through and redo it rather than returning garbage silently

    records = _act_once(cluster, nodes)
    payload = json.dumps(records)
    b64 = base64.b64encode(payload.encode()).decode()
    cluster.exec_out("auth", ["sh", "-c", f"printf %s '{b64}' | base64 -d > {DONE_MARKER}"])
    return records


def _act_once(cluster, nodes):
    records = []

    # 0. Initial join: binds the key, creates bot_instance generation 1, no fault active.
    #    Leaves a valid cached identity behind, so the NEXT call takes the cheap
    #    "standard rejoin" path - exactly what's needed for case 1.
    rc0, out0 = _run_tbot_standard(cluster)
    if rc0 != 0:
        return [_record("initial-join", {}, _snapshot(cluster), rc0,
                         note=f"initial join failed (exit {rc0}), nothing below could run: {out0}")]

    # 1. Cert-signing fails before any write - the fix's own claim: never locks. Uses the
    #    STANDARD rejoin (cached identity intact) so this exercises write A
    #    (updateBotInstance/commitFn on the EXISTING bot_instance) specifically, not the
    #    unrelated new-instance-creation path a hard rejoin would take instead.
    before1 = _snapshot(cluster)
    _set_fault(cluster, CERT_SIGN_FAULT, True)
    rc1, out1 = _run_tbot_standard(cluster)
    after1 = _snapshot(cluster)
    _set_fault(cluster, CERT_SIGN_FAULT, False)
    rc1_retry, _ = _run_tbot_standard(cluster)
    after1_retry = _snapshot(cluster)
    records.append(_record(
        "cert-signing-failure", before1, after1, rc1,
        note="fault active during this join; expect failure, no state change, no lock",
        fault_text=out1, expect_fault_text=CERT_SIGN_FAULT_TEXT))
    records.append(_record("cert-signing-failure-retry", after1, after1_retry, rc1_retry,
                            note="fault cleared; expect this retry to succeed with no lock"))

    # 2. Write B commits, but the response never reaches the client - the race the fix
    #    does not close. Needs a HARD rejoin (patchToken()/write B only runs when
    #    mutators are non-empty, which only happens on initial join or hard rejoin).
    #    Expect: recovery_count DOES change (the write landed), and the retry -
    #    presenting now-stale state - gets LOCKED.
    before2 = _snapshot(cluster)
    _set_fault(cluster, POST_COMMIT_FAULT, True)
    rc2, out2 = _run_tbot_hard(cluster)
    after2 = _snapshot(cluster)
    _set_fault(cluster, POST_COMMIT_FAULT, False)
    rc2_retry, _ = _run_tbot_hard(cluster)
    after2_retry = _snapshot(cluster)
    records.append(_record(
        "post-commit-fault", before2, after2, rc2,
        note="fault active during this join; expect failure but the write DOES land",
        fault_text=out2, expect_fault_text=POST_COMMIT_FAULT_TEXT))
    records.append(_record("post-commit-fault-retry", after2, after2_retry, rc2_retry,
                            note="fault cleared; this is the case expected to be LOCKED despite the fix"))

    # 3. Quantify the residual race across trials, each on its OWN fresh token.
    #
    #    Why a fresh token per trial: a bound_keypair lockout targets the JOIN TOKEN (these
    #    joins are non-renewable, so tryLockBotDueToGenerationMismatch sets
    #    spec.target.join_token - lib/auth/bot.go), so once case 2 above has locked
    #    bk-token, EVERY subsequent join on it is refused before it can reach patchToken().
    #    An earlier version of this loop reused bk-token after case 2, which meant none of
    #    its trials could commit write B or create a new lock: it ran its whole budget
    #    against an already-locked token and reported a falsely reassuring "no lock within
    #    the trial budget" (recovery_count and last_recovered_at provably never moved for
    #    the loop's entire duration). A token per trial - its own registration secret, its
    #    own tbot storage, an explicit recovery limit so that can't gate either - makes
    #    each trial independent and the tally mean something.
    #
    #    Each trial is THREE joins on a brand new token, and the first one is essential:
    #      1. a clean initial join, so the client ends up holding a VALID join state;
    #      2. a hard rejoin with the fault maybe active - patchToken() commits (issuing a
    #         new join state) but the response is lost, leaving the client on its OLD one;
    #      3. a hard rejoin, always fault-free, which presents that now-STALE join state.
    #    Step 1 is what makes step 3 lockable. Without it the faulted join IS the initial
    #    join, so a lost response leaves the client with NO join state rather than a stale
    #    one - refused, but not a lockout, because staleness is what
    #    verifyBoundKeypairJoinState treats as possible key theft. An earlier version of
    #    this loop omitted step 1 and measured a 0% lock rate with the faults provably
    #    firing (every faulted join exited 1 with fault_confirmed true); the
    #    some_faulted_trial_locked check below is what surfaced that rather than letting a
    #    0% rate read as good news.
    trials = []
    for i in range(THROTTLE_TRIALS):
        trial_token, trial_secret = f"{TOKEN}-t{i}", f"{REG_SECRET}-t{i}"
        storage = f"/var/lib/tbot-t{i}"
        if not _create_trial_token(cluster, trial_token, trial_secret):
            trials.append({"trial": i, "setup_ok": False, "why": "token create failed"})
            continue
        cfg = _trial_tbot_config(cluster, i, trial_token, trial_secret)
        if cfg is None:
            trials.append({"trial": i, "setup_ok": False, "why": "per-trial tbot config failed"})
            continue

        # 1. Seed: a clean join so there is a valid join state to go stale later.
        rc_seed, _ = _settle(cluster, cluster.exec_out("bkbot", ["tbot", "start", "-c", cfg]))
        if rc_seed != 0:
            trials.append({"trial": i, "setup_ok": False,
                           "why": f"seed join failed (exit {rc_seed})"})
            continue

        # Baseline AFTER seeding, so the seed join can never be mistaken for the lock.
        locks_before = _lock_count(cluster)
        fault_on = (i % 2 == 0)

        # 2. The race: write B commits, the response carrying the new join state does not.
        _set_fault(cluster, POST_COMMIT_FAULT, fault_on)
        rc_fault, out_fault = _run_tbot_hard_in(cluster, storage, cfg)
        _set_fault(cluster, POST_COMMIT_FAULT, False)

        # 3. The retry, nothing interfering: a lock here is the stale join state alone.
        rc_retry, _ = _run_tbot_hard_in(cluster, storage, cfg)

        trials.append({
            "trial": i,
            "setup_ok": True,
            "token": trial_token,
            "fault_on": fault_on,
            "fault_confirmed": bool(fault_on and POST_COMMIT_FAULT_TEXT in (out_fault or "")),
            "seed_join_exit_code": rc_seed,
            "faulted_join_exit_code": rc_fault,
            "retry_exit_code": rc_retry,
            "locked": _lock_count(cluster) > locks_before,
        })

    ran = [t for t in trials if t.get("setup_ok")]
    faulted = [t for t in ran if t["fault_on"]]
    clean = [t for t in ran if not t["fault_on"]]
    faulted_locked = [t for t in faulted if t["locked"]]
    clean_locked = [t for t in clean if t["locked"]]
    records.append({
        "case": "throttled-signing-failures",
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "token": f"{TOKEN}-t0..{TOKEN}-t{THROTTLE_TRIALS - 1}",
        "actor": ACTOR,
        "before": {},
        # Everything assertable goes in `after`: observation_equals reads rec["after"][f]
        # and FAILs on a missing field, which is why the previous version of this record -
        # with its fields at the top level - could carry no declarative checks at all, and
        # so reported a wrong answer without failing anything.
        "after": {
            "trials_planned": str(THROTTLE_TRIALS),
            "trials_ran": str(len(ran)),
            "setup_failures": str(len(trials) - len(ran)),
            "faulted_trials": str(len(faulted)),
            "clean_trials": str(len(clean)),
            "faulted_trials_locked": str(len(faulted_locked)),
            "clean_trials_locked": str(len(clean_locked)),
            "lock_rate_faulted": f"{len(faulted_locked) / len(faulted):.2f}" if faulted else "n/a",
            "lock_rate_clean": f"{len(clean_locked) / len(clean):.2f}" if clean else "n/a",
            # Actor-computed booleans: observation_equals only does exact equality and
            # there is no observation_greater verb, so the comparisons happen here.
            # `some_faulted_trial_locked` is the anti-vacuity guard - if the trials were
            # being refused up front again, nothing would lock and this would go false.
            "some_faulted_trial_locked": str(bool(faulted_locked)).lower(),
            "no_clean_trial_locked": str(not clean_locked).lower(),
        },
        "trials": trials,
        "note": ("each trial is a fresh token + three joins (clean seed, maybe-faulted hard "
                 "rejoin, clean retry); the fault is alternated so half the trials carry "
                 "it. Expect every faulted trial to lock and no clean one to - the residual "
                 "race is reliable when triggered, and absent otherwise"),
    })

    return records
