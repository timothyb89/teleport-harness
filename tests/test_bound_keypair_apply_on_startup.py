"""Unit tests for modules/bound_keypair_apply_on_startup/checks.py — the restart-based
re-apply verification, exercised with a staged fake cluster (no docker). Proves the checks
PASS on correct-fix behavior AND actually FAIL on the regression they're meant to catch
(re-apply overwriting status), so they're not vacuous."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from harness.cluster import Cluster

REPO = Path(__file__).resolve().parent.parent
MODULE_DIR = REPO / "modules" / "bound_keypair_apply_on_startup"


def _load_checks():
    spec = importlib.util.spec_from_file_location("bk_aos_checks", MODULE_DIR / "checks.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CHECKS = _load_checks()

REAL_KEY = "ssh-ed25519 AAAArealboundkeyfromthejoin"
REAL_COUNT = 2
REAL_SECRET = "harness-bk-regsecret"


def _token(bound_public_key, recovery_count, registration_secret, limit):
    return {
        "kind": "token",
        "metadata": {"name": "bk-token"},
        "spec": {"join_method": "bound_keypair",
                 "bound_keypair": {"recovery": {"limit": limit}}},
        "status": {"bound_keypair": {
            "bound_public_key": bound_public_key,
            "recovery_count": recovery_count,
            "registration_secret": registration_secret}},
    }


class StagedCluster(Cluster):
    """get_resource returns `before` until restart_auth() is called, then `after` —
    the two states a real auth restart (re-applying the token) transitions between."""

    def __init__(self, before, after, restart_ok=True):
        super().__init__("c1")
        self._before, self._after, self._restart_ok = before, after, restart_ok
        self._restarted = False
        self.rewrote = False

    def get_resource(self, kind, name):
        return self._after if self._restarted else self._before

    def exec_out(self, suffix, argv):
        self.rewrote = True
        return (0, "")

    def restart_auth(self, timeout=150.0):
        if self._restart_ok:
            self._restarted = True
        return self._restart_ok


# The module's checks.py is now a pure ACTOR: act() drives the scenario and RETURNS
# observation records, and module.yaml's observation_* verbs do the judging. These tests
# assert on what act() RECORDED — including that it records on the unhappy paths, since a
# missing record is indistinguishable from a crashed actor.
def _rec(records, case="reapply-on-restart"):
    return next(r for r in records if r["case"] == case)


def test_act_records_before_and_after_the_restart():
    before = _token(REAL_KEY, REAL_COUNT, REAL_SECRET, 1)
    # correct fix: status preserved verbatim, spec's recovery.limit updated to NEW_LIMIT.
    after = _token(REAL_KEY, REAL_COUNT, REAL_SECRET, CHECKS.NEW_LIMIT)
    c = StagedCluster(before, after)

    rec = _rec(CHECKS.act(c, []))

    assert c.rewrote  # it rewrote the applied YAML before restarting
    assert rec["actor"] == "checks.py"          # so the proof can link back to this file
    assert rec["before"]["status.bound_keypair.bound_public_key"] == REAL_KEY
    assert rec["after"]["status.bound_keypair.bound_public_key"] == REAL_KEY
    assert rec["after"]["spec.bound_keypair.recovery.limit"] == str(CHECKS.NEW_LIMIT)
    # every field the checks assert over is present in BOTH snapshots, or
    # observation_unchanged would fail on "not recorded" rather than on a real change
    assert set(rec["before"]) == set(CHECKS.FIELDS) == set(rec["after"])


def test_act_records_the_regression_faithfully():
    before = _token(REAL_KEY, REAL_COUNT, REAL_SECRET, 1)
    # regression: re-apply lands the config-supplied BOGUS status (wiping the real bound key).
    after = _token(CHECKS.BOGUS_KEY, CHECKS.BOGUS_COUNT, CHECKS.BOGUS_SECRET, CHECKS.NEW_LIMIT)

    rec = _rec(CHECKS.act(StagedCluster(before, after), []))

    # the actor does not judge — it records the transition the checks will flag
    assert rec["before"]["status.bound_keypair.bound_public_key"] == REAL_KEY
    assert rec["after"]["status.bound_keypair.bound_public_key"] == CHECKS.BOGUS_KEY
    assert rec["after"]["status.bound_keypair.registration_secret"] == CHECKS.BOGUS_SECRET
    assert rec["after"]["spec.bound_keypair.recovery.limit"] == str(CHECKS.NEW_LIMIT)


def test_never_bound_records_instead_of_bailing_silently(monkeypatch):
    monkeypatch.setattr(CHECKS, "BOUND_WAIT_TIMEOUT", 0.0)  # don't poll the full window
    never = _token("", 0, "", 1)
    c = StagedCluster(never, never)

    rec = _rec(CHECKS.act(c, []))

    assert not c.rewrote  # bailed before mutating anything / restarting
    # a record with an empty key makes observation_unchanged fail loudly; NO record would
    # look identical to a crashed actor, so the unhappy path must still produce one
    assert rec["after"]["status.bound_keypair.bound_public_key"] == ""
    assert "never bound a key" in rec["note"]


def test_restart_failure_is_recorded():
    before = _token(REAL_KEY, REAL_COUNT, REAL_SECRET, 1)
    rec = _rec(CHECKS.act(StagedCluster(before, before, restart_ok=False), []))
    assert "did not come back healthy" in rec["note"]
    # before == after, so the spec-updated check fails: "unchanged" must not be mistaken
    # for success when the re-apply never actually ran
    assert rec["after"]["spec.bound_keypair.recovery.limit"] == "1"
