"""Tests for the capability overlay.

The interesting logic is in `plan()`, which is pure, so most of this needs no
fakes at all.
"""

from __future__ import annotations

import pytest

from logic.capabilities import (
    CapabilityDriftError,
    CapabilityKey,
    CapabilityWriteError,
    DriftPolicy,
    apply_standard_capabilities,
    load_standard,
    plan,
)


def cap(name, category, authority=False, rsos=False):
    return {
        "authority_enabled": authority,
        "rsos_enabled": rsos,
        "capability_type": {"name": name, "category": category, "display_name": name},
    }


@pytest.fixture
def standard():
    return load_standard(
        {
            "capabilities": [
                cap("jurisdiction_view", 0, True, True),
                cap("alerts", 2, True, True),
                cap("alerts_fire", 0, True, True),
                cap("agent_511", 0, False, False),
                cap("agent_511", 2, True, True),
                cap("doordash", 2, False, False),
            ]
        }
    )


@pytest.fixture
def live():
    """A freshly created integration: same catalog, everything off."""
    return {
        "capabilities": [
            cap("jurisdiction_view", 0),
            cap("alerts", 2),
            cap("alerts_fire", 0),
            cap("agent_511", 0),
            cap("agent_511", 2),
            cap("doordash", 2),
        ]
    }


class FakeClient:
    """Echoes the PATCH body back, as the real API does.

    `fail_on` is a set of (name, category); any PATCH that tries to enable one
    of them raises, mimicking Andromeda's 500 on alerts.
    """

    def __init__(self, live, *, echo=None, fail_on=frozenset()):
        self._live = live
        self._echo = echo
        self._fail_on = set(fail_on)
        self.patched = None
        self.patch_calls = 0

    def get(self, path):
        return self._live

    def patch(self, path, json):
        self.patch_calls += 1
        for entry in json["capabilities"]:
            ct = entry["capability_type"]
            if (ct["name"], ct["category"]) in self._fail_on and (
                entry["authority_enabled"] or entry["rsos_enabled"]
            ):
                raise RuntimeError("500 Internal Server Error")
        self.patched = json
        return self._echo if self._echo is not None else json


def test_alerts_fallback_retries_without_alerts(live, standard):
    client = FakeClient(live, fail_on={("alerts", 2), ("alerts_fire", 0)})
    report = apply_standard_capabilities(client, "2451", "4827", standard=standard)

    assert client.patch_calls == 2
    assert report.applied
    assert report.fallback_used
    assert [str(k) for k in report.alerts_skipped] == [
        "alerts(cat 2)", "alerts_fire(cat 0)"
    ]
    # the alerts stay off, everything else still applied
    sent = {(c["capability_type"]["name"], c["capability_type"]["category"]): c
            for c in client.patched["capabilities"]}
    assert sent[("alerts", 2)]["authority_enabled"] is False
    assert sent[("alerts_fire", 0)]["authority_enabled"] is False
    assert sent[("jurisdiction_view", 0)]["authority_enabled"] is True
    assert sent[("agent_511", 2)]["authority_enabled"] is True
    # changed no longer claims the skipped ones
    assert all(c.key not in set(report.alerts_skipped) for c in report.changed)
    assert report.first_attempt_error


def test_no_fallback_when_failure_is_unrelated(live, standard):
    """A failure with no alerts changes in play must propagate."""
    client = FakeClient(live, fail_on={("doordash", 2)})
    # standard wants doordash off, so it is never enabled -> patch succeeds
    report = apply_standard_capabilities(client, "2451", "4827", standard=standard)
    assert client.patch_calls == 1
    assert not report.fallback_used


def test_retry_failure_reraises(live, standard):
    """If it still fails without alerts, the original error surfaces."""

    class AlwaysFails(FakeClient):
        def patch(self, path, json):
            self.patch_calls += 1
            raise RuntimeError("500 Internal Server Error")

    client = AlwaysFails(live)
    with pytest.raises(RuntimeError, match="500"):
        apply_standard_capabilities(client, "2451", "4827", standard=standard)
    assert client.patch_calls == 2


def test_fallback_can_be_disabled(live, standard):
    client = FakeClient(live, fail_on={("alerts", 2)})
    with pytest.raises(RuntimeError):
        apply_standard_capabilities(
            client, "2451", "4827", standard=standard, alerts_fallback=False
        )
    assert client.patch_calls == 1


def test_fallback_preserves_already_enabled_alerts(standard):
    """An alerts capability already on must not be switched off by the retry."""
    already_on = {
        "capabilities": [
            cap("jurisdiction_view", 0),
            cap("alerts", 2, True, True),      # already enabled
            cap("alerts_fire", 0),             # needs enabling, will fail
            cap("agent_511", 0),
            cap("agent_511", 2),
            cap("doordash", 2),
        ]
    }
    client = FakeClient(already_on, fail_on={("alerts_fire", 0)})
    report = apply_standard_capabilities(client, "2451", "4827", standard=standard)
    sent = {(c["capability_type"]["name"], c["capability_type"]["category"]): c
            for c in client.patched["capabilities"]}
    assert sent[("alerts", 2)]["authority_enabled"] is True   # untouched
    assert sent[("alerts_fire", 0)]["authority_enabled"] is False
    assert [str(k) for k in report.alerts_skipped] == ["alerts_fire(cat 0)"]


def test_enables_the_standard_set(live, standard):
    body, report = plan(live, standard)
    assert len(report.changed) == 4  # the four that are True in the standard
    enabled = {
        c["capability_type"]["name"]
        for c in body["capabilities"]
        if c["authority_enabled"]
    }
    assert enabled == {"jurisdiction_view", "alerts", "alerts_fire", "agent_511"}


def test_leaves_standard_off_capabilities_off(live, standard):
    body, _ = plan(live, standard)
    off = {
        c["capability_type"]["name"]
        for c in body["capabilities"]
        if not c["authority_enabled"] and not c["rsos_enabled"]
    }
    assert "doordash" in off


def test_turns_off_a_capability_the_standard_says_should_be_off(live, standard):
    """An account configured by hand may have extras enabled; the standard wins."""
    for entry in live["capabilities"]:
        if entry["capability_type"]["name"] == "doordash":
            entry["authority_enabled"] = entry["rsos_enabled"] = True
    body, report = plan(live, standard)
    assert any(c.key == CapabilityKey("doordash", 2) for c in report.changed)
    doordash = next(
        c for c in body["capabilities"] if c["capability_type"]["name"] == "doordash"
    )
    assert doordash["authority_enabled"] is False


def test_same_name_different_category_resolved_independently(live, standard):
    body, _ = plan(live, standard)
    by_key = {
        (c["capability_type"]["name"], c["capability_type"]["category"]): c
        for c in body["capabilities"]
    }
    assert by_key[("agent_511", 0)]["authority_enabled"] is False
    assert by_key[("agent_511", 2)]["authority_enabled"] is True


def test_input_is_not_mutated(live, standard):
    plan(live, standard)
    assert all(not c["authority_enabled"] for c in live["capabilities"])


def test_idempotent(live, standard):
    body, _ = plan(live, standard)
    _, second = plan(body, standard)
    assert second.is_noop


def test_capability_only_in_target_is_left_alone(live, standard):
    live["capabilities"].append(cap("brand_new", 0))
    body, report = plan(live, standard)
    assert report.missing_from_standard == [CapabilityKey("brand_new", 0)]
    assert report.has_drift
    new = next(
        c for c in body["capabilities"] if c["capability_type"]["name"] == "brand_new"
    )
    assert new["authority_enabled"] is False


def test_capability_only_in_standard_is_reported(live, standard):
    live["capabilities"] = [
        c for c in live["capabilities"] if c["capability_type"]["name"] != "alerts_fire"
    ]
    _, report = plan(live, standard)
    assert report.missing_from_target == [CapabilityKey("alerts_fire", 0)]


def test_drift_policy_raise(live, standard):
    live["capabilities"].append(cap("brand_new", 0))
    client = FakeClient(live)
    with pytest.raises(CapabilityDriftError):
        apply_standard_capabilities(
            client, "2451", "4827", standard=standard, drift_policy=DriftPolicy.RAISE
        )


def test_dry_run_sends_nothing(live, standard):
    client = FakeClient(live)
    report = apply_standard_capabilities(
        client, "2451", "4827", standard=standard, dry_run=True
    )
    assert client.patched is None
    assert report.applied is False
    assert report.changed


def test_noop_sends_nothing(live, standard):
    body, _ = plan(live, standard)
    client = FakeClient(body)
    report = apply_standard_capabilities(client, "2451", "4827", standard=standard)
    assert client.patched is None
    assert report.is_noop


def test_write_verification_catches_silent_rejection(live, standard):
    """Server accepts the PATCH but refuses to enable one flag."""
    echo = {
        "capabilities": [
            cap("jurisdiction_view", 0, True, True),
            cap("alerts", 2, False, False),  # server refused
            cap("alerts_fire", 0, True, True),
            cap("agent_511", 0),
            cap("agent_511", 2, True, True),
            cap("doordash", 2),
        ]
    }
    client = FakeClient(live, echo=echo)
    with pytest.raises(CapabilityWriteError) as exc:
        apply_standard_capabilities(client, "2451", "4827", standard=standard)
    assert len(exc.value.mismatched) == 1


def test_duplicate_key_in_standard_is_rejected():
    with pytest.raises(Exception):
        load_standard({"capabilities": [cap("x", 0), cap("x", 0)]})