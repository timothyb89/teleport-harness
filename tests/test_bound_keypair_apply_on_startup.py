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


def _by_tag(results):
    return {r.args[-1]: r for r in results}


def test_reapply_correct_fix_all_checks_pass():
    before = _token(REAL_KEY, REAL_COUNT, REAL_SECRET, 1)
    # correct fix: status preserved verbatim, spec's recovery.limit updated to NEW_LIMIT.
    after = _token(REAL_KEY, REAL_COUNT, REAL_SECRET, CHECKS.NEW_LIMIT)
    c = StagedCluster(before, after)

    res = _by_tag(CHECKS.checks(c, []))

    assert c.rewrote  # it rewrote the applied YAML before restarting
    assert res["bound"].status == "PASS"
    assert res["status-preserved"].status == "PASS"
    assert res["status-discarded"].status == "PASS"
    assert res["spec-updated"].status == "PASS"
    assert res["secret-unchanged"].status == "PASS"


def test_reapply_regression_overwrites_status_is_caught():
    before = _token(REAL_KEY, REAL_COUNT, REAL_SECRET, 1)
    # regression: re-apply lands the config-supplied BOGUS status (wiping the real bound key).
    after = _token(CHECKS.BOGUS_KEY, CHECKS.BOGUS_COUNT, CHECKS.BOGUS_SECRET, CHECKS.NEW_LIMIT)
    c = StagedCluster(before, after)

    res = _by_tag(CHECKS.checks(c, []))

    assert res["status-preserved"].status == "FAIL"   # real key was wiped
    assert res["status-discarded"].status == "FAIL"   # bogus values landed
    assert res["secret-unchanged"].status == "FAIL"   # secret overwritten
    assert res["spec-updated"].status == "PASS"       # spec still applied


def test_never_bound_fails_fast(monkeypatch):
    monkeypatch.setattr(CHECKS, "BOUND_WAIT_TIMEOUT", 0.0)  # don't poll the full window
    never = _token("", 0, "", 1)
    c = StagedCluster(never, never)

    res = CHECKS.checks(c, [])

    assert len(res) == 1
    assert res[0].status == "FAIL" and res[0].args == ["bound"]
    assert not c.rewrote  # bailed before mutating anything / restarting


def test_restart_failure_is_reported():
    before = _token(REAL_KEY, REAL_COUNT, REAL_SECRET, 1)
    c = StagedCluster(before, before, restart_ok=False)

    res = CHECKS.checks(c, [])

    assert res[-1].status == "FAIL" and res[-1].args == ["restart"]
