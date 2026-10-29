"""The test harness's TOTP helper never offers a step the hub has already spent (CI flake, test_phones_pairing).

The hub accepts steps ``s-1, s, s+1`` and claims the HIGHEST match, so when two adjacent steps share a 6-digit code a
code typed at ``s`` spends ``s+1`` too. ``SECRET`` is such a secret: its codes at steps ``S`` and ``S+1`` are equal.
Before the fix ``next_code`` just advanced 30 s and offered the spent step: ``bad_credentials``."""

from __future__ import annotations

import pytest
from channels_helpers import PW, Env, fresh_code, wall_codes
from test_phones_pairing import code_of, register_phone, w  # noqa: F401  (the pairing fixture)

from orch_hub.channels.service import totp_code

SECRET = (3288180).to_bytes(20, "big")
S = 60_000_003  # the Clock starts at step 60_000_000


def test_the_secret_collides_where_the_tests_need_it():
    assert totp_code(SECRET, S) == totp_code(SECRET, S + 1)


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.close()


def _give(svc_db, secrets, u):
    svc_db.w("UPDATE credentials SET totp_secret=? WHERE user_id=?", (SECRET, u))
    secrets[u] = SECRET


def test_a_code_that_spent_the_next_step_is_followed_by_a_later_one(env):
    u = env.user("ann")
    _give(env.svc.db, env.secrets, u)
    env.clock.t = S * 30_000
    env.svc.sign_in(u, PW, totp_code(SECRET, S))  # spends S+1 as well (same code)
    assert env.svc.db.q("SELECT totp_last FROM credentials WHERE user_id=?", (u,)) == [(S + 1,)]
    assert env.svc.sign_in(u, PW, env.next_code(u))["user_id"] == u
    assert env.clock() // 30_000 == S + 2


def test_fresh_code_skips_a_step_whose_code_a_neighbour_shares(env):
    env.clock.t = (S - 1) * 30_000
    assert fresh_code(SECRET, env.clock) == totp_code(SECRET, S + 2)  # S and S+1 both skipped


def test_the_flaky_pairing_sequence_with_a_colliding_secret(w):  # noqa: F811
    _give(w.env.svc.db, w.env.secrets, w.ann)
    ann, adm = w.login(w.ann), w.login(w.admin)  # ann's next code would be step S, the one that spends S+1
    s, got, _ = register_phone(w, w.ann, code_of(w, ann, w.ann), begin={"app_version": "0.0.5"})
    assert s == 200, got
    (row,) = w.call("GET", "/admin/channels", token=adm)[1]["channels"]
    assert row["user_id"] == w.ann


class WallClock:
    """A fake real clock in seconds for :func:`wall_codes`: ``sleep`` moves it, so no test waits."""

    def __init__(self, t: float) -> None:
        self.t = t

    def now(self) -> float:
        return self.t

    def sleep(self, d: float) -> None:
        self.t += d


def test_wall_codes_wait_out_the_end_of_a_step():
    c = WallClock((S - 10) * 30 + 29.0)  # 1 s left, fewer than left_s
    assert wall_codes(SECRET, (-1, 0), left_s=15, now=c.now, sleep=c.sleep) == [
        totp_code(SECRET, S - 10),
        totp_code(SECRET, S - 9),
    ]
    assert int(c.t) // 30 == S - 9 and c.t % 30 < 1


def test_wall_codes_skip_a_step_whose_code_a_neighbour_shares():
    c = WallClock(S * 30 + 1.0)
    assert wall_codes(SECRET, now=c.now, sleep=c.sleep) == [totp_code(SECRET, S + 2)]  # S and S+1 share a code
    c = WallClock((S + 1) * 30 + 1.0)
    assert wall_codes(SECRET, (-1, 0), now=c.now, sleep=c.sleep) == [totp_code(SECRET, S + 2), totp_code(SECRET, S + 3)]
