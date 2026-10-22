"""Opus delta review of D13 round 3 (l2-add, hub#29 eee12bf). Strict xfail = an open finding; drop the marker when fixed."""

from __future__ import annotations

from test_webapi_review_r2 import _clock, _good, _try, w  # noqa: F401  (w is the fixture)


def test_r3d_spray_cannot_hold_a_new_source_of_the_real_user_beyond_the_cap(w):  # noqa: F811
    t = _clock(w)
    t[0] += 1000
    for i in range(20):  # a spray at one user id: 20 wrong passwords from unknown sources, the delay is at its cap
        _try(w, w.ann, f"203.0.113.{i + 1}", password="wrong wrong wrong")
        t[0] += 1
    # the stranger keeps going at ONE wrong password per ~80 s, a fresh source each time (far under every cap);
    # the real user, on a never-seen address (a new mobile network), signs in with the CORRECT password and TOTP
    # 30 s after each of them (the helper advances the clock 30 s for a fresh code)
    codes = []
    for k in range(8):  # ~11 minutes
        t[0] += 50_000
        _try(w, w.ann, f"198.18.{k}.1", password="wrong wrong wrong")
        s, o, _ = _good(w, "192.0.2.77")
        codes.append(o.get("error", s))
    # the PR's residual says "waits up to 60 s"; in fact the real user never gets in while the spray runs, and
    # after 5 refused correct sign-ins its own pair turns rate_limited for the rest of the 15-min window
    assert 200 in codes, codes


def test_r3d_neighbours_in_the_same_24_cannot_lock_out_a_known_source(w, monkeypatch):  # noqa: F811
    from orch_hub.webapi import api as A

    monkeypatch.setattr(A, "SIGNIN_NET_LIMIT", (30, 60_000))  # scaled: 200/min in production, 10 neighbours x 20/min
    t = _clock(w)
    assert _good(w, "203.0.113.200")[0] == 200  # the real user's home address becomes a known source
    t[0] += 1000
    for i in range(1, 40):  # strangers elsewhere in the same /24 (same ISP pool / CGNAT range / office block)
        _try(w, "0" * 32, f"203.0.113.{i}")
    s, o, _ = _good(w, "203.0.113.200")
    assert s == 200, o
